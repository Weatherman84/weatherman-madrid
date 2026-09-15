"""Versioned, additive research logic for v1.0.11.

Nothing in this module is imported by the forecast engine or collector.  It
operates on already persisted checkpoint evidence and never promotes itself.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd


TRADING_CHALLENGER_VERSION = "trading_challenger_v0.1"
REGIME_MATRIX_VERSION = "regime_research_matrix_v0.1"
RESEARCH_ONLY = True
AUTOMATIC_PROMOTION = False
TAF_HEDGE_WEIGHT = 0.30
MIN_COMBINATION_SAMPLE = 10

ACTIONABLE_CHECKPOINTS = {"First Live @12:00", "Late Live @16:00"}


def positive_temperature_bucket(value: object) -> int | None:
    """Round positive Celsius values half-up, never with bankers rounding."""
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed) or float(parsed) < 0:
        return None
    return math.floor(float(parsed) + 0.5)


def probability_ranking(value: object) -> list[tuple[int, float]]:
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, Mapping):
        return []
    result: list[tuple[int, float]] = []
    for bucket, probability in payload.items():
        parsed_bucket = pd.to_numeric(bucket, errors="coerce")
        parsed_probability = pd.to_numeric(probability, errors="coerce")
        if pd.notna(parsed_bucket) and pd.notna(parsed_probability):
            result.append((int(parsed_bucket), float(parsed_probability)))
    return sorted(result, key=lambda item: (-item[1], item[0]))


def market_freshness(age_minutes: object) -> str:
    age = pd.to_numeric(age_minutes, errors="coerce")
    if pd.isna(age):
        return "unavailable"
    if float(age) <= 60:
        return "le_60m"
    if float(age) <= 180:
        return "le_180m"
    if float(age) <= 360:
        return "le_360m"
    return "gt_360m_sensitivity_only"


def build_trading_shadow_decision(
    *,
    target_date: str,
    checkpoint: str,
    generated_at: str,
    probabilities: object,
    taf_bucket: int | None,
    market_checkpoint: Mapping[str, Any] | None,
    forecast_confidence: object = None,
    regimes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build the immutable v0.1 hypothetical decision from checkpoint inputs."""
    ranked = probability_ranking(probabilities)
    top1 = ranked[0] if ranked else (None, None)
    top2 = ranked[1] if len(ranked) > 1 else (None, None)
    top_buckets = {bucket for bucket in (top1[0], top2[0]) if bucket is not None}
    taf_outside = taf_bucket is not None and taf_bucket not in top_buckets
    status = "actionable_shadow" if checkpoint in ACTIONABLE_CHECKPOINTS else "observe_only"
    reason = "observe_only_no_fixed_rule"
    weights: dict[int, float] = {}

    if status == "actionable_shadow" and top1[0] is not None:
        champion_total = float(top1[1] or 0) + float(top2[1] or 0)
        hedge = checkpoint == "First Live @12:00" and taf_outside
        champion_weight = 1.0 - TAF_HEDGE_WEIGHT if hedge else 1.0
        if champion_total > 0:
            weights[int(top1[0])] = champion_weight * float(top1[1]) / champion_total
            if top2[0] is not None:
                weights[int(top2[0])] = champion_weight * float(top2[1]) / champion_total
        if hedge and taf_bucket is not None:
            weights[int(taf_bucket)] = TAF_HEDGE_WEIGHT
            reason = "first_live_top2_plus_external_taf_hedge_30pct"
        elif checkpoint == "First Live @12:00":
            reason = "first_live_champion_top2"
        else:
            reason = "late_live_champion_top2_no_taf_hedge"

    market_checkpoint = market_checkpoint or {}
    markets = {
        row.get("bucket_c"): row
        for row in market_checkpoint.get("markets", [])
        if row.get("bucket_c") is not None
    }
    selected = []
    for bucket, weight in sorted(weights.items()):
        quote = markets.get(bucket, {})
        champion_probability = next(
            (probability for candidate, probability in ranked if candidate == bucket), None
        )
        ask = pd.to_numeric(quote.get("best_ask"), errors="coerce")
        edge = (
            float(champion_probability) - float(ask)
            if champion_probability is not None and pd.notna(ask)
            else None
        )
        selected.append(
            {
                "bucket_c": bucket,
                "weight": round(float(weight), 8),
                "champion_probability": champion_probability,
                "best_bid": quote.get("best_bid"),
                "best_ask": quote.get("best_ask"),
                "spread": quote.get("spread"),
                "estimated_edge": round(edge, 8) if edge is not None else None,
            }
        )

    age = market_checkpoint.get("market_snapshot_age_minutes")
    return {
        "target_date": target_date,
        "checkpoint": checkpoint,
        "generated_at": generated_at,
        "challenger_version": TRADING_CHALLENGER_VERSION,
        "evidence_class": "historical_replay",
        "champion_top1_bucket": top1[0],
        "champion_top1_probability": top1[1],
        "champion_top2_bucket": top2[0],
        "champion_top2_probability": top2[1],
        "taf_bucket": taf_bucket,
        "taf_outside_top2": taf_outside,
        "status": status if ranked else "unavailable",
        "selected_buckets": selected,
        "market_snapshot_at": market_checkpoint.get("market_captured_at"),
        "market_snapshot_status": market_checkpoint.get("status", "unavailable"),
        "market_snapshot_age_minutes": age,
        "market_freshness_band": market_freshness(age),
        "forecast_confidence": _number(forecast_confidence),
        "regimes": list(regimes or []),
        "strategy_code": reason if ranked else "missing_champion_distribution",
        "research_only": RESEARCH_ONLY,
        "automatic_promotion": AUTOMATIC_PROMOTION,
    }


