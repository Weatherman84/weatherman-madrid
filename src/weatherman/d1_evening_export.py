"""Bounded read-only export for Track C: D-1 Evening Forecast Improvement."""
from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from numbers import Number
from typing import Any

import pandas as pd
from sqlalchemy import and_, func, or_, select, tuple_

from . import __version__
from .db import DailyActual, Forecast, ForecastSnapshot, ForecastVariantSnapshot, TafReport
from .d1_evening_research import (
    D1_CHECKPOINT,
    D1_CHALLENGER_VERSION,
    build_d1_walk_forward_challenger,
    compare_d1_challenger,
)
from .market_replay_export import AIRPORT, MAX_EXPORT_DAYS, _final_madrid_dates, checkpoint_schedule
from .research_tracks import (
    AUTOMATIC_PROMOTION,
    RESEARCH_ONLY,
    active_regimes,
    checkpoint_evidence_class,
    positive_temperature_bucket,
    probability_ranking,
    regime_matrix_views,
)


EXPORT_ENGINE_VERSION = "v10.7.11"
PROTECTED_FORECAST_BASELINE = "v10.7.10"
MAX_D1_CHECKPOINT_ROWS = MAX_EXPORT_DAYS
MAX_MODELS_PER_CHECKPOINT = 12
MAX_MODEL_RUNS_PER_MODEL = 2
MAX_MODEL_SOURCE_ROWS = (
    MAX_EXPORT_DAYS * MAX_MODELS_PER_CHECKPOINT * MAX_MODEL_RUNS_PER_MODEL
)
MAX_TAF_SOURCE_ROWS = MAX_EXPORT_DAYS * 8
ESTIMATED_BYTES_PER_D1_DAY = 8_000

D1_SNAPSHOT_FIELDS = (
    "airport", "target_date", "captured_at", "checkpoint_label", "checkpoint_at",
    "checkpoint_status", "checkpoint_reconstructed", "freshness_status", "evidence_class",
    "raw_model_mean_c", "weighted_raw_c", "bias_corrected_c", "metar_conditioned_c",
    "final_forecast_c", "raw_spread_c", "final_spread_c", "model_count",
    "taf_max_temp_c", "taf_adjustment_c", "taf_conflict", "pre_taf_modal_bucket_c",
    "temp_anchor_adjustment_c", "clear_sky_override_adjustment_c",
    "rapid_heat_ramp_active", "regional_cluster_active", "persistent_hot_active",
    "phase_vs_amplitude_active", "maritime_advection_active", "features_json",
    "source_provenance_json", "expected_model_count", "available_model_count",
    "fresh_model_count", "used_model_count", "used_models_json",
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
    row_cap = min(MAX_D1_CHECKPOINT_ROWS, len(target_dates))
    frame = pd.read_sql(
        select(*(getattr(ForecastSnapshot, name) for name in D1_SNAPSHOT_FIELDS)).where(
            ForecastSnapshot.airport == AIRPORT,
            ForecastSnapshot.target_date.in_(target_dates),
            ForecastSnapshot.checkpoint_label == D1_CHECKPOINT,
        ).order_by(ForecastSnapshot.target_date, ForecastSnapshot.captured_at).limit(
            row_cap + 1
        ),
        connection,
    )
    if len(frame) > row_cap:
        raise RuntimeError("Safety stop: D-1 checkpoint rows exceed configured cap.")
    if frame.empty:
        return frame
    frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    return frame.sort_values("captured_at").drop_duplicates("target_date", keep="last")


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
        ).limit(MAX_D1_CHECKPOINT_ROWS + 1),
        connection,
    )
    if len(frame) > MAX_D1_CHECKPOINT_ROWS:
        raise RuntimeError("Safety stop: D-1 Champion rows exceed configured cap.")
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


