"""Research-only seasonal Shadow Engine and trading opportunity desk v1.1.

The module consumes an already-created Production Champion checkpoint. It can
only select among explicit, frozen research policies and never mutates the
protected forecast path or executes a trade.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from functools import lru_cache
from importlib.resources import files
from typing import Any, Mapping, Sequence

from .d1_evening_research import shift_bucket_distribution
from .research_tracks import probability_ranking


SHADOW_VERSION = "shadow_engine_v1.1"
OPPORTUNITY_VERSION = "trading_opportunity_v1.1"
RESEARCH_ONLY = True
AUTOMATIC_PROMOTION = False
AUTOMATIC_EXECUTION = False
EVIDENCE_SOURCE = "Deep Seasonal Replay v1"
FIDELITY_CLASS = "deep_seasonal_reduced_fidelity"
FIDELITY_WARNING = "directional seasonal evidence; not Production replication"

D1 = "D-1 Evening @20:00"
D0 = "D0 Morning @09:00"
FIRST = "First Live @12:00"
LATE = "Late Live @16:00"

SEASONS = {
    12: "Winter", 1: "Winter", 2: "Winter",
    3: "Spring", 4: "Spring", 5: "Spring",
    6: "Summer", 7: "Summer", 8: "Summer",
    9: "Autumn", 10: "Autumn", 11: "Autumn",
}

PRIMARY_POLICY = {
    D1: {season: "current_protected_forecast" for season in set(SEASONS.values())},
    D0: {
        "Winter": "anchor_50pct",
        "Spring": "current_protected_forecast",
        "Summer": "anchor_25pct",
        "Autumn": "anchor_25pct",
    },
    FIRST: {season: "current_protected_forecast" for season in set(SEASONS.values())},
    LATE: {season: "late_dry_mixing_25pct" for season in set(SEASONS.values())},
}

CANDIDATE_POLICIES = {
    D1: ("current_protected_forecast",),
    D0: (
        "current_protected_forecast", "anchor_disabled", "anchor_25pct",
        "anchor_50pct", "anchor_75pct",
    ),
    FIRST: ("current_protected_forecast",),
    LATE: (
        "current_protected_forecast", "late_dry_mixing_0pct",
        "late_dry_mixing_25pct", "late_dry_mixing_50pct",
        "late_dry_mixing_75pct",
    ),
}

POLICY_MULTIPLIER = {
    "anchor_disabled": ("Temperature anchor", 0.0),
    "anchor_25pct": ("Temperature anchor", 0.25),
    "anchor_50pct": ("Temperature anchor", 0.50),
    "anchor_75pct": ("Temperature anchor", 0.75),
    "late_dry_mixing_0pct": ("Late dry mixing", 0.0),
    "late_dry_mixing_25pct": ("Late dry mixing", 0.25),
    "late_dry_mixing_50pct": ("Late dry mixing", 0.50),
    "late_dry_mixing_75pct": ("Late dry mixing", 0.75),
}


@lru_cache(maxsize=1)
def _evidence() -> dict[str, Any]:
    path = files("weatherman").joinpath("data/seasonal_shadow_evidence_v1.json")
    return json.loads(path.read_text(encoding="utf-8"))


def season_for_target_date(value: object) -> str:
    """Return meteorological season from target date, never run date."""
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        parsed = date.fromisoformat(str(value)[:10])
    return SEASONS[parsed.month]


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
    return {
        label: next((_number(source[key]) for key in keys if source.get(key) is not None), 0.0)
        for label, keys in aliases.items()
    }


def _policy_evidence(checkpoint: str, season: str, policy: str) -> dict[str, Any]:
    evidence_policy = "current" if policy == "current_protected_forecast" else policy
    row = next((item for item in _evidence().get("policy_evidence", []) if
                item.get("checkpoint") == checkpoint and item.get("season") == season
                and item.get("policy_id") == evidence_policy), {})
    return {
        "source": EVIDENCE_SOURCE,
        "fidelity_class": FIDELITY_CLASS,
        "fidelity_warning": FIDELITY_WARNING,
        "season": season,
        "checkpoint": checkpoint,
        "policy": policy,
        "n": row.get("n"),
        "accuracy_delta_pp": row.get("accuracy_delta_pp"),
        "mae_delta_c": row.get("mae_delta_c"),
        "quality": "directional" if row else "insufficient",
    }


def _regime_evidence(checkpoint: str, season: str, regimes: Sequence[str]) -> list[dict[str, Any]]:
    data = _evidence()
    result = []
    for regime in regimes:
        cell = next((item for item in data.get("regime_evidence", []) if
                     item.get("checkpoint") == checkpoint and item.get("season") == season
                     and item.get("regime") == regime), {})
        classification_row = next((item for item in data.get("regime_classifications", []) if
                                   item.get("checkpoint") == checkpoint
                                   and item.get("regime") == regime), {})
        classification = classification_row.get("classification")
        trading = next((item for item in data.get("regime_trading_evidence", []) if
                        item.get("checkpoint") == checkpoint and item.get("season") == season
                        and item.get("regime") == regime), {})
        if cell or classification or trading:
            result.append({
                "checkpoint": checkpoint,
                "season": season,
                "regime": regime,
                "n": cell.get("n"),
                "accuracy_lift_pp": cell.get("lift_pp"),
                "classification": classification or "insufficient seasonal evidence",
                "trading_n": trading.get("n"),
                "trading_roi": trading.get("roi"),
                "trading_status": trading.get("status", "unavailable"),
                "fidelity_class": FIDELITY_CLASS,
            })
    return result


def _checkpoint_trading_evidence(checkpoint: str, season: str) -> dict[str, Any]:
    row = next((item for item in _evidence().get("trading_evidence", []) if
                item.get("checkpoint") == checkpoint and item.get("season") == season), {})
    return {
        "checkpoint": checkpoint,
        "season": season,
        "n": row.get("n", 0),
        "roi": row.get("roi"),
        "status": row.get("status", "unavailable"),
        "same_season": True,
        "fidelity_class": FIDELITY_CLASS,
    }


def _candidate(
    policy: str, champion_center: float, probabilities: Mapping[str, Any],
    production_corrections: Mapping[str, float], checkpoint: str, season: str,
) -> dict[str, Any]:
    shadow_corrections = dict(production_corrections)
    if policy in POLICY_MULTIPLIER:
        correction, multiplier = POLICY_MULTIPLIER[policy]
        shadow_corrections[correction] *= multiplier
    correction_rows = [{
        "correction": name,
        "production_c": value,
        "shadow_c": shadow_corrections[name],
        "delta_c": shadow_corrections[name] - value,
    } for name, value in production_corrections.items()]
    delta = sum(row["delta_c"] for row in correction_rows)
    shadow_probabilities = (
        dict(probabilities) if abs(delta) < 1e-12
        else shift_bucket_distribution(probabilities, delta)
    )
    ranked = probability_ranking(shadow_probabilities)
    return {
        "policy": policy,
        "forecast_center_c": champion_center + delta,
        "center_delta_c": delta,
        "modal_bucket": ranked[0][0] if ranked else None,
        "ranking": ranked[:3],
        "probabilities": shadow_probabilities,
        "correction_comparison": correction_rows,
        "seasonal_policy_evidence": _policy_evidence(checkpoint, season, policy),
    }


def _market_by_bucket(market: Mapping[str, Any] | None) -> dict[int, Mapping[str, Any]]:
    return {
        int(row["bucket_c"]): row for row in (market or {}).get("markets", [])
        if row.get("bucket_c") is not None
    }


def _opportunity(
    *, checkpoint: str, season: str, candidate: Mapping[str, Any],
    regimes: Sequence[str], regime_evidence: Sequence[Mapping[str, Any]],
    spread_c: float | None, taf_agreement: str | None,
    market: Mapping[str, Any] | None, evidence_fidelity: str,
    sequential_trade_n: int = 0,
) -> dict[str, Any]:
    ranked = list(candidate.get("ranking") or [])
    top1 = ranked[0] if ranked else (None, None)
    top2 = ranked[1] if len(ranked) > 1 else (None, None)
    gap_pp = ((top1[1] - top2[1]) * 100 if top1[1] is not None and top2[1] is not None else None)
    market_row = _market_by_bucket(market).get(top1[0]) if top1[0] is not None else None
    price = None
    if market_row:
        price = market_row.get("best_ask")
        if price is None:
            price = market_row.get("yes_price")
    age = (market or {}).get("market_snapshot_age_minutes")
    raw_edge = top1[1] - _number(price) if top1[1] is not None and price is not None else None
    policy_evidence = dict(candidate.get("seasonal_policy_evidence") or {})
    trading = _checkpoint_trading_evidence(checkpoint, season)
    warnings: list[str] = []
    reasons: list[str] = []
    labels = ["same-season evidence", "reduced-fidelity seasonal evidence"]
    if sequential_trade_n:
        labels.append("sequential OOS evidence")
    if not policy_evidence.get("n"):
        labels.append("insufficient seasonal evidence")
    negative_regime = any(
        row.get("classification") == "robust_negative"
        or (_number(row.get("accuracy_lift_pp")) < -2 and _number(row.get("n")) >= 20)
        for row in regime_evidence
    )
    if negative_regime:
        warnings.append("negative_season_checkpoint_regime")
    if checkpoint == D0 and "Temperature Plateau" in regimes:
        warnings.append("d0_temperature_plateau_robust_negative")
    if evidence_fidelity not in {"scheduled_causal", "live_shadow", "live_preview"}:
        warnings.append("unsupported_current_evidence_fidelity")
    if not market_row or price is None:
        warnings.append("market_price_unavailable")
    if age is None or _number(age, 9999) > 30:
        warnings.append("market_price_stale_or_unknown")
    if top1[1] is None or top1[1] < 0.30:
        warnings.append("top1_probability_too_low")
    if gap_pp is None or gap_pp < 5:
        warnings.append("small_top1_top2_gap")
    if spread_c is not None and spread_c > 2.0:
        warnings.append("high_model_spread")
    if raw_edge is not None and raw_edge <= 0:
        warnings.append("no_positive_raw_edge")
    if policy_evidence.get("accuracy_delta_pp") is not None and _number(policy_evidence["accuracy_delta_pp"]) < 0:
        warnings.append("negative_seasonal_policy_evidence")

    trading_available = (
        trading.get("status") == "available" and _number(trading.get("n")) >= 30
        and trading.get("roi") is not None
    )
    winter_cap = season == "Winter" and sequential_trade_n < 20
    if winter_cap:
        reasons.append("winter_trading_roi_unavailable_caps_at_watch")
    if not trading_available:
        reasons.append("same_season_trading_evidence_insufficient")
    hard_warnings = {
        "d0_temperature_plateau_robust_negative", "negative_season_checkpoint_regime",
        "market_price_stale_or_unknown", "top1_probability_too_low",
        "small_top1_top2_gap", "high_model_spread", "no_positive_raw_edge",
        "negative_seasonal_policy_evidence",
    }
    high = (
        not any(item in hard_warnings for item in warnings)
        and not winter_cap and trading_available
        and age is not None and _number(age) <= 10
        and top1[1] is not None and top1[1] >= 0.35
        and gap_pp is not None and gap_pp >= 8
        and raw_edge is not None and raw_edge >= 0.05
        and _number(policy_evidence.get("n")) >= 30
        and _number(policy_evidence.get("accuracy_delta_pp")) >= 0
        and _number(trading.get("roi"), -1) > 0
    )
    if high:
        classification = "HIGH INTEREST"
        reasons.append("all_seasonal_high_interest_gates_passed")
    elif any(item in hard_warnings for item in warnings):
        classification = "NO TRADE"
        reasons.append("strong_negative_research_signal")
    elif market_row is None:
        classification = "NO SIGNAL"
        reasons.append("no_live_market_signal")
    else:
        classification = "WATCH"
        reasons.append("forecast_interest_without_complete_trading_evidence")
    return {
        "version": OPPORTUNITY_VERSION,
        "classification": classification,
        "season": season,
        "top1_top2_gap_pp": gap_pp,
        "model_spread_c": spread_c,
        "taf_signal": taf_agreement or "unavailable",
        "market_price": price,
        "market_price_kind": "best_ask" if market_row and market_row.get("best_ask") is not None else "series_price" if price is not None else None,
        "market_snapshot_age_minutes": age,
        "market_freshness": "fresh" if age is not None and _number(age) <= 10 else "aging" if age is not None and _number(age) <= 30 else "stale_or_unavailable",
        "best_bid": market_row.get("best_bid") if market_row else None,
        "best_ask": market_row.get("best_ask") if market_row else None,
        "spread": market_row.get("spread") if market_row else None,
        "raw_model_market_edge": raw_edge,
        "seasonal_trading_evidence": trading,
        "seasonal_forecast_evidence": policy_evidence,
        "evidence_labels": list(dict.fromkeys(labels)),
        "reason_codes": reasons,
        "warning_codes": warnings,
        "winter_watch_cap": winter_cap,
        "research_only": True,
        "automatic_execution": False,
    }


def build_shadow_mirror(
    record: Mapping[str, Any], *, market: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build a seasonal mirror from one already-persisted Champion record."""
    checkpoint = str(record.get("checkpoint_label") or record.get("checkpoint") or "")
    season = season_for_target_date(record["target_date"])
    selected_policy = PRIMARY_POLICY.get(checkpoint, {}).get(season, "current_protected_forecast")
    champion_center = _number(record.get("champion_center_c"))
    probabilities = dict(record.get("champion_probabilities") or {})
    production_corrections = _corrections(record)
    variants = [
        _candidate(policy, champion_center, probabilities, production_corrections, checkpoint, season)
        for policy in CANDIDATE_POLICIES.get(checkpoint, ("current_protected_forecast",))
    ]
    selected = next(item for item in variants if item["policy"] == selected_policy)
    production_ranked = probability_ranking(probabilities)
    regimes = list(dict.fromkeys(record.get("active_regimes") or []))
    regime_evidence = _regime_evidence(checkpoint, season, regimes)
    policy_evidence = selected["seasonal_policy_evidence"]
    opportunity = _opportunity(
        checkpoint=checkpoint, season=season, candidate=selected, regimes=regimes,
        regime_evidence=regime_evidence,
        spread_c=record.get("model_spread_c") or record.get("raw_spread_c"),
        taf_agreement=record.get("taf_agreement"), market=market,
        evidence_fidelity=str(record.get("evidence_class") or "unknown"),
        sequential_trade_n=int(record.get("sequential_seasonal_trade_n") or 0),
    )
    delta_pp = policy_evidence.get("accuracy_delta_pp")
    selection_reason = (
        f"{season} research candidate: {selected_policy}; historical seasonal delta: "
        f"{delta_pp:+.2f} pp; evidence: {FIDELITY_CLASS}; not certified OOS"
        if delta_pp is not None else
        f"{season} mirror policy: {selected_policy}; insufficient seasonal policy evidence; not certified OOS"
    )
    return {
        "shadow_version": SHADOW_VERSION,
        "checkpoint_label": checkpoint,
        "season": season,
        "selected_policy": selected_policy,
        "selected_shadow_policy": selected_policy,
        "selection_reason": selection_reason,
        "candidate_policies": [item["policy"] for item in variants],
        "candidate_variants": variants,
        "production_center_c": champion_center,
        "shadow_center_c": selected["forecast_center_c"],
        "center_delta_c": selected["center_delta_c"],
        "production_probabilities": probabilities,
        "shadow_probabilities": selected["probabilities"],
        "production_ranking": production_ranked[:3],
        "shadow_ranking": selected["ranking"],
        "production_modal_bucket": production_ranked[0][0] if production_ranked else None,
        "shadow_modal_bucket": selected["modal_bucket"],
        "modal_delta_buckets": selected["modal_bucket"] - production_ranked[0][0] if production_ranked and selected["modal_bucket"] is not None else None,
        "correction_comparison": selected["correction_comparison"],
        "active_regimes": regimes,
        "seasonal_policy_evidence": policy_evidence,
        "seasonal_regime_evidence": regime_evidence,
        "historical_evidence_source": EVIDENCE_SOURCE,
        "fidelity_class": FIDELITY_CLASS,
        "fidelity_warning": FIDELITY_WARNING,
        "trading_opportunity": opportunity,
        "model_spread_c": record.get("model_spread_c") or record.get("raw_spread_c"),
        "taf_bucket": record.get("taf_bucket"),
        "taf_agreement": record.get("taf_agreement"),
        "confidence": record.get("forecast_confidence"),
        "research_only": RESEARCH_ONLY,
        "automatic_promotion": AUTOMATIC_PROMOTION,
        "automatic_execution": AUTOMATIC_EXECUTION,
    }
