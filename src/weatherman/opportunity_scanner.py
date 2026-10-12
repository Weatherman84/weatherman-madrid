"""Frozen, causal core for the cross-airport forward Shadow scanner.

This module has no database dependency and performs no network or Production writes.
Inputs are compact, already-computed common-model forecast snapshots.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from datetime import date, datetime, time, timedelta, timezone
from importlib.resources import files
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


APP_NAME = "Weatherman Opportunity Scanner"
APP_VERSION = "0.1-shadow"
RULE_SET_VERSION = "frozen_opportunity_rule_set_v1"
RULE_SET_FILE_SHA256 = "eac98339693550d66cbe02a4cacd109c42085a0b616cd32238cf1773e3863b6c"
FORWARD_EVIDENCE_CLASS = "sequential_oos_shadow"
CHECKPOINT_OFFSETS = (120, 90, 60, 30)
FORBIDDEN_SELECTOR_FIELDS = {
    "actual_bucket", "actual_c", "actual_first_peak_local", "actual_last_peak_local",
    "actual_probability", "brier", "forecast_error_c", "hit", "log_loss",
    "minutes_until_actual_first_peak", "peak_already_observed", "peak_duration_minutes",
}


def _resource(name: str) -> dict[str, Any]:
    with files("weatherman").joinpath("data", name).open(encoding="utf-8") as handle:
        return json.load(handle)


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_airport_config() -> dict[str, Any]:
    payload = _resource("opportunity_scanner_airports_v1.json")
    if set(payload["airports"]) != {"LEMD", "EHAM", "EDDM", "EPWA", "LTAC", "LTFM"}:
        raise RuntimeError("Scanner airport configuration is not the frozen six-airport set")
    if payload.get("research_only") is not True:
        raise RuntimeError("Scanner airport configuration is not research-only")
    return payload


def load_policy() -> dict[str, Any]:
    payload = _resource("opportunity_scanner_policy_v1.json")
    safe = (
        payload.get("rule_set_version") == RULE_SET_VERSION
        and payload.get("research_only") is True
        and payload.get("automatic_promotion") is False
        and payload.get("automatic_execution") is False
        and float(payload.get("nominal_stake_usd", 0)) == 1.0
    )
    if not safe:
        raise RuntimeError("Unsafe or incompatible scanner policy")
    return payload


def load_rule_pack() -> dict[str, Any]:
    raw = files("weatherman").joinpath("data", "frozen_opportunity_rule_set_v1.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != RULE_SET_FILE_SHA256:
        raise RuntimeError("Frozen opportunity rule artifact checksum mismatch")
    payload = json.loads(raw)
    rules = payload.get("rules", [])
    counts: dict[str, int] = {}
    for rule in rules:
        counts[rule["hc_class"]] = counts.get(rule["hc_class"], 0) + 1
    valid = (
        payload.get("frozen_rule_set_id") == RULE_SET_VERSION
        and payload.get("research_only") is True
        and payload.get("automatic_promotion") is False
        and len(rules) == 24
        and counts == {"HC-B": 20, "HC-A": 4}
        and canonical_hash(rules) == payload.get("canonical_rules_sha256")
    )
    if not valid:
        raise RuntimeError("Frozen opportunity rule set failed hash or invariant validation")
    return payload


def load_display_rules() -> list[dict[str, Any]]:
    payload = _resource("frozen_opportunity_display_rules_v1.json")
    rules = payload.get("rules", [])
    if len(rules) != 12 or any(rule.get("hc_class") != "HC-C" for rule in rules):
        raise RuntimeError("HC-C display pack failed invariant validation")
    return rules


def season_for(target_date: date | str) -> str:
    target = date.fromisoformat(target_date) if isinstance(target_date, str) else target_date
    return {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
            6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}[target.month]


def _parse_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _history_minute(row: Mapping[str, Any], zone: ZoneInfo) -> float | None:
    value = row.get("actual_first_peak_minute")
    if value is not None:
        return float(value)
    parsed = _parse_datetime(row.get("actual_first_peak_local"))
    if parsed is None:
        return None
    local = parsed.astimezone(zone)
    return float(local.hour * 60 + local.minute + local.second / 60)


def expected_peak_time(
    history: Iterable[Mapping[str, Any]], target_date: date | str, zone_name: str,
    *, window_valid_days: int = 60, minimum_valid_days: int = 20,
) -> dict[str, Any]:
    """Return a target-local expected peak using prior eligible days only."""
    target = date.fromisoformat(target_date) if isinstance(target_date, str) else target_date
    zone = ZoneInfo(zone_name)
    eligible: list[tuple[date, float]] = []
    for row in history:
        row_date = date.fromisoformat(str(row["target_date"])[:10])
        minute = _history_minute(row, zone)
        if row_date < target and minute is not None and row.get("eligible", True):
            eligible.append((row_date, minute))
    eligible.sort(key=lambda item: item[0])
    selected = eligible[-window_valid_days:]
    if len(selected) < minimum_valid_days:
        return {
            "available": False, "method": "rolling_median_peak_time",
            "history_days": len(selected), "minimum_history_days": minimum_valid_days,
            "expected_peak_local": None, "history_end": selected[-1][0].isoformat() if selected else None,
        }
    minute = float(statistics.median(item[1] for item in selected))
    local_midnight = datetime.combine(target, time.min, tzinfo=zone)
    expected = local_midnight + timedelta(minutes=minute)
    return {
        "available": True, "method": "rolling_median_peak_time",
        "history_days": len(selected), "minimum_history_days": minimum_valid_days,
        "history_start": selected[0][0].isoformat(), "history_end": selected[-1][0].isoformat(),
        "history_dates_sha256": canonical_hash([item[0].isoformat() for item in selected]),
        "expected_peak_minute": minute, "expected_peak_local": expected.isoformat(),
    }


def checkpoint_schedule(expected_peak_local: str | datetime) -> list[dict[str, Any]]:
    peak = _parse_datetime(expected_peak_local)
    if peak is None:
        return []
    return [{
        "checkpoint": f"W60 Peak -{offset}m",
        "minutes_before_expected_peak": offset,
        "checkpoint_local": (peak - timedelta(minutes=offset)).isoformat(),
        "checkpoint_utc": (peak - timedelta(minutes=offset)).astimezone(timezone.utc).isoformat(),
    } for offset in CHECKPOINT_OFFSETS]


def validate_causal_forecast(snapshot: Mapping[str, Any]) -> None:
    leaked = FORBIDDEN_SELECTOR_FIELDS.intersection(snapshot)
    if leaked:
        raise ValueError(f"Outcome leakage fields are forbidden in live selection: {sorted(leaked)}")
    required = {"airport", "target_date", "checkpoint", "generated_at", "modal_bucket",
                "top1_probability", "top1_top2_gap", "model_spread_c", "regimes"}
    missing = sorted(field for field in required if snapshot.get(field) is None)
    if missing:
        raise ValueError(f"Forecast snapshot is missing required fields: {missing}")


def _condition_matches(condition: Mapping[str, Any], snapshot: Mapping[str, Any]) -> bool:
    field, op, expected = condition["field"], condition["op"], condition["value"]
    actual = snapshot.get(field)
    if op == "contains":
        return expected in (actual or [])
    if actual is None:
        return False
    if op == "ge":
        return float(actual) >= float(expected)
    if op == "gt":
        return float(actual) > float(expected)
    if op == "le":
        return float(actual) <= float(expected)
    if op == "lt":
        return float(actual) < float(expected)
    if op == "eq":
        return actual == expected
    raise ValueError(f"Unsupported frozen rule operator: {op}")


def matching_rules(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    validate_causal_forecast(snapshot)
    target_season = season_for(str(snapshot["target_date"])[:10])
    matched = []
    for rule in load_rule_pack()["rules"]:
        scope = (
            rule["airport"] == snapshot["airport"]
            and rule["checkpoint"] == snapshot["checkpoint"]
            and rule["season"] in {"ALL", target_season}
        )
        if scope and all(_condition_matches(condition, snapshot) for condition in rule["selector_conditions"]):
            matched.append(rule)
    return matched


def market_freshness(snapshot_at: object, generated_at: object) -> tuple[str, float | None]:
    quote, generated = _parse_datetime(snapshot_at), _parse_datetime(generated_at)
    if quote is None or generated is None:
        return "unavailable", None
    age = max(0.0, (generated.astimezone(timezone.utc) - quote.astimezone(timezone.utc)).total_seconds() / 60)
    if age <= 15:
        return "fresh", age
    if age <= 30:
        return "acceptable", age
    if age <= 60:
        return "stale", age
    return "very_stale", age


def _event_id(prefix: str, value: Mapping[str, Any]) -> str:
    return f"{prefix}_{canonical_hash(value)[:24]}"


def evaluate_trigger(
    forecast: Mapping[str, Any], rule: Mapping[str, Any], market: Mapping[str, Any] | None,
    *, triggered_at: datetime | str | None = None, policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy = dict(policy or load_policy())
    generated = _parse_datetime(triggered_at or forecast["generated_at"])
    assert generated is not None
    warnings = sorted(set(str(item) for item in forecast.get("data_quality_warnings", [])))
    market = dict(market or {})
    matched = bool(market) and int(market.get("bucket", -9999)) == int(forecast["modal_bucket"])
    freshness, age = market_freshness(market.get("snapshot_at"), generated)
    bid = market.get("best_bid")
    ask = market.get("best_ask")
    bid = float(bid) if bid is not None else None
    ask = float(ask) if ask is not None else None
    spread = float(market.get("spread")) if market.get("spread") is not None else (
        ask - bid if ask is not None and bid is not None else None
    )
    model_p = float(forecast["top1_probability"])
    historical_p = float(rule["validation_accuracy"])
    raw_edge = model_p - ask if ask is not None else None
    historical_edge = historical_p - ask if ask is not None else None
    conservative_p = min(model_p, historical_p)
    conservative_edge = conservative_p - ask if ask is not None else None
    reason_codes: list[str] = []
    critical = bool(warnings)
    if critical:
        reason_codes.extend(warnings)
    if not matched:
        status = "WATCH"
        reason_codes.append("missing_market_match")
    elif market.get("market_status") in {"closed", "resolved"}:
        status = "NO TRADE"
        reason_codes.append("market_already_resolved")
    elif critical:
        status = "NO TRADE"
    elif ask is None:
        status = "WATCH"
        reason_codes.append("best_ask_unavailable")
    elif freshness not in {"fresh", "acceptable"}:
        status = "WATCH"
        reason_codes.append(f"market_quote_{freshness}")
    elif spread is not None and spread > float(policy["high_interest"]["maximum_spread"]):
        status = "NO TRADE"
        reason_codes.append("market_spread_too_wide")
    elif conservative_edge is None or conservative_edge <= float(policy["high_interest"]["minimum_conservative_edge"]):
        status = "NO TRADE"
        reason_codes.append("non_positive_conservative_edge")
    else:
        status = "HIGH INTEREST"
        reason_codes.append("frozen_rule_fresh_quote_positive_edge")
    identity = {
        "rule_set_version": RULE_SET_VERSION, "rule_id": rule["rule_id"],
        "airport": forecast["airport"], "target_date": str(forecast["target_date"])[:10],
        "checkpoint": forecast["checkpoint"],
    }
    return {
        "signal_id": _event_id("signal", identity), **identity,
        "triggered_at": generated.isoformat(),
        "rule_triggered": True, "hc_class": rule["hc_class"],
        "evidence_class": FORWARD_EVIDENCE_CLASS, "research_only": True,
        "automatic_promotion": False, "automatic_execution": False,
        "season": season_for(str(forecast["target_date"])[:10]),
        "bucket": int(forecast["modal_bucket"]), "forecast": dict(forecast),
        "historical_rule_evidence": {
            "discovery_n": rule["discovery_n"], "discovery_accuracy": rule["discovery_accuracy"],
            "validation_n": rule["validation_n"], "validation_accuracy": historical_p,
            "historical_status": "historical_research",
        },
        "market_match": matched, "market_freshness": freshness, "price_age_minutes": age,
        "best_bid": bid, "best_ask": ask, "spread": spread,
        "raw_model_edge": raw_edge, "historical_rule_edge": historical_edge,
        "conservative_rule_edge": conservative_edge,
        "opportunity_status": status, "opportunity_reason_codes": reason_codes,
    }


def make_market_snapshot(signal: Mapping[str, Any], market: Mapping[str, Any]) -> dict[str, Any]:
    identity = {"signal_id": signal["signal_id"], "snapshot_at": market.get("snapshot_at"),
                "market_id": market.get("market_id"), "bucket": market.get("bucket")}
    return {
        "market_snapshot_id": _event_id("market", identity), **identity,
        "airport": signal["airport"], "target_date": signal["target_date"],
        "event": market.get("event"), "best_bid": market.get("best_bid"),
        "best_ask": market.get("best_ask"), "midpoint": market.get("midpoint"),
        "spread": signal.get("spread"), "last_price": market.get("last_price"),
        "series_price": market.get("series_price"), "liquidity": market.get("liquidity"),
        "price_age_minutes": signal.get("price_age_minutes"),
        "freshness": signal.get("market_freshness"), "market_status": market.get("market_status"),
        "evidence_class": FORWARD_EVIDENCE_CLASS, "immutable": True,
    }


def make_shadow_trade(signal: Mapping[str, Any], market_snapshot_id: str) -> dict[str, Any] | None:
    if signal["opportunity_status"] != "HIGH INTEREST" or signal.get("best_ask") is None:
        return None
    identity = {"signal_id": signal["signal_id"], "entry_snapshot_id": market_snapshot_id}
    return {
        "shadow_trade_id": _event_id("trade", identity), **identity,
        "rule_set_version": RULE_SET_VERSION, "rule_id": signal["rule_id"],
        "airport": signal["airport"], "target_date": signal["target_date"],
        "checkpoint": signal["checkpoint"], "bucket": signal["bucket"],
        "generated_at": signal["triggered_at"], "entry_price": signal["best_ask"],
        "best_bid": signal["best_bid"], "best_ask": signal["best_ask"],
        "spread": signal["spread"], "model_probability": signal["forecast"]["top1_probability"],
        "historical_rule_accuracy": signal["historical_rule_evidence"]["validation_accuracy"],
        "estimated_edge": signal["conservative_rule_edge"], "stake_usd": 1.0,
        "evidence_class": FORWARD_EVIDENCE_CLASS, "real_money": False, "immutable": True,
    }


def resolve_shadow_trade(
    trade: Mapping[str, Any], official_resolved_bucket: int, settlement_source: str,
    *, resolved_at: datetime | str, metar_actual_bucket: int | None = None,
) -> dict[str, Any]:
    price = float(trade["entry_price"])
    if not 0 < price <= 1:
        raise ValueError("Shadow trade entry price must be in (0, 1]")
    won = int(official_resolved_bucket) == int(trade["bucket"])
    stake = float(trade.get("stake_usd", 1.0))
    pnl = stake * (1 / price - 1) if won else -stake
    identity = {"shadow_trade_id": trade["shadow_trade_id"], "official_resolved_bucket": int(official_resolved_bucket),
                "settlement_source": settlement_source}
    return {
        "shadow_resolution_id": _event_id("resolution", identity), **identity,
        "resolved_at": _parse_datetime(resolved_at).isoformat(), "win": won,
        "hypothetical_pnl_usd": pnl, "metar_actual_bucket": metar_actual_bucket,
        "metar_is_market_resolution": False, "evidence_class": FORWARD_EVIDENCE_CLASS,
        "immutable": True,
    }


def no_signal_record(forecast: Mapping[str, Any]) -> dict[str, Any]:
    warnings = list(forecast.get("data_quality_warnings", []))
    return {
        "airport": forecast["airport"], "target_date": str(forecast["target_date"])[:10],
        "checkpoint": forecast["checkpoint"], "rule_triggered": False, "rule_id": None,
        "hc_class": None,
        "opportunity_status": "DATA UNAVAILABLE" if warnings else "NO SIGNAL",
        "opportunity_reason_codes": warnings,
        "evidence_class": FORWARD_EVIDENCE_CLASS, "research_only": True,
    }