def _model_rows(connection, target_dates: list[date], checkpoints: pd.DataFrame) -> pd.DataFrame:
    if not target_dates or checkpoints.empty:
        return pd.DataFrame()
    checkpoint_cutoffs: dict[date, datetime] = {}
    for row in checkpoints.itertuples():
        target = pd.Timestamp(row.target_date).date()
        parsed = pd.to_datetime(row.checkpoint_at, utc=True, errors="coerce")
        if pd.notna(parsed):
            checkpoint_cutoffs[target] = pd.Timestamp(parsed).to_pydatetime()
    effective_available_at = func.coalesce(
        Forecast.available_at, Forecast.fetched_at, Forecast.run_at
    )
    causal_windows = []
    for target in target_dates:
        cutoff = checkpoint_cutoffs.get(
            target, dict(checkpoint_schedule(target))[D1_CHECKPOINT]
        )
        causal_windows.append(and_(
            Forecast.target_date == target,
            Forecast.run_at >= cutoff - timedelta(hours=48),
            Forecast.run_at <= cutoff,
            effective_available_at <= cutoff,
        ))
    row_cap = min(
        MAX_MODEL_SOURCE_ROWS,
        len(target_dates) * MAX_MODELS_PER_CHECKPOINT * MAX_MODEL_RUNS_PER_MODEL,
    )
    fields = (
        "target_date", "model", "max_temp_c", "run_at", "model_run_at",
        "available_at", "fetched_at", "source", "horizon", "provenance_status",
    )
    captured = select(
        *(getattr(Forecast, field) for field in fields),
        func.row_number().over(
            partition_by=(
                Forecast.target_date, Forecast.model, Forecast.model_run_at
            ),
            order_by=(effective_available_at.desc(), Forecast.run_at.desc()),
        ).label("capture_rank"),
    ).where(
        Forecast.airport == AIRPORT,
        or_(*causal_windows),
    ).subquery("causal_model_captures")
    ranked = select(
        *(captured.c[field] for field in fields),
        func.row_number().over(
            partition_by=(captured.c.target_date, captured.c.model),
            order_by=(
                captured.c.model_run_at.desc().nullslast(),
                captured.c.run_at.desc(),
            ),
        ).label("model_run_rank"),
    ).where(captured.c.capture_rank == 1).subquery("distinct_model_runs")
    frame = pd.read_sql(
        select(*(ranked.c[field] for field in fields)).where(
            ranked.c.model_run_rank <= MAX_MODEL_RUNS_PER_MODEL
        ).order_by(
            ranked.c.target_date, ranked.c.model, ranked.c.model_run_at.desc()
        ).limit(row_cap + 1),
        connection,
    )
    if len(frame) > row_cap:
        raise RuntimeError("Safety stop: model source rows exceed configured cap.")
    if not frame.empty:
        frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
        for column in ("run_at", "model_run_at", "available_at", "fetched_at"):
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    return frame


def _taf_rows(connection, target_dates: list[date]) -> pd.DataFrame:
    if not target_dates:
        return pd.DataFrame()
    row_cap = min(MAX_TAF_SOURCE_ROWS, len(target_dates) * 8)
    frame = pd.read_sql(
        select(
            TafReport.target_local_date, TafReport.issue_time, TafReport.bulletin_time,
            TafReport.max_temp_c, TafReport.max_temp_at, TafReport.is_amended,
            TafReport.is_corrected, TafReport.collected_at, TafReport.first_seen_at,
            TafReport.fetched_at, TafReport.content_hash, TafReport.revision_of_hash,
            TafReport.backfilled, TafReport.coverage_status,
        ).where(
            TafReport.airport == AIRPORT,
            TafReport.target_local_date.in_(target_dates),
        ).order_by(TafReport.target_local_date, TafReport.issue_time).limit(
            row_cap + 1
        ),
        connection,
    )
    if len(frame) > row_cap:
        raise RuntimeError("Safety stop: TAF source rows exceed configured cap.")
    if not frame.empty:
        frame["target_local_date"] = pd.to_datetime(
            frame.target_local_date, errors="coerce"
        ).dt.date
        for column in (
            "issue_time", "bulletin_time", "max_temp_at", "collected_at",
            "first_seen_at", "fetched_at",
        ):
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    return frame


def _checkpoint_at(row: dict[str, Any], target: date) -> datetime:
    parsed = pd.to_datetime(row.get("checkpoint_at"), utc=True, errors="coerce")
    return (
        parsed.to_pydatetime()
        if pd.notna(parsed)
        else dict(checkpoint_schedule(target))[D1_CHECKPOINT]
    )


