"""Immutable offline forecast freeze followed by a separate market diagnostic.

There is deliberately no exchange client, order placement, scheduler, or DB writer.
"""
import hashlib
import json
from math import isfinite
from pathlib import Path

from .contracts import distribution, instant


def freeze_forecast(path: Path, *, probabilities: dict, airport: str, target_date: str,
                    cutoff: str, frozen_at: str, model_version: str, source_sha256: str,
                    evidence_class: str) -> dict:
    from .contracts import EVIDENCE_CLASSES
    if evidence_class not in EVIDENCE_CLASSES:
        raise ValueError("Unknown evidence class")
    if instant(frozen_at) < instant(cutoff):
        raise ValueError("Forecast cannot be frozen before its input cutoff")
    if not model_version or len(source_sha256) != 64:
        raise ValueError("Model/source version evidence required")
    payload = {"schema": "v11-frozen-forecast-1", "airport": airport,
               "target_date": target_date, "cutoff": cutoff, "frozen_at": frozen_at,
               "model_version": model_version, "source_sha256": source_sha256,
               "evidence_class": evidence_class, "calibration_status": "provisional",
               "research_only": True, "automatic_promotion": False, "automatic_execution": False,
               "probabilities": {str(k): v for k, v in distribution(probabilities).items()}}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["forecast_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL semantics: historical forecasts cannot silently be replaced by reruns.
    with path.open("x") as f:
        json.dump(payload, f, sort_keys=True)
        f.write("\n")
    return payload


def market_diagnostic(frozen: dict, market: dict) -> dict:
    copy = {k: v for k, v in frozen.items() if k != "forecast_sha256"}
    body = json.dumps(copy, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if hashlib.sha256(body.encode()).hexdigest() != frozen["forecast_sha256"]:
        raise ValueError("Frozen forecast has changed")
    if instant(market["read_at"]) < instant(frozen["frozen_at"]):
        raise ValueError("Market was read before forecast freeze")
    if market["airport"] != frozen["airport"] or market["target_date"] != frozen["target_date"]:
        raise ValueError("Market station/day does not match forecast target")
    if market.get("resolution_source") != "stored_metar" or not market.get("resolution_rules_verified"):
        raise ValueError("Resolution mapping not verified; cannot substitute physical AEMET/WU targets")
    p = distribution(frozen["probabilities"])
    quotes = market["quotes"]
    if not market.get("market_id") or not quotes:
        raise ValueError("Market identity and full quote partition required")
    seen, mids, lines = set(), {}, []
    for q in quotes:
        bucket = q["bucket"]
        if bucket in seen or not q.get("outcome_id"):
            raise ValueError("Duplicate bucket or missing outcome identity")
        seen.add(bucket)
        age = (instant(frozen["cutoff"]) - instant(q["captured_at"])).total_seconds()
        if not 0 <= age <= 60:
            raise ValueError("Quote outside matching cutoff/freshness window")
        bid, ask = q["bid"], q["ask"]
        if not 0 <= bid <= ask <= 1 or ask <= 0 or q["ask_depth"] < 0:
            raise ValueError("Invalid book")
        fee, slippage = q["fee_per_share"], q["slippage_per_share"]
        if not isfinite(fee) or not isfinite(slippage) or fee < 0 or slippage < 0:
            raise ValueError("Cost must be nonnegative")
        mids[bucket] = (bid + ask) / 2
        lines.append({"bucket": bucket, "outcome_id": q["outcome_id"],
                      "forecast_probability": p.get(bucket, 0), "ask": ask, "spread": ask - bid,
                      "edge_after_cost": p.get(bucket, 0) - ask - fee - slippage,
                      "available_ask_depth": q["ask_depth"], "fill_assumption": "no_fills_assumed"})
    if not {k for k, v in p.items() if v > 0}.issubset(seen) or sum(mids.values()) <= 0:
        raise ValueError("Incomplete market partition; map open tails before comparison")
    total = sum(mids.values())
    for line in lines:
        line["normalized_midpoint_probability"] = mids[line["bucket"]] / total
    return {"forecast_sha256": frozen["forecast_sha256"], "market_id": market["market_id"],
            "quotes": lines, "trade_allowed": False, "no_trade_reason": "provisional_forecast_research_only",
            "official_roi": None, "resolution_status": market.get("resolution_status", "unknown")}