def _feature_dict(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _number(value: object) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(parsed) else float(parsed)


def active_regimes(record: Mapping[str, Any]) -> list[str]:
    """Apply explicit v0.1 research labels to persisted scalar evidence."""
    features = _feature_dict(record.get("features_json"))
    spread = _number(record.get("raw_spread_c"))
    center = _number(record.get("champion_center_c"))
    top_gap = _number(record.get("top1_top2_gap_pp"))
    heating = _number(features.get("observed_heating_rate_60m_cph"))
    dryness = _number(features.get("observed_dryness_c"))
    regimes: list[str] = []

    if spread is not None and spread <= 1.0 and (top_gap or 0) >= 15:
        regimes.append("Stable / High Confidence")
    if spread is not None and spread >= 2.0:
        regimes.extend(["Model Split", "Model Spread high"])
    elif spread is not None and spread <= 1.0:
        regimes.append("Model Spread low")
    if bool(record.get("rapid_heat_ramp_active")):
        regimes.append("Rapid Heat Ramp")
    if heating is not None and abs(heating) <= 0.2:
        regimes.append("Temperature Plateau")
    if heating is not None and heating > 0.2 and dryness is not None and dryness >= 10:
        regimes.append("Continued Dry Heating")
    if (_number(record.get("cloud_adjustment_c")) or 0) < -0.05 or (
        _number(record.get("radiation_adjustment_c")) or 0
    ) < -0.05:
        regimes.append("Cloud / Radiation Suppression")
    if abs(_number(record.get("wind_adjustment_c")) or 0) >= 0.05:
        regimes.append("Front / Wind Shift Timing")
    if (_number(record.get("temp_anchor_adjustment_c")) or 0) < -0.05:
        regimes.append("Negative Temperature Anchor")

    taf_bucket = record.get("taf_bucket")
    top = {record.get("modal_bucket"), record.get("top2_bucket")}
    if taf_bucket is not None:
        regimes.append("TAF Agreement" if taf_bucket in top else "TAF Disagreement")
    if center is not None and abs(abs(center - math.floor(center)) - 0.5) <= 0.15:
        regimes.append("Bucket Boundary")

    scalar_flags = {
        "clear_sky_override_adjustment_c": "Clear Sky Override",
        "late_dry_mixing_adjustment_c": "Late Dry Mixing",
        "failed_convection_adjustment_c": "Failed Convection",
        "regional_cluster_active": "Regional Cluster",
        "persistent_hot_active": "Persistent Hot",
        "phase_vs_amplitude_active": "Phase Anchor",
        "maritime_advection_active": "Maritime Advection",
    }
    for field, label in scalar_flags.items():
        value = record.get(field)
        if field.endswith("_c"):
            enabled = abs(_number(value) or 0) >= 0.05
        else:
            enabled = bool(value)
        if enabled:
            regimes.append(label)
    return list(dict.fromkeys(regimes))


def score_regime_matrix(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Summarise global and checkpoint metrics; no small-N combinations are emitted."""
    expanded: list[dict[str, Any]] = []
    for source in records:
        actual = source.get("stored_metar_actual_c")
        actual_bucket = positive_temperature_bucket(actual)
        if actual_bucket is None:
            continue
        row = dict(source)
        row["actual_bucket"] = actual_bucket
        for regime in source.get("active_regimes", []):
            for checkpoint in ("ALL", str(source.get("checkpoint"))):
                expanded.append({**row, "matrix_regime": regime, "matrix_checkpoint": checkpoint})
    if not expanded:
        return []
    frame = pd.DataFrame(expanded)
    result: list[dict[str, Any]] = []
    for (regime, checkpoint), group in frame.groupby(["matrix_regime", "matrix_checkpoint"]):
        centers = pd.to_numeric(group.champion_center_c, errors="coerce")
        actuals = pd.to_numeric(group.stored_metar_actual_c, errors="coerce")
        errors = centers - actuals
        modal_hits = group.modal_bucket.eq(group.actual_bucket)
        top2_hits = group.apply(
            lambda row: row.actual_bucket in {row.modal_bucket, row.top2_bucket}, axis=1
        )
        top3_hits = group.apply(
            lambda row: row.actual_bucket in {
                row.modal_bucket, row.top2_bucket, row.top3_bucket
            }, axis=1
        )
        champion_bucket_error = (
            pd.to_numeric(group.modal_bucket, errors="coerce")
            - pd.to_numeric(group.actual_bucket, errors="coerce")
        ).abs()
        taf_error = (
            pd.to_numeric(group.taf_bucket, errors="coerce")
            - pd.to_numeric(group.actual_bucket, errors="coerce")
        ).abs()
        center_error = errors.abs()
        taf_pairs = taf_error.notna() & champion_bucket_error.notna()
        result.append(
            {
                "regime": regime,
                "checkpoint": checkpoint,
                "n": int(len(group)),
                "small_sample": len(group) < MIN_COMBINATION_SAMPLE,
                "modal_bucket_accuracy": float(modal_hits.mean()),
                "top2_coverage": float(top2_hits.mean()),
                "top3_coverage": float(top3_hits.mean()),
                "center_mae_c": float(center_error.mean()),
                "center_bias_c": float(errors.mean()),
                "underestimate_rate": float(errors.lt(0).mean()),
                "overestimate_rate": float(errors.gt(0).mean()),
                "taf_value_mae_gain_c": (
                    float((champion_bucket_error[taf_pairs] - taf_error[taf_pairs]).mean())
                    if taf_pairs.any() else None
                ),
                "taf_conflict_champion_wins": int(
                    (champion_bucket_error[taf_pairs] < taf_error[taf_pairs]).sum()
                ),
                "taf_conflict_taf_wins": int(
                    (taf_error[taf_pairs] < champion_bucket_error[taf_pairs]).sum()
                ),
                "taf_conflict_ties": int(
                    (taf_error[taf_pairs] == champion_bucket_error[taf_pairs]).sum()
                ),
                "mean_top1_top2_gap_pp": _number(
                    pd.to_numeric(group.top1_top2_gap_pp, errors="coerce").mean()
                ),
                "mean_model_spread_c": _number(
                    pd.to_numeric(group.raw_spread_c, errors="coerce").mean()
                ),
                "trading_pnl": None,
                "mean_hedge_width": None,
            }
        )
    return sorted(result, key=lambda row: (row["regime"], row["checkpoint"] != "ALL", row["checkpoint"]))