def _effective_time(row: pd.Series) -> pd.Timestamp:
    for field in ("available_at", "fetched_at", "run_at"):
        value = row.get(field)
        if pd.notna(value):
            return pd.Timestamp(value)
    return pd.NaT


def model_history_features(
    frame: pd.DataFrame, target: date, cutoff: datetime
) -> dict[str, Any]:
    if frame.empty:
        return _empty_model_features()
    selected = frame[frame.target_date.eq(target)].copy()
    selected["effective_available_at"] = selected.apply(_effective_time, axis=1)
    selected = selected[
        selected.effective_available_at.notna()
        & selected.effective_available_at.le(pd.Timestamp(cutoff))
        & selected.run_at.ge(pd.Timestamp(cutoff) - pd.Timedelta(hours=48))
    ].sort_values(["model", "effective_available_at", "run_at"])
    models: list[dict[str, Any]] = []
    for model, group in selected.groupby("model", sort=True):
        distinct = group.drop_duplicates("model_run_at", keep="last")
        current = distinct.iloc[-1]
        previous = distinct.iloc[-2] if len(distinct) > 1 else None
        delta = (
            float(current.max_temp_c) - float(previous.max_temp_c)
            if previous is not None else None
        )
        models.append({
            "model": str(model),
            "forecast_max_c": float(current.max_temp_c),
            "model_run_at": _safe(current.model_run_at),
            "available_at": _safe(current.available_at),
            "effective_available_at": _safe(current.effective_available_at),
            "age_at_checkpoint_minutes": round(
                (pd.Timestamp(cutoff) - current.effective_available_at).total_seconds() / 60, 3
            ),
            "previous_forecast_max_c": (
                float(previous.max_temp_c) if previous is not None else None
            ),
            "previous_model_run_at": _safe(previous.model_run_at) if previous is not None else None,
            "run_to_run_delta_c": delta,
            "run_trend": (
                "warming" if delta is not None and delta >= 0.2
                else "cooling" if delta is not None and delta <= -0.2
                else "stable" if delta is not None else "unavailable"
            ),
            "source": current.source,
            "provenance_status": current.provenance_status,
        })
    current_values = [item["forecast_max_c"] for item in models]
    previous_values = [
        item["previous_forecast_max_c"]
        for item in models if item["previous_forecast_max_c"] is not None
    ]
    consensus = statistics.fmean(current_values) if current_values else None
    prior_consensus = statistics.fmean(previous_values) if previous_values else None
    current_spread = statistics.pstdev(current_values) if len(current_values) > 1 else None
    previous_spread = statistics.pstdev(previous_values) if len(previous_values) > 1 else None
    for item in models:
        offset = item["forecast_max_c"] - consensus if consensus is not None else None
        item["consensus_offset_c"] = offset
        item["outlier"] = (
            "hot" if offset is not None and offset >= 1.5
            else "cold" if offset is not None and offset <= -1.5
            else None
        )
    consensus_delta = (
        consensus - prior_consensus
        if consensus is not None and prior_consensus is not None else None
    )
    return {
        "models": models,
        "model_count": len(models),
        "previous_run_pairs": len(previous_values),
        "consensus_c": consensus,
        "previous_consensus_c": prior_consensus,
        "consensus_run_trend_c": consensus_delta,
        "consensus_movement": (
            "warming" if consensus_delta is not None and consensus_delta >= 0.2
            else "cooling" if consensus_delta is not None and consensus_delta <= -0.2
            else "stable" if consensus_delta is not None else "unavailable"
        ),
        "current_spread_c": current_spread,
        "previous_spread_c": previous_spread,
        "spread_change_c": (
            current_spread - previous_spread
            if current_spread is not None and previous_spread is not None else None
        ),
        "cluster_shift": (
            "up" if consensus_delta is not None and consensus_delta >= 0.2
            else "down" if consensus_delta is not None and consensus_delta <= -0.2
            else "stable" if consensus_delta is not None else "unavailable"
        ),
        "hot_model_outliers": [item["model"] for item in models if item["outlier"] == "hot"],
        "cold_model_outliers": [item["model"] for item in models if item["outlier"] == "cold"],
    }


