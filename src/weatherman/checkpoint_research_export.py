"""Compact four-checkpoint export for offline counterfactual research.

This module is additive research infrastructure.  It is not imported by the
forecast engine, collector, or trading shadow runtime.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date, datetime, timezone
from numbers import Number
from typing import Any

import pandas as pd
from sqlalchemy import select, tuple_

from . import __version__
from .db import DailyActual, ForecastSnapshot, ForecastVariantSnapshot
from .d1_evening_export import (
    MAX_MODEL_RUNS_PER_MODEL,
    MAX_MODELS_PER_CHECKPOINT,
    _model_rows,
    _taf_features,
    _taf_rows,
    model_history_features,
)
from .market_replay_export import (
    AIRPORT,
    CHECKPOINT_LABELS,
    MAX_EXPORT_DAYS,
    _final_madrid_dates,
    checkpoint_schedule,
)
from .research_tracks import (
    AUTOMATIC_PROMOTION,
    RESEARCH_ONLY,
    active_regimes,
    checkpoint_evidence_class,
    positive_temperature_bucket,
    probability_ranking,
    regime_matrix_views,
)


CHECKPOINT_RESEARCH_EXPORT_VERSION = "checkpoint_research_export_v0.1"
EXPORT_ENGINE_VERSION = "v10.7.11"
PROTECTED_FORECAST_BASELINE = "v10.7.10"
MAX_CHECKPOINT_ROWS = MAX_EXPORT_DAYS * len(CHECKPOINT_LABELS)
MAX_MODEL_SOURCE_ROWS = (
    MAX_EXPORT_DAYS
    * len(CHECKPOINT_LABELS)
    * MAX_MODELS_PER_CHECKPOINT
    * MAX_MODEL_RUNS_PER_MODEL
)
MAX_TAF_SOURCE_ROWS = MAX_EXPORT_DAYS * 8
ESTIMATED_BYTES_PER_CHECKPOINT = 7_500

SNAPSHOT_FIELDS = (
    "id", "airport", "target_date", "captured_at", "checkpoint_label",
    "checkpoint_at", "checkpoint_status", "checkpoint_reconstructed",
    "freshness_status", "evidence_class", "raw_model_mean_c", "weighted_raw_c",
    "bias_corrected_c", "metar_conditioned_c", "final_forecast_c", "raw_spread_c",
    "final_spread_c", "model_count", "expected_model_count", "available_model_count",
    "fresh_model_count", "used_model_count", "used_models_json", "taf_max_temp_c",
    "taf_adjustment_c", "taf_conflict", "pre_taf_modal_bucket_c",
    "champion_modal_bucket_c", "taf_modal_bucket_flip", "observed_max_c",
    "temp_anchor_adjustment_c", "dryness_adjustment_c",
    "dewpoint_trend_adjustment_c", "cloud_adjustment_c",
    "heating_rate_adjustment_c", "recent_error_adjustment_c",
    "radiation_adjustment_c", "wind_adjustment_c", "run_trend_adjustment_c",
    "late_dry_mixing_adjustment_c", "failed_convection_adjustment_c",
    "clear_sky_override_adjustment_c", "rapid_heat_ramp_adjustment_c",
    "regional_cluster_adjustment_c", "persistent_hot_adjustment_c",
    "phase_anchor_delta_c", "maritime_advection_adjustment_c", "live_adjustment_c",
    "rapid_heat_ramp_active", "regional_cluster_active", "persistent_hot_active",
    "phase_vs_amplitude_active", "maritime_advection_active",
    "maritime_low_range_active", "post_convective_active", "post_convective_reports",
    "post_convective_spread_multiplier", "model_ceiling_reached_early",
    "features_json", "peak_lock_json",
)

RAW_FEATURE_KEYS = (
    "temperature_anomaly_c", "effective_temperature_residual_c",
    "recent_temperature_residual_c", "temperature_anchor_gain",
    "temperature_anchor_streak", "observed_dryness_c", "observed_dewpoint_c",
    "model_dryness_c", "dryness_surprise_c", "observed_dewpoint_trend_cph",
    "observed_cloud_cover_pct", "model_cloud_cover_pct", "cloud_surprise_pct",
    "observed_heating_rate_cph", "observed_heating_rate_30m_cph",
    "observed_heating_rate_60m_cph", "observed_heating_rate_120m_cph",
    "model_heating_rate_cph", "heating_rate_surprise_cph",
    "recent_station_residual_c", "model_radiation_wm2", "future_radiation_max_wm2",
    "remaining_model_rise_c", "future_outlook_status", "hours_to_critical_window_end",
    "late_dry_mixing_active", "failed_convection_active", "clear_sky_override_active",
    "sky_overlap_guard_active", "sky_overlap_reduction_c", "positive_live_cap_active",
    "positive_live_cap_reduction_c", "rapid_heat_ramp_applicable",
    "rapid_heat_ramp_strength", "rapid_heat_ramp_forecast_vs_latest_c",
    "rapid_heat_ramp_latest_actual_change_c", "rapid_heat_ramp_forecast_vs_two_back_c",
    "rapid_heat_ramp_bias_multiplier", "rapid_heat_ramp_spread_multiplier",
    "persistent_hot_applicable", "persistent_hot_strength",
    "persistent_hot_latest_anomaly_c", "persistent_hot_forecast_vs_latest_c",
    "persistent_hot_recent_warm_error_c", "persistent_hot_evidence_score",
    "persistent_hot_intensity", "regional_cluster_mean_gap_c",
    "regional_cluster_weight_multiplier", "post_convective_uncertainty_strength",
    "hours_since_latest_convection", "phase_vs_amplitude_strength",
    "phase_vs_amplitude_classification", "phase_shift_hours",
    "phase_same_time_residual_c", "phase_level_residual_after_shift_c",
    "phase_baseline_rmse_c", "phase_fit_rmse_c", "phase_anchor_blend",
    "maritime_advection_strength", "maritime_advection_temperature_rate_cph",
)

ADJUSTMENT_FIELDS = (
    "temp_anchor_adjustment_c", "dryness_adjustment_c",
    "dewpoint_trend_adjustment_c", "cloud_adjustment_c",
    "heating_rate_adjustment_c", "recent_error_adjustment_c",
    "radiation_adjustment_c", "wind_adjustment_c", "run_trend_adjustment_c",
    "late_dry_mixing_adjustment_c", "failed_convection_adjustment_c",
    "clear_sky_override_adjustment_c", "rapid_heat_ramp_adjustment_c",
    "regional_cluster_adjustment_c", "persistent_hot_adjustment_c",
    "phase_anchor_delta_c", "maritime_advection_adjustment_c",
    "taf_adjustment_c", "live_adjustment_c",
)


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Number):
        return value.item() if hasattr(value, "item") else value
    return value


def _json(value: object, default: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _bool(value: object) -> bool:
    if value is None:
        return False
    try:
        if bool(pd.isna(value)):
            return False
    except (TypeError, ValueError):
        pass
    return bool(value)


def _checkpoint_rows(connection, target_dates: list[date]) -> pd.DataFrame:
    if not target_dates:
        return pd.DataFrame()
    row_cap = min(MAX_CHECKPOINT_ROWS, len(target_dates) * len(CHECKPOINT_LABELS))
    frame = pd.read_sql(
        select(*(getattr(ForecastSnapshot, name) for name in SNAPSHOT_FIELDS)).where(
            ForecastSnapshot.airport == AIRPORT,
            ForecastSnapshot.target_date.in_(target_dates),
            ForecastSnapshot.checkpoint_label.in_(CHECKPOINT_LABELS),
        ).order_by(ForecastSnapshot.target_date, ForecastSnapshot.captured_at).limit(
            row_cap + 1
        ),
        connection,
    )
    if len(frame) > row_cap:
        raise RuntimeError("Safety stop: checkpoint rows exceed configured cap.")
    if frame.empty:
        return frame
    frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    return frame.sort_values("captured_at").drop_duplicates(
        ["target_date", "checkpoint_label"], keep="last"
    )


def _champion_rows(connection, checkpoints: pd.DataFrame) -> pd.DataFrame:
    if checkpoints.empty:
        return pd.DataFrame()
    keys = [
        (row.target_date, pd.Timestamp(row.captured_at).to_pydatetime())
        for row in checkpoints.itertuples()
    ]
    frame = pd.read_sql(
        select(
            ForecastVariantSnapshot.target_date,
            ForecastVariantSnapshot.captured_at,
            ForecastVariantSnapshot.forecast_c,
            ForecastVariantSnapshot.spread_c,
            ForecastVariantSnapshot.probabilities_json,
            ForecastVariantSnapshot.forecast_confidence,
        ).where(
            ForecastVariantSnapshot.airport == AIRPORT,
            ForecastVariantSnapshot.variant == "Champion",
            tuple_(ForecastVariantSnapshot.target_date, ForecastVariantSnapshot.captured_at).in_(keys),
        ).limit(MAX_CHECKPOINT_ROWS + 1),
        connection,
    )
    if len(frame) > MAX_CHECKPOINT_ROWS:
        raise RuntimeError("Safety stop: Champion rows exceed configured cap.")
    if not frame.empty:
        frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
        frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    return frame


def _actuals(connection, target_dates: list[date]) -> dict[date, float]:
    if not target_dates:
        return {}
    frame = pd.read_sql(
        select(DailyActual.target_date, DailyActual.max_temp_c, DailyActual.source).where(
            DailyActual.airport == AIRPORT,
            DailyActual.target_date.in_(target_dates),
        ).limit(MAX_EXPORT_DAYS * 4),
        connection,
    )
    if frame.empty:
        return {}
    frame = frame[frame.source.fillna("").astype(str).str.contains("metar|station", case=False)]
    return {
        pd.Timestamp(row.target_date).date(): float(row.max_temp_c)
        for row in frame.itertuples()
    }


def _cutoff(row: dict[str, Any], target: date) -> datetime:
    parsed = pd.to_datetime(row.get("checkpoint_at"), utc=True, errors="coerce")
    if pd.notna(parsed):
        return parsed.to_pydatetime()
    return dict(checkpoint_schedule(target))[row["checkpoint_label"]]


def _local_probability_window(value: object) -> dict[str, Any]:
    ranking = probability_ranking(value)
    if not ranking:
        return {"buckets": {}, "normalization_sum": None, "window_policy": "unavailable"}
    raw = {bucket: probability for bucket, probability in ranking}
    modal = ranking[0][0]
    selected = {modal - 1, modal, modal + 1, *(item[0] for item in ranking[:3])}
    window = {bucket: raw[bucket] for bucket in sorted(selected) if bucket in raw}
    return {
        "buckets": window,
        "normalization_sum": sum(raw.values()),
        "window_probability_sum": sum(window.values()),
        "window_policy": "top3_plus_direct_modal_neighbors",
        "mapping_metadata": "persisted_champion_distribution_not_reconstructed_from_center",
    }


def _checkpoint_matrix(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for checkpoint in CHECKPOINT_LABELS:
        rows = [
            row for row in records
            if row["checkpoint_label"] == checkpoint
            and row["evidence_class"] == "scheduled_causal"
            and row.get("actual_bucket") is not None
        ]
        if not rows:
            result.append({"checkpoint": checkpoint, "n": 0})
            continue
        errors = [float(row["signed_error_c"]) for row in rows if row.get("signed_error_c") is not None]
        modal_hits = [row["champion_modal_bucket"] == row["actual_bucket"] for row in rows]
        top2 = [row["actual_bucket"] in {row["top1_bucket"], row["top2_bucket"]} for row in rows]
        top3 = [
            row["actual_bucket"] in {row["top1_bucket"], row["top2_bucket"], row["top3_bucket"]}
            for row in rows
        ]
        taf_conflicts = [row for row in rows if row.get("taf_agreement") == "disagreement"]
        taf_wins = champion_wins = neither = 0
        for row in taf_conflicts:
            taf = row.get("taf_bucket")
            champion = row.get("champion_modal_bucket")
            actual = row.get("actual_bucket")
            if taf is None or champion is None:
                neither += 1
            elif abs(taf - actual) < abs(champion - actual):
                taf_wins += 1
            elif abs(champion - actual) < abs(taf - actual):
                champion_wins += 1
            else:
                neither += 1
        result.append({
            "checkpoint": checkpoint,
            "n": len(rows),
            "modal_accuracy": sum(modal_hits) / len(rows),
            "top2_coverage": sum(top2) / len(rows),
            "top3_coverage": sum(top3) / len(rows),
            "center_mae_c": sum(abs(error) for error in errors) / len(errors),
            "signed_bias_c": sum(errors) / len(errors),
            "underforecast_rate": sum(error < 0 for error in errors) / len(errors),
            "overforecast_rate": sum(error > 0 for error in errors) / len(errors),
            "taf_conflict_count": len(taf_conflicts),
            "taf_wins": taf_wins,
            "champion_wins": champion_wins,
            "neither": neither,
            "bucket_boundary_n": sum("Bucket Boundary" in row["active_regimes"] for row in rows),
            "model_trend_counts": dict(Counter(
                row["model_history"]["consensus_movement"] for row in rows
            )),
        })
    return result


def _model_pattern_labels(record: dict[str, Any]) -> list[str]:
    history = record["model_history"]
    labels: list[str] = []
    movement = history.get("consensus_movement")
    if movement in {"warming", "cooling"}:
        labels.append(f"consensus_{movement}")
    if int(history.get("warming_model_count") or 0) >= 2:
        labels.append("multiple_models_warming")
    if int(history.get("cooling_model_count") or 0) >= 2:
        labels.append("multiple_models_cooling")
    trends = {
        str(item.get("model", "")).casefold(): item.get("run_trend")
        for item in history.get("models", [])
    }
    ifs = {trend for model, trend in trends.items() if "ifs" in model or "ecmwf" in model}
    arome = {trend for model, trend in trends.items() if "arome" in model}
    for direction in ("warming", "cooling"):
        if direction in ifs and direction in arome:
            labels.append(f"ifs_arome_{direction}")
    spread_change = history.get("spread_change_c")
    if spread_change is not None and float(spread_change) <= -0.2:
        labels.append("spread_narrowing")
    elif spread_change is not None and float(spread_change) >= 0.2:
        labels.append("spread_widening")
    cluster = history.get("cluster_shift")
    if cluster in {"up", "down"}:
        labels.append(f"cluster_shift_{cluster}")
    if history.get("hot_model_outliers"):
        labels.append("hot_model_outlier")
    if history.get("cold_model_outliers"):
        labels.append("cold_model_outlier")
    return labels


def _model_pattern_matrix(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[tuple[str, str, dict[str, Any]]] = []
    for row in records:
        if row["evidence_class"] != "scheduled_causal" or row.get("actual_bucket") is None:
            continue
        for pattern in _model_pattern_labels(row):
            expanded.append((pattern, "ALL", row))
            expanded.append((pattern, row["checkpoint_label"], row))
    result: list[dict[str, Any]] = []
    for pattern, checkpoint in sorted({(item[0], item[1]) for item in expanded}):
        rows = [item[2] for item in expanded if item[0] == pattern and item[1] == checkpoint]
        errors = [float(row["signed_error_c"]) for row in rows]
        result.append({
            "pattern": pattern,
            "checkpoint": checkpoint,
            "n": len(rows),
            "small_sample": len(rows) < 10,
            "actual_minus_champion_mean_c": -sum(errors) / len(errors),
            "underforecast_rate": sum(error < 0 for error in errors) / len(errors),
            "overforecast_rate": sum(error > 0 for error in errors) / len(errors),
            "modal_accuracy": sum(
                row["champion_modal_bucket"] == row["actual_bucket"] for row in rows
            ) / len(rows),
            "top2_coverage": sum(
                row["actual_bucket"] in {row["top1_bucket"], row["top2_bucket"]}
                for row in rows
            ) / len(rows),
            "center_mae_c": sum(abs(error) for error in errors) / len(errors),
        })
    return result


def build_checkpoint_research_export(
    connection,
    *,
    days: int = 30,
    end_date: date | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build one bounded four-checkpoint research dataset for offline analysis."""
    if not 1 <= int(days) <= MAX_EXPORT_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_EXPORT_DAYS}")
    generated = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    target_dates = _final_madrid_dates(connection, int(days), end_date)
    checkpoints = _checkpoint_rows(connection, target_dates)
    champions = _champion_rows(connection, checkpoints)
    if not checkpoints.empty and not champions.empty:
        checkpoints = checkpoints.merge(
            champions, on=["target_date", "captured_at"], how="left",
            suffixes=("", "_champion"),
        )
    actuals = _actuals(connection, target_dates)
    model_frames: dict[str, pd.DataFrame] = {}
    model_rows_total = 0
    for label in CHECKPOINT_LABELS:
        subset = checkpoints[checkpoints.checkpoint_label.eq(label)]
        frame = _model_rows(connection, target_dates, subset)
        model_frames[label] = frame
        model_rows_total += len(frame)
    if model_rows_total > MAX_MODEL_SOURCE_ROWS:
        raise RuntimeError("Safety stop: combined model source rows exceed configured cap.")
    tafs = _taf_rows(connection, target_dates)

    records: list[dict[str, Any]] = []
    for row in checkpoints.sort_values(["target_date", "checkpoint_at"]).to_dict("records"):
        target = row["target_date"]
        checkpoint = row["checkpoint_label"]
        cutoff = _cutoff(row, target)
        probabilities = _json(row.get("probabilities_json"), {})
        ranked = probability_ranking(probabilities)
        top = ranked + [(None, None)] * 3
        center = pd.to_numeric(row.get("forecast_c", row.get("final_forecast_c")), errors="coerce")
        actual = actuals.get(target)
        actual_bucket = positive_temperature_bucket(actual)
        source_class = row.get("evidence_class")
        reconstructed = _bool(row.get("checkpoint_reconstructed"))
        evidence = checkpoint_evidence_class(row.get("checkpoint_status"), reconstructed)
        model_history = model_history_features(model_frames[checkpoint], target, cutoff)
        taf_history = _taf_features(tafs, target, cutoff)
        taf_value = taf_history.get("current_tx_c")
        if taf_value is None:
            taf_value = row.get("taf_max_temp_c")
        taf_bucket = positive_temperature_bucket(taf_value)
        features = _json(row.get("features_json"), {})
        raw_features = {key: features[key] for key in RAW_FEATURE_KEYS if key in features}
        adjustments = {field: row.get(field) for field in ADJUSTMENT_FIELDS}
        additive_live_center = (
            float(row["bias_corrected_c"]) + float(row.get("live_adjustment_c") or 0.0)
            if row.get("bias_corrected_c") is not None else None
        )
        additive_pre_taf_center = additive_live_center
        additive_final_center = (
            additive_live_center + float(row.get("taf_adjustment_c") or 0.0)
            if additive_live_center is not None else None
        )
        record: dict[str, Any] = {
            "target_date": target.isoformat(),
            "checkpoint_label": checkpoint,
            "checkpoint_at": cutoff.isoformat(),
            "captured_at": _safe(row.get("captured_at")),
            "forecast_snapshot_id": row.get("id"),
            "research_export_version": CHECKPOINT_RESEARCH_EXPORT_VERSION,
            "evidence_class": evidence,
            "source_evidence_class": source_class,
            "evaluation_class": "not_asserted_by_export",
            "checkpoint_status": row.get("checkpoint_status"),
            "checkpoint_reconstructed": reconstructed,
            "freshness_status": row.get("freshness_status"),
            "raw_center_c": row.get("raw_model_mean_c"),
            "weighted_raw_center_c": row.get("weighted_raw_c"),
            "bias_center_c": row.get("bias_corrected_c"),
            "live_center_c": row.get("metar_conditioned_c"),
            "pre_taf_center_c": additive_pre_taf_center,
            "final_center_c": None if pd.isna(center) else float(center),
            "champion_center_c": None if pd.isna(center) else float(center),
            "champion_modal_bucket": top[0][0],
            "top1_bucket": top[0][0], "top1_probability": top[0][1],
            "top2_bucket": top[1][0], "top2_probability": top[1][1],
            "top3_bucket": top[2][0], "top3_probability": top[2][1],
            "top1_top2_gap_pp": (
                (top[0][1] - top[1][1]) * 100
                if top[0][1] is not None and top[1][1] is not None else None
            ),
            "distance_to_bucket_boundary_c": (
                abs((float(center) - math.floor(float(center))) - 0.5)
                if pd.notna(center) else None
            ),
            "probability_window": _local_probability_window(probabilities),
            "model_spread_c": row.get("raw_spread_c"),
            "final_spread_c": row.get("spread_c", row.get("final_spread_c")),
            "model_count": row.get("model_count"),
            "expected_model_count": row.get("expected_model_count"),
            "available_model_count": row.get("available_model_count"),
            "fresh_model_count": row.get("fresh_model_count"),
            "used_model_count": row.get("used_model_count"),
            "used_models": _json(row.get("used_models_json"), []),
            "model_history": model_history,
            "model_split_indicator": (
                "high" if float(row.get("raw_spread_c") or 0) >= 2.0
                else "low" if float(row.get("raw_spread_c") or 0) <= 1.0
                else "middle"
            ),
            "taf_bucket": taf_bucket,
            "taf_history": taf_history,
            "taf_vs_champion_bucket_difference": (
                taf_bucket - top[0][0]
                if taf_bucket is not None and top[0][0] is not None else None
            ),
            "taf_agreement": (
                "agreement" if taf_bucket in {top[0][0], top[1][0]}
                else "disagreement" if taf_bucket is not None else "unavailable"
            ),
            "taf_outside_top2": taf_bucket is not None and taf_bucket not in {top[0][0], top[1][0]},
            "taf_adjustment_c": row.get("taf_adjustment_c"),
            "taf_modal_bucket_flip": _bool(row.get("taf_modal_bucket_flip")),
            "active_regimes": [],
            "regime_raw_inputs": raw_features,
            "correction_contributions": adjustments,
            "stage_attribution": {
                "additive_live_center_c": additive_live_center,
                "additive_final_center_c": additive_final_center,
                "stored_live_center_c": row.get("metar_conditioned_c"),
                "stored_final_center_c": None if pd.isna(center) else float(center),
                "conditioning_effect_after_additive_components_c": (
                    float(center) - additive_final_center
                    if pd.notna(center) and additive_final_center is not None else None
                ),
                "before_after_per_regime_available": False,
            },
            "stored_metar_actual_c": actual,
            "actual_bucket": actual_bucket,
            "signed_error_c": (
                float(center) - actual if pd.notna(center) and actual is not None else None
            ),
            "absolute_error_c": (
                abs(float(center) - actual) if pd.notna(center) and actual is not None else None
            ),
            "bucket_error": (
                top[0][0] - actual_bucket
                if top[0][0] is not None and actual_bucket is not None else None
            ),
            "aemet_physical_tmax_c": None,
            "market_resolution_actual": None,
            "observed_max_at_checkpoint_c": row.get("observed_max_c"),
        }
        regime_record = {
            **record,
            "modal_bucket": record["champion_modal_bucket"],
            "raw_spread_c": record["model_spread_c"],
            "features_json": features,
            **adjustments,
            **{field: row.get(field) for field in (
                "rapid_heat_ramp_active", "regional_cluster_active",
                "persistent_hot_active", "phase_vs_amplitude_active",
                "maritime_advection_active",
            )},
        }
        record["active_regimes"] = active_regimes(regime_record)
        records.append(record)

    evidence_counts = dict(Counter(record["evidence_class"] for record in records))
    checkpoint_counts = dict(Counter(record["checkpoint_label"] for record in records))
    regime_records = [
        {
            **record,
            "checkpoint": record["checkpoint_label"],
            "modal_bucket": record["champion_modal_bucket"],
            "raw_spread_c": record["model_spread_c"],
        }
        for record in records
    ]
    return _safe({
        "schema_version": "1.0",
        "application_version": __version__,
        "export_engine_version": EXPORT_ENGINE_VERSION,
        "protected_forecast_baseline": PROTECTED_FORECAST_BASELINE,
        "generated_at": generated.isoformat(),
        "airport": AIRPORT,
        "research_export_version": CHECKPOINT_RESEARCH_EXPORT_VERSION,
        "research_only": RESEARCH_ONLY,
        "automatic_promotion": AUTOMATIC_PROMOTION,
        "requested_final_days": int(days),
        "exported_final_days": len(target_dates),
        "records": records,
        "default_analysis_view": "scheduled_causal_only",
        "evidence_policy": {
            "checkpoint_origin_values": ["scheduled_causal", "reconstructed_research"],
            "sequential_oos": "not_inferred_without_explicit_persisted_cohort_membership",
            "live_shadow": "not_inferred_without_explicit_persisted_shadow_membership",
            "never_combine_reconstructed_with_scheduled_by_default": True,
        },
        "checkpoint_research_matrix": _checkpoint_matrix(records),
        "model_trend_pattern_matrix": _model_pattern_matrix(records),
        "regime_checkpoint_matrix": regime_matrix_views(regime_records),
        "counterfactual_support": {
            "available_raw_feature_keys": sorted({
                key for record in records for key in record["regime_raw_inputs"]
            }),
            "available_correction_contributions": list(ADJUSTMENT_FIELDS),
            "not_reconstructable": [
                "regime_specific_forecast_before_and_after_when_not_persisted",
                "trigger_thresholds_not_present_in_features_json_or_snapshot_columns",
                "mutual_exclusion_order_not_persisted_per_checkpoint",
                "full_probability_generation_internal_state",
            ],
            "current_research_thresholds": {
                "model_spread_low_c": 1.0,
                "model_spread_high_c": 2.0,
                "bucket_boundary_distance_c": 0.15,
                "regime_combination_min_n": 10,
            },
            "methodology": {
                "in_sample_upper_bound": "diagnostic_only_not_future_performance",
                "walk_forward": "fit_earlier_days_then_score_next_day",
                "optimization_targets": [
                    "modal_accuracy", "calibration", "top2", "top3", "mae", "bias",
                    "brier", "log_loss", "checkpoint_robustness", "time_robustness",
                    "low_rule_complexity",
                ],
            },
        },
        "export_log": {
            "number_of_target_days": len(target_dates),
            "number_of_checkpoints": len(records),
            "number_of_rows": len(records),
            "rows_by_evidence_class": evidence_counts,
            "rows_by_checkpoint": checkpoint_counts,
            "model_source_rows": model_rows_total,
            "taf_source_rows": len(tafs),
            "database_queries_executed": 9,
            "output_file_size_bytes": None,
        },
        "transfer_policy": {
            "production_access": "read_only_transaction",
            "full_table_scans": False,
            "automatic_backfill": False,
            "maximum_days": MAX_EXPORT_DAYS,
            "maximum_checkpoint_rows": MAX_CHECKPOINT_ROWS,
            "maximum_model_source_rows": MAX_MODEL_SOURCE_ROWS,
            "maximum_taf_source_rows": MAX_TAF_SOURCE_ROWS,
            "raw_model_grids_exported": False,
            "raw_provider_payloads_exported": False,
            "aemet_read_from_neon": False,
            "market_resolution_read": False,
        },
    })
