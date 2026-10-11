"""Transparent research-only Shadow Engine and opportunity classification.

This module is deliberately isolated from the production forecast path.  It
translates an already-created Champion checkpoint into one of the fixed
policies selected by Historical Replay Analysis v1 and explains every delta.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .d1_evening_research import shift_bucket_distribution
from .research_tracks import probability_ranking


SHADOW_VERSION = "shadow_engine_v1.0"
OPPORTUNITY_VERSION = "trading_opportunity_v1.0"
RESEARCH_ONLY = True
AUTOMATIC_PROMOTION = False
SEASONAL_COVERAGE = "Apr–Oct 2026 only"

D1 = "D-1 Evening @20:00"
D0 = "D0 Morning @09:00"
FIRST = "First Live @12:00"
LATE = "Late Live @16:00"

POLICIES = {
    D1: "current_protected_forecast",
    D0: "anchor_25pct",
    FIRST: "current_protected_forecast",
    LATE: "late_dry_mixing_0pct",
}

# Frozen results supplied by Historical Replay Analysis v1.  Missing metrics
# stay explicit; they are never silently filled or presented as OOS evidence.
FORECAST_EVIDENCE = {
    D0: {
        "policy": "anchor_25pct",
        "champion_modal_accuracy": 0.4754,
        "shadow_modal_accuracy": 0.5137,
        "champion_mae_c": 0.660,
        "shadow_mae_c": 0.637,
        "robustness": "positive in 6/6 sensitivity scenarios",
    },
    LATE: {
        "policy": "late_dry_mixing_0pct",
        "champion_modal_accuracy": 0.5924,
        "shadow_modal_accuracy": 0.6196,
        "champion_mae_c": 0.550,
        "shadow_mae_c": 0.533,
        "walk_forward_modal_accuracy": 0.6141,
        "walk_forward_lift_pp": 2.17,
        "robustness": "historical diagnostic plus walk-forward selector",
    },
}

REGIME_EVIDENCE = {
    D1: {
        "Stable / High Confidence": (76, 0.3628, "positive historical research"),
        "Persistent Hot": (62, 0.3385, "positive historical research"),
        "Rapid Heat Ramp": (60, 0.2182, "positive historical research"),
    },
    D0: {
        "Rapid Heat Ramp": (60, 0.5274, "positive historical research"),
        "Stable / High Confidence": (61, 0.4394, "positive historical research"),
        "Continued Dry Heating": (63, 0.0602, "positive historical research"),
    },
    FIRST: {
        "Persistent Hot": (None, None, "positive qualitative historical result"),
        "Phase Anchor": (None, None, "positive qualitative historical result"),
        "Negative Temperature Anchor": (None, None, "weak/negative historical result"),
        "Bucket Boundary": (None, None, "weak/negative historical result"),
    },
    LATE: {
        "Bucket Boundary": (None, None, "positive qualitative historical result"),
        "Persistent Hot": (None, None, "positive qualitative historical result"),
        "Model Spread low": (None, None, "positive qualitative historical result"),
    },
}


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _corrections(record: Mapping[str, Any]) -> dict[str, float]:
    source = dict(record.get("correction_contributions") or {})
    aliases = {
        "Base bias": ("base_bias_c", "bias_adjustment_c"),
        "Temperature anchor": ("temp_anchor_adjustment_c", "temperature_anchor"),
        "Clear sky": ("clear_sky_override_adjustment_c", "clear_sky_override"),
        "Late dry mixing": ("late_dry_mixing_adjustment_c", "late_dry_mixing"),
        "TAF": ("taf_adjustment_c", "taf"),
        "Cloud": ("cloud_adjustment_c", "cloud"),
        "Radiation": ("radiation_adjustment_c", "radiation"),
        "Wind": ("wind_adjustment_c", "wind"),
        "Heating rate": ("heating_rate_adjustment_c", "heating_rate"),
    }
    result: dict[str, float] = {}
    for label, keys in aliases.items():
        result[label] = next(
            (_number(source[key]) for key in keys if source.get(key) is not None), 0.0
        )
    return result


def _market_by_bucket(market: Mapping[str, Any] | None) -> dict[int, Mapping[str, Any]]:
    result: dict[int, Mapping[str, Any]] = {}
    for row in (market or {}).get("markets", []):
        bucket = row.get("bucket_c")
        if bucket is not None:
            result[int(bucket)] = row
    return result


def _historical_evidence(checkpoint: str, regimes: Sequence[str]) -> list[dict[str, Any]]:
    catalog = REGIME_EVIDENCE.get(checkpoint, {})
    return [
        {
            "regime": regime,
            "historical_n": catalog[regime][0],
            "historical_roi": catalog[regime][1],
            "robustness_classification": catalog[regime][2],
            "evidence_class": "historical research evidence",
            "seasonal_coverage": SEASONAL_COVERAGE,
            "historical_max_drawdown": None,
        }
        for regime in regimes if regime in catalog
    ]


def _opportunity(
    *, checkpoint: str, ranked: list[tuple[int, float]], regimes: Sequence[str],
    spread_c: float | None, taf_agreement: str | None,
    market: Mapping[str, Any] | None, evidence_fidelity: str,
) -> dict[str, Any]:
    evidence = _historical_evidence(checkpoint, regimes)
    top1 = ranked[0] if ranked else (None, None)
    top2 = ranked[1] if len(ranked) > 1 else (None, None)
    gap_pp = (
        (top1[1] - top2[1]) * 100
        if top1[1] is not None and top2[1] is not None else None
    )
    market_row = _market_by_bucket(market).get(top1[0]) if top1[0] is not None else None
    price = None
    if market_row:
        price = market_row.get("best_ask")
        if price is None:
            price = market_row.get("yes_price")
    age = (market or {}).get("market_snapshot_age_minutes")
    raw_edge = top1[1] - _number(price) if top1[1] is not None and price is not None else None
    reasons: list[str] = []
    blockers: list[str] = []
    negative = any("weak/negative" in item["robustness_classification"] for item in evidence)
    quantitative = [
        item for item in evidence
        if item["historical_n"] is not None and item["historical_roi"] is not None
    ]
    forecast_evidence = FORECAST_EVIDENCE.get(checkpoint)
    forecast_gate_passed = (
        forecast_evidence is None
        or forecast_evidence["shadow_modal_accuracy"]
        >= forecast_evidence["champion_modal_accuracy"]
    )
    if evidence_fidelity not in {"scheduled_causal", "live_shadow", "live_preview"}:
        blockers.append("unsupported_evidence_fidelity")
    if not market_row or price is None:
        blockers.append("market_price_unavailable")
    if age is None or _number(age, 9999) > 30:
        blockers.append("market_price_stale_or_unknown")
    if top1[1] is None or top1[1] < 0.30:
        blockers.append("top1_probability_too_low")
    if gap_pp is None or gap_pp < 5:
        blockers.append("small_top1_top2_gap")
    if spread_c is not None and spread_c > 2.0:
        blockers.append("high_model_spread")
    if negative:
        blockers.append("negative_historical_regime")
    if raw_edge is not None and raw_edge <= 0:
        blockers.append("no_positive_raw_edge")
    if not forecast_gate_passed:
        blockers.append("shadow_forecast_historically_weaker")

    high_candidate = (
        not blockers
        and age is not None and _number(age) <= 10
        and gap_pp is not None and gap_pp >= 8
        and top1[1] is not None and top1[1] >= 0.35
        and raw_edge is not None and raw_edge >= 0.05
        and any(
            item["historical_n"] >= 30 and item["historical_roi"] >= 0.20
            for item in quantitative
        )
    )
    # Historical drawdown was requested as a gate, but no regime-level values were
    # supplied in Replay v1.  Missing drawdown caps an otherwise strong setup at WATCH.
    drawdown_known = any(item["historical_max_drawdown"] is not None for item in evidence)
    if high_candidate and drawdown_known:
        status = "HIGH INTEREST"
        reasons.append("all_high_interest_gates_passed")
    elif high_candidate:
        status = "WATCH"
        reasons.append("high_interest_candidate_but_drawdown_unavailable")
    elif blockers and any(item in blockers for item in (
        "negative_historical_regime", "no_positive_raw_edge",
        "top1_probability_too_low", "small_top1_top2_gap", "high_model_spread",
        "shadow_forecast_historically_weaker",
    )):
        status = "NO TRADE"
    elif market_row and evidence:
        status = "WATCH"
        reasons.append("partial_but_not_high_interest_evidence")
    else:
        status = "NO SIGNAL"

    return {
        "version": OPPORTUNITY_VERSION,
        "classification": status,
        "top1_top2_gap_pp": gap_pp,
        "model_spread_c": spread_c,
        "taf_signal": taf_agreement or "unavailable",
        "market_price": price,
        "market_price_kind": (
            "best_ask" if market_row and market_row.get("best_ask") is not None
            else "series_price" if price is not None else None
        ),
        "market_snapshot_age_minutes": age,
        "market_freshness": (
            "fresh" if age is not None and _number(age) <= 10
            else "aging" if age is not None and _number(age) <= 30
            else "stale_or_unavailable"
        ),
        "best_bid": market_row.get("best_bid") if market_row else None,
        "best_ask": market_row.get("best_ask") if market_row else None,
        "spread": market_row.get("spread") if market_row else None,
        "raw_model_market_edge": raw_edge,
        "calibrated_research_confidence": "not calibrated for live execution",
        "historical_edge_band_performance": "not monotonic in Historical Replay v1",
        "historical_evidence_used": evidence,
        "historical_forecast_evidence": forecast_evidence,
        "historical_forecast_accuracy_gate": forecast_gate_passed,
        "reason_codes": reasons,
        "blockers": blockers,
        "drawdown_gate": "available" if drawdown_known else "unavailable_caps_at_watch",
        "research_only": True,
        "automatic_execution": False,
    }


def build_shadow_mirror(
    record: Mapping[str, Any], *, market: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build the fixed v1 mirror from one already-persisted Champion record."""
    checkpoint = str(record.get("checkpoint_label") or record.get("checkpoint") or "")
    policy = POLICIES.get(checkpoint, "current_protected_forecast")
    champion_center = _number(record.get("champion_center_c"))
    production_corrections = _corrections(record)
    shadow_corrections = dict(production_corrections)
    if policy == "anchor_25pct":
        shadow_corrections["Temperature anchor"] *= 0.25
    elif policy == "late_dry_mixing_0pct":
        shadow_corrections["Late dry mixing"] = 0.0
    correction_rows = [
        {
            "correction": name,
            "production_c": value,
            "shadow_c": shadow_corrections[name],
            "delta_c": shadow_corrections[name] - value,
        }
        for name, value in production_corrections.items()
    ]
    delta = sum(row["delta_c"] for row in correction_rows)
    probabilities = record.get("champion_probabilities") or {}
    shadow_probabilities = (
        dict(probabilities) if abs(delta) < 1e-12
        else shift_bucket_distribution(probabilities, delta)
    )
    production_ranked = probability_ranking(probabilities)
    shadow_ranked = probability_ranking(shadow_probabilities)
    regimes = list(dict.fromkeys(record.get("active_regimes") or []))
    opportunity = _opportunity(
        checkpoint=checkpoint,
        ranked=shadow_ranked,
        regimes=regimes,
        spread_c=record.get("model_spread_c") or record.get("raw_spread_c"),
        taf_agreement=record.get("taf_agreement"),
        market=market,
        evidence_fidelity=str(record.get("evidence_class") or "unknown"),
    )
    return {
        "shadow_version": SHADOW_VERSION,
        "checkpoint_label": checkpoint,
        "selected_policy": policy,
        "production_center_c": champion_center,
        "shadow_center_c": champion_center + delta,
        "center_delta_c": delta,
        "production_probabilities": probabilities,
        "shadow_probabilities": shadow_probabilities,
        "production_ranking": production_ranked[:3],
        "shadow_ranking": shadow_ranked[:3],
        "production_modal_bucket": production_ranked[0][0] if production_ranked else None,
        "shadow_modal_bucket": shadow_ranked[0][0] if shadow_ranked else None,
        "modal_delta_buckets": (
            shadow_ranked[0][0] - production_ranked[0][0]
            if production_ranked and shadow_ranked else None
        ),
        "correction_comparison": correction_rows,
        "active_regimes": regimes,
        "forecast_historical_evidence": FORECAST_EVIDENCE.get(checkpoint),
        "trading_opportunity": opportunity,
        "model_spread_c": record.get("model_spread_c") or record.get("raw_spread_c"),
        "taf_bucket": record.get("taf_bucket"),
        "taf_agreement": record.get("taf_agreement"),
        "confidence": record.get("forecast_confidence"),
        "seasonal_coverage": SEASONAL_COVERAGE,
        "research_only": RESEARCH_ONLY,
        "automatic_promotion": AUTOMATIC_PROMOTION,
        "automatic_execution": False,
    }