def _empty_model_features() -> dict[str, Any]:
    return {
        "models": [], "model_count": 0, "previous_run_pairs": 0,
        "consensus_c": None, "previous_consensus_c": None,
        "consensus_run_trend_c": None, "consensus_movement": "unavailable",
        "current_spread_c": None, "previous_spread_c": None,
        "spread_change_c": None, "cluster_shift": "unavailable",
        "hot_model_outliers": [], "cold_model_outliers": [],
    }


def _taf_features(frame: pd.DataFrame, target: date, cutoff: datetime) -> dict[str, Any]:
    if frame.empty:
        return _empty_taf_features()
    selected = frame[frame.target_local_date.eq(target)].copy()
    selected["effective_available_at"] = selected.first_seen_at.fillna(
        selected.fetched_at
    ).fillna(selected.collected_at).fillna(selected.issue_time)
    selected = selected[
        selected.effective_available_at.notna()
        & selected.effective_available_at.le(pd.Timestamp(cutoff))
        & ~selected.backfilled.fillna(False).astype(bool)
    ].sort_values(["effective_available_at", "issue_time"])
    if selected.empty:
        return _empty_taf_features()
    revisions = selected.drop_duplicates("content_hash", keep="last")
    current = revisions.iloc[-1]
    previous = revisions.iloc[-2] if len(revisions) > 1 else None
    delta = (
        float(current.max_temp_c) - float(previous.max_temp_c)
        if previous is not None
        and pd.notna(current.max_temp_c) and pd.notna(previous.max_temp_c)
        else None
    )
    return {
        "current_tx_c": float(current.max_temp_c) if pd.notna(current.max_temp_c) else None,
        "current_issue_time": _safe(current.issue_time),
        "current_available_at": _safe(current.effective_available_at),
        "previous_tx_c": (
            float(previous.max_temp_c)
            if previous is not None and pd.notna(previous.max_temp_c) else None
        ),
        "previous_issue_time": _safe(previous.issue_time) if previous is not None else None,
        "tx_revision_delta_c": delta,
        "tx_revision_direction": (
            "up" if delta is not None and delta > 0
            else "down" if delta is not None and delta < 0
            else "unchanged" if delta is not None else "unavailable"
        ),
        "revision_count": max(0, len(revisions) - 1),
        "coverage_status": current.coverage_status,
    }


def _empty_taf_features() -> dict[str, Any]:
    return {
        "current_tx_c": None, "current_issue_time": None, "current_available_at": None,
        "previous_tx_c": None, "previous_issue_time": None,
        "tx_revision_delta_c": None, "tx_revision_direction": "unavailable",
        "revision_count": 0, "coverage_status": "unavailable",
    }


def _availability(records: list[dict[str, Any]]) -> dict[str, Any]:
    models: dict[str, dict[str, int]] = {}
    for record in records:
        for item in record.get("model_history", {}).get("models", []):
            status = models.setdefault(item["model"], {"current": 0, "previous": 0})
            status["current"] += 1
            status["previous"] += int(item.get("previous_forecast_max_c") is not None)
    return {
        "models": models,
        "checkpoint_fields_missing": sorted({
            field for field in (
                "raw_c", "bias_c", "live_c", "taf_adjusted_center_c",
                "champion_center_c", "champion_probabilities", "raw_spread_c",
            ) if any(record.get(field) is None for record in records)
        }),
        "model_fields": [
            "forecast_max_c", "model_run_at", "available_at", "effective_available_at",
            "previous_forecast_max_c", "previous_model_run_at", "run_to_run_delta_c",
        ],
        "automatic_backfill_executed": False,
    }


def _error_avoidance_analysis(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Explain D-1 modal misses using checkpoint-causal signals only."""
    upside = {
        "taf_upside_signal", "warming_run_trend", "clear_sky_warm_bias",
        "negative_anchor_risk",
    }
    downside = {"taf_downside_signal", "cooling_run_trend"}
    diagnostics: list[dict[str, Any]] = []
    for record in records:
        bucket_error = record.get("champion_bucket_error")
        if bucket_error in (None, 0):
            continue
        reasons = set(record.get("reason_codes") or [])
        underforecast = bucket_error < 0
        helpful = sorted(reasons.intersection(upside if underforecast else downside))
        misleading = sorted(reasons.intersection(downside if underforecast else upside))
        diagnostics.append({
            "target_date": record.get("target_date"),
            "checkpoint_at": record.get("checkpoint_at"),
            "evidence_class": record.get("evidence_class"),
            "actual_bucket": record.get("actual_bucket"),
            "champion_modal_bucket": record.get("modal_bucket"),
            "champion_bucket_error": bucket_error,
            "champion_center_error_c": record.get("champion_center_error_c"),
            "causal_signals": sorted(reasons),
            "signals_that_warned_in_correct_direction": helpful,
            "signals_that_were_misleading": misleading,
            "assessment": (
                "causal_warning_available" if helpful
                else "no_directionally_correct_candidate_warning"
            ),
            "uses_post_checkpoint_information": False,
        })
    return diagnostics


def build_d1_evening_export(
    connection,
    *,
    days: int = 30,
    end_date: date | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
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
    models = _model_rows(connection, target_dates, checkpoints)
    tafs = _taf_rows(connection, target_dates)
    records: list[dict[str, Any]] = []
    for row in checkpoints.sort_values("target_date").to_dict("records"):
        target = row["target_date"]
        cutoff = _checkpoint_at(row, target)
        ranked = probability_ranking(row.get("probabilities_json"))
        top = ranked + [(None, None)] * 3
        champion_center = row.get("forecast_c", row.get("final_forecast_c"))
        actual = actuals.get(target)
        actual_bucket = positive_temperature_bucket(actual)
        modal = top[0][0]
        model_history = model_history_features(models, target, cutoff)
        taf_history = _taf_features(tafs, target, cutoff)
        causal_taf_tx = taf_history.get("current_tx_c")
        if causal_taf_tx is None:
            causal_taf_tx = row.get("taf_max_temp_c")
        checkpoint_status = row.get("checkpoint_status")
        reconstructed = _bool(row.get("checkpoint_reconstructed"))
        center_number = pd.to_numeric(champion_center, errors="coerce")
        record: dict[str, Any] = {
            "target_date": target.isoformat(),
            "checkpoint": D1_CHECKPOINT,
            "checkpoint_at": cutoff.isoformat(),
            "captured_at": _safe(row.get("captured_at")),
            "checkpoint_status": checkpoint_status,
            "checkpoint_reconstructed": reconstructed,
            "evidence_class": checkpoint_evidence_class(checkpoint_status, reconstructed),
            "source_evidence_class": row.get("evidence_class"),
            "freshness_status": row.get("freshness_status"),
            "raw_c": row.get("raw_model_mean_c"),
            "weighted_raw_c": row.get("weighted_raw_c"),
            "bias_c": row.get("bias_corrected_c"),
            "live_c": row.get("metar_conditioned_c"),
            "taf_adjusted_center_c": row.get("final_forecast_c"),
            "champion_center_c": champion_center,
            "champion_probabilities": _json(row.get("probabilities_json"), {}),
            "modal_bucket": modal,
            "top1_probability": top[0][1],
            "top2_bucket": top[1][0], "top2_probability": top[1][1],
            "top3_bucket": top[2][0], "top3_probability": top[2][1],
            "top1_top2_gap_pp": (
                (top[0][1] - top[1][1]) * 100
                if top[0][1] is not None and top[1][1] is not None else None
            ),
            "distance_to_nearest_bucket_boundary_c": (
                abs((float(center_number) - math.floor(float(center_number))) - 0.5)
                if pd.notna(center_number) else None
            ),
            "raw_spread_c": row.get("raw_spread_c"),
            "final_spread_c": row.get("spread_c", row.get("final_spread_c")),
            "forecast_confidence": row.get("forecast_confidence"),
            "model_count": row.get("model_count"),
            "expected_model_count": row.get("expected_model_count"),
            "available_model_count": row.get("available_model_count"),
            "fresh_model_count": row.get("fresh_model_count"),
            "used_model_count": row.get("used_model_count"),
            "used_models": _json(row.get("used_models_json"), []),
            "model_history": model_history,
            "consensus_run_trend_c": model_history["consensus_run_trend_c"],
            "taf_bucket": positive_temperature_bucket(causal_taf_tx),
            "taf_history": taf_history,
            "taf_adjustment_c": row.get("taf_adjustment_c"),
            "taf_conflict": _bool(row.get("taf_conflict")),
            "taf_champion_difference_c": (
                float(causal_taf_tx) - float(center_number)
                if causal_taf_tx is not None and pd.notna(center_number)
                else None
            ),
            "stored_metar_actual_c": actual,
            "actual_bucket": actual_bucket,
            "champion_center_error_c": (
                float(center_number) - actual
                if pd.notna(center_number) and actual is not None else None
            ),
            "absolute_error_c": (
                abs(float(center_number) - actual)
                if pd.notna(center_number) and actual is not None else None
            ),
            "bucket_error": (
                modal - actual_bucket
                if modal is not None and actual_bucket is not None else None
            ),
            "aemet_physical_tmax_c": None,
            "market_resolution_actual": None,
            "temp_anchor_adjustment_c": row.get("temp_anchor_adjustment_c"),
            "clear_sky_override_adjustment_c": row.get(
                "clear_sky_override_adjustment_c"
            ),
            "rapid_heat_ramp_active": _bool(row.get("rapid_heat_ramp_active")),
            "regional_cluster_active": _bool(row.get("regional_cluster_active")),
            "persistent_hot_active": _bool(row.get("persistent_hot_active")),
            "phase_vs_amplitude_active": _bool(row.get("phase_vs_amplitude_active")),
            "maritime_advection_active": _bool(row.get("maritime_advection_active")),
            "features_json": row.get("features_json"),
        }
        record["active_regimes"] = active_regimes(record)
        records.append(record)
    challenged = build_d1_walk_forward_challenger(records)
    scheduled = [
        record for record in challenged if record.get("evidence_class") == "scheduled_causal"
    ]
    reconstructed = [
        record for record in challenged
        if record.get("evidence_class") == "reconstructed_research"
    ]
    public_records = [
        {key: value for key, value in record.items() if key != "features_json"}
        for record in challenged
    ]
    return _safe({
        "schema_version": "1.0",
        "application_version": __version__,
        "export_engine_version": EXPORT_ENGINE_VERSION,
        "protected_forecast_baseline": PROTECTED_FORECAST_BASELINE,
        "generated_at": generated.isoformat(),
        "airport": AIRPORT,
        "track": "Track C - D-1 Evening Forecast Improvement",
        "challenger_version": D1_CHALLENGER_VERSION,
        "research_only": RESEARCH_ONLY,
        "automatic_promotion": AUTOMATIC_PROMOTION,
        "requested_final_days": int(days),
        "exported_final_days": len(target_dates),
        "d1_checkpoint_rows": len(public_records),
        "evidence_counts": {
            "scheduled_causal": len(scheduled),
            "reconstructed_research": len(reconstructed),
            "other_research": len(challenged) - len(scheduled) - len(reconstructed),
            "sequential_oos": 0,
            "live_shadow": 0,
        },
        "default_analysis_view": "scheduled_causal_only",
        "records": public_records,
        "comparison": compare_d1_challenger(scheduled),
        "sensitivity_comparison_reconstructed": compare_d1_challenger(reconstructed),
        "regime_research": regime_matrix_views(challenged),
        "error_avoidance_analysis": _error_avoidance_analysis(scheduled),
        "data_availability": _availability(challenged),
        "transfer_policy": {
            "production_access": "read_only_transaction",
            "full_table_scans": False,
            "automatic_backfill": False,
            "maximum_days": MAX_EXPORT_DAYS,
            "maximum_d1_checkpoint_rows": MAX_D1_CHECKPOINT_ROWS,
            "maximum_model_source_rows": MAX_MODEL_SOURCE_ROWS,
            "maximum_taf_source_rows": MAX_TAF_SOURCE_ROWS,
            "effective_d1_checkpoint_row_cap": min(
                MAX_D1_CHECKPOINT_ROWS, len(target_dates)
            ),
            "effective_model_source_row_cap": min(
                MAX_MODEL_SOURCE_ROWS,
                len(target_dates) * MAX_MODELS_PER_CHECKPOINT * 4,
            ),
            "effective_taf_source_row_cap": min(
                MAX_TAF_SOURCE_ROWS, len(target_dates) * 8
            ),
            "raw_model_grids_exported": False,
            "raw_taf_exported": False,
            "aemet_read_from_neon": False,
        },
    })
