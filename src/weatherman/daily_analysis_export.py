from __future__ import annotations

import json
import os
import hashlib
import re
import tempfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from . import __version__
from .db import (
    CollectionCoverage,
    CollectionRun,
    DailyActual,
    Forecast,
    ForecastSnapshot,
    ForecastVariantSnapshot,
    Observation,
    ProviderCall,
    RegimeMemorySnapshot,
    TafReport,
)


AIRPORT = "LEMD"
AIRPORT_TIMEZONE = "Europe/Madrid"
ENGINE_BASELINE = "v10.7.10"
EXPORT_SCHEMA_VERSION = "1.0"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()
    return str(value)


def _json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _captured_key(value: datetime | None) -> str | None:
    return _iso(value)


def _public_ref(value: str) -> str:
    """Create a stable join key without exposing an internal run identifier."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _safe_error_text(value: str | None) -> str | None:
    """Retain useful provider diagnostics while removing query-string secrets."""
    if value is None:
        return None
    return re.sub(
        r"(?i)(apikey|api_key|access_token|token|password)=([^&\s\"']+)",
        r"\1=REDACTED",
        str(value),
    )


def assert_export_safe(payload: dict[str, Any]) -> None:
    """Fail closed if a database credential could enter the public artifact."""
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    lowered = serialized.lower()
    forbidden_keys = (
        '"database_url"',
        '"password"',
        '"api_key"',
        '"access_token"',
        '"secret"',
    )
    if any(marker in lowered for marker in forbidden_keys):
        raise ValueError("Public export contains a forbidden credential field")
    if re.search(
        r"(?i)(apikey|api_key|access_token|token|password)=((?!REDACTED)[^&\s\"']+)",
        serialized,
    ):
        raise ValueError("Public export contains an unredacted query credential")

    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        return
    parsed = urlsplit(database_url)
    sensitive_values = [database_url, parsed.password]
    if any(value and len(value) >= 8 and value in serialized for value in sensitive_values):
        raise ValueError("Public export contains database credentials")


def _actual_payload(row: DailyActual) -> dict[str, Any]:
    return {
        "target_date": row.target_date.isoformat(),
        "max_temp_c": row.max_temp_c,
        "source": row.source,
        "is_final_station_actual": row.source == "stored-metar-station",
    }


def _observation_payload(row: Observation) -> dict[str, Any]:
    return {
        "observed_at": _iso(row.observed_at),
        "temp_c": row.temp_c,
        "dewpoint_c": row.dewpoint_c,
        "wind_kph": row.wind_kph,
        "wind_direction": row.wind_direction,
        "cloud_cover": row.cloud_cover,
        "cloud_base_ft": row.cloud_base_ft,
        "raw_metar": row.raw,
    }


def _forecast_payload(row: Forecast) -> dict[str, Any]:
    return {
        "target_date": row.target_date.isoformat(),
        "model": row.model,
        "max_temp_c": row.max_temp_c,
        "source": row.source,
        "horizon": row.horizon,
        "run_at": _iso(row.run_at),
        "model_run_at": _iso(row.model_run_at),
        "available_at": _iso(row.available_at),
        "fetched_at": _iso(row.fetched_at),
        "provenance_status": row.provenance_status,
    }


def _taf_payload(row: TafReport) -> dict[str, Any]:
    return {
        "target_local_date": _iso(row.target_local_date),
        "issue_time": _iso(row.issue_time),
        "bulletin_time": _iso(row.bulletin_time),
        "first_seen_at": _iso(row.first_seen_at),
        "fetched_at": _iso(row.fetched_at),
        "valid_from": _iso(row.valid_from),
        "valid_to": _iso(row.valid_to),
        "max_temp_c": row.max_temp_c,
        "max_temp_at": _iso(row.max_temp_at),
        "is_amended": row.is_amended,
        "is_corrected": row.is_corrected,
        "coverage_status": row.coverage_status,
        "content_hash": row.content_hash,
        "revision_of_hash": row.revision_of_hash,
        "raw_taf": row.raw_taf,
    }


def _variant_payload(row: ForecastVariantSnapshot) -> dict[str, Any]:
    return {
        "variant": row.variant,
        "factor": row.factor,
        "forecast_c": row.forecast_c,
        "spread_c": row.spread_c,
        "bucket_probabilities": _json_value(row.probabilities_json, {}),
        "forecast_confidence": row.forecast_confidence,
        "day_phase": row.day_phase,
    }


def _regime_memory_payload(row: RegimeMemorySnapshot) -> dict[str, Any]:
    return {
        "status": row.status,
        "label": row.label,
        "confidence": row.confidence,
        "analog_count": row.analog_count,
        "best_similarity": row.best_similarity,
        "center_adjustment_c": row.center_adjustment_c,
        "suggested_forecast_c": row.suggested_forecast_c,
        "suggested_spread_c": row.suggested_spread_c,
        "shadow_only": row.shadow_only,
        "applied_to_champion": row.applied_to_champion,
        "promotion_status": row.promotion_status,
        "promotion_eligible": row.promotion_eligible,
        "oos_days": row.oos_days,
        "regimes": _json_value(row.regimes_json, []),
        "analogs": _json_value(row.analogs_json, []),
        "pro_signals": _json_value(row.pro_signals_json, []),
        "contra_signals": _json_value(row.contra_signals_json, []),
        "feature_signature": _json_value(row.feature_signature_json, {}),
        "explanation": row.explanation,
    }


def _checkpoint_payload(
    row: ForecastSnapshot,
    *,
    variants: list[dict[str, Any]],
    regime_memory: dict[str, Any] | None,
) -> dict[str, Any]:
    adjustments = {
        "temperature_anchor_c": row.temp_anchor_adjustment_c,
        "dryness_c": row.dryness_adjustment_c,
        "dewpoint_trend_c": row.dewpoint_trend_adjustment_c,
        "cloud_c": row.cloud_adjustment_c,
        "heating_rate_c": row.heating_rate_adjustment_c,
        "recent_station_error_c": row.recent_error_adjustment_c,
        "radiation_c": row.radiation_adjustment_c,
        "wind_c": row.wind_adjustment_c,
        "run_trend_c": row.run_trend_adjustment_c,
        "late_dry_mixing_c": row.late_dry_mixing_adjustment_c,
        "failed_convection_c": row.failed_convection_adjustment_c,
        "clear_sky_override_c": row.clear_sky_override_adjustment_c,
        "rapid_heat_ramp_c": row.rapid_heat_ramp_adjustment_c,
        "regional_cluster_c": row.regional_cluster_adjustment_c,
        "persistent_hot_c": row.persistent_hot_adjustment_c,
        "phase_anchor_delta_c": row.phase_anchor_delta_c,
        "maritime_advection_c": row.maritime_advection_adjustment_c,
        "taf_c": row.taf_adjustment_c,
        "live_total_c": row.live_adjustment_c,
    }
    regimes = {
        "rapid_heat_ramp_active": row.rapid_heat_ramp_active,
        "regional_cluster_active": row.regional_cluster_active,
        "persistent_hot_active": row.persistent_hot_active,
        "phase_vs_amplitude_active": row.phase_vs_amplitude_active,
        "maritime_advection_active": row.maritime_advection_active,
        "maritime_low_range_active": row.maritime_low_range_active,
        "post_convective_active": row.post_convective_active,
        "post_convective_reports": row.post_convective_reports,
        "post_convective_spread_multiplier": row.post_convective_spread_multiplier,
        "model_ceiling_reached_early": row.model_ceiling_reached_early,
    }
    return {
        "target_date": row.target_date.isoformat(),
        "checkpoint": row.checkpoint_label,
        "checkpoint_at": _iso(row.checkpoint_at),
        "recorded_at": _iso(row.checkpoint_recorded_at),
        "captured_at": _iso(row.captured_at),
        "source_captured_at": _iso(row.source_captured_at),
        "checkpoint_gap_minutes": row.checkpoint_gap_minutes,
        "checkpoint_reconstructed": row.checkpoint_reconstructed,
        "checkpoint_status": row.checkpoint_status,
        "evidence_class": row.evidence_class,
        "freshness_status": row.freshness_status,
        "source_age_minutes": {
            "min": row.source_age_min_minutes,
            "median": row.source_age_median_minutes,
            "max": row.source_age_max_minutes,
        },
        "model_counts": {
            "expected": row.expected_model_count,
            "available": row.available_model_count,
            "fresh": row.fresh_model_count,
            "used": row.used_model_count,
        },
        "source_coverage_ratio": row.source_coverage_ratio,
        "expected_models": _json_value(row.expected_models_json, []),
        "available_models": _json_value(row.available_models_json, []),
        "used_models": _json_value(row.used_models_json, []),
        "extra_models": _json_value(row.extra_models_json, []),
        "source_provenance": _json_value(row.source_provenance_json, []),
        "forecast_chain_c": {
            "raw_ensemble": row.raw_model_mean_c,
            "weighted_raw": row.weighted_raw_c,
            "bias_corrected_equal": row.bias_corrected_equal_c,
            "bias_corrected": row.bias_corrected_c,
            "live_weather_adjusted": row.metar_conditioned_c,
            "champion": row.final_forecast_c,
        },
        "forecast_spreads_c": {
            "raw_ensemble": row.raw_spread_c,
            "weighted_raw": row.weighted_raw_spread_c,
            "bias_corrected_equal": row.bias_corrected_equal_spread_c,
            "bias_corrected": row.bias_corrected_spread_c,
            "live_weather_adjusted": row.metar_conditioned_spread_c,
            "champion": row.final_spread_c,
        },
        "observed_max_c": row.observed_max_c,
        "latest_metar_at": _iso(row.latest_metar_at),
        "expected_peak_at": _iso(row.expected_peak_at),
        "hours_to_peak": row.hours_to_peak,
        "day_phase": row.day_phase,
        "taf": {
            "issue_time": _iso(row.taf_issue_time),
            "first_seen_at": _iso(row.taf_first_seen_at),
            "max_temp_c": row.taf_max_temp_c,
            "content_hash": row.taf_content_hash,
            "adjustment_c": row.taf_adjustment_c,
            "conflict": row.taf_conflict,
            "pre_taf_modal_bucket_c": row.pre_taf_modal_bucket_c,
            "champion_modal_bucket_c": row.champion_modal_bucket_c,
            "modal_bucket_flip": row.taf_modal_bucket_flip,
        },
        "forecast_drivers": _json_value(row.features_json, {}),
        "adjustment_impacts": adjustments,
        "regime_flags": regimes,
        "regime_memory": regime_memory,
        "champion_and_challengers": variants,
        "peak_lock": _json_value(row.peak_lock_json, {}),
        "post_peak_diagnostic": _json_value(row.post_peak_diagnostic_json, {}),
        "market_snapshot": {
            "status": row.market_snapshot_status,
            "captured_at": _iso(row.market_snapshot_at),
            "bucket_count": row.market_bucket_count,
        },
    }


def build_daily_analysis_export(
    session,
    *,
    generated_at: datetime | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Build a credential-free, Madrid-only research view from production data."""
    generated = generated_at or datetime.now(timezone.utc)
    generated = generated if generated.tzinfo else generated.replace(tzinfo=timezone.utc)
    local_today = generated.astimezone(ZoneInfo(AIRPORT_TIMEZONE)).date()
    safe_days = max(1, min(31, int(days)))
    first_target = local_today - timedelta(days=safe_days - 1)
    last_target = local_today + timedelta(days=1)
    first_observation = datetime.combine(
        first_target,
        time.min,
        tzinfo=ZoneInfo(AIRPORT_TIMEZONE),
    ).astimezone(timezone.utc)

    checkpoint_rows = list(
        session.scalars(
            select(ForecastSnapshot)
            .where(
                ForecastSnapshot.airport == AIRPORT,
                ForecastSnapshot.target_date >= first_target,
                ForecastSnapshot.target_date <= last_target,
                ForecastSnapshot.checkpoint_label.is_not(None),
            )
            .order_by(ForecastSnapshot.checkpoint_at, ForecastSnapshot.captured_at)
        )
    )
    variants_by_capture: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in session.scalars(
        select(ForecastVariantSnapshot).where(
            ForecastVariantSnapshot.airport == AIRPORT,
            ForecastVariantSnapshot.target_date >= first_target,
            ForecastVariantSnapshot.target_date <= last_target,
        )
    ):
        key = _captured_key(row.captured_at)
        if key is not None:
            variants_by_capture[key].append(_variant_payload(row))
    regimes_by_capture: dict[str, dict[str, Any]] = {}
    for row in session.scalars(
        select(RegimeMemorySnapshot).where(
            RegimeMemorySnapshot.airport == AIRPORT,
            RegimeMemorySnapshot.target_date >= first_target,
            RegimeMemorySnapshot.target_date <= last_target,
        )
    ):
        key = _captured_key(row.captured_at)
        if key is not None:
            regimes_by_capture[key] = _regime_memory_payload(row)

    actuals = list(
        session.scalars(
            select(DailyActual)
            .where(
                DailyActual.airport == AIRPORT,
                DailyActual.target_date >= first_target,
                DailyActual.target_date <= local_today,
            )
            .order_by(DailyActual.target_date)
        )
    )
    observations = list(
        session.scalars(
            select(Observation)
            .where(
                Observation.airport == AIRPORT,
                Observation.observed_at >= first_observation,
                Observation.observed_at <= generated.astimezone(timezone.utc),
            )
            .order_by(Observation.observed_at)
        )
    )
    forecasts = list(
        session.scalars(
            select(Forecast)
            .where(
                Forecast.airport == AIRPORT,
                Forecast.target_date >= first_target,
                Forecast.target_date <= last_target,
            )
            .order_by(Forecast.target_date, Forecast.model, Forecast.run_at)
        )
    )
    tafs = list(
        session.scalars(
            select(TafReport)
            .where(
                TafReport.airport == AIRPORT,
                TafReport.target_local_date >= first_target,
                TafReport.target_local_date <= last_target,
            )
            .order_by(TafReport.issue_time)
        )
    )
    provider_calls = list(
        session.scalars(
            select(ProviderCall)
            .where(
                ProviderCall.airport == AIRPORT,
                ProviderCall.local_date >= first_target,
                ProviderCall.local_date <= local_today,
            )
            .order_by(ProviderCall.attempted_at)
        )
    )
    collection_runs = list(
        session.scalars(
            select(CollectionRun)
            .where(CollectionRun.started_at >= first_observation)
            .order_by(CollectionRun.started_at)
        )
    )
    run_ids = [row.run_id for row in collection_runs]
    coverage_rows = (
        list(
            session.scalars(
                select(CollectionCoverage)
                .where(
                    CollectionCoverage.run_id.in_(run_ids),
                    CollectionCoverage.airport == AIRPORT,
                )
                .order_by(CollectionCoverage.scheduled_at)
            )
        )
        if run_ids
        else []
    )

    checkpoints = []
    for row in checkpoint_rows:
        key = _captured_key(row.captured_at)
        variants = sorted(
            variants_by_capture.get(key or "", []),
            key=lambda item: str(item["variant"]),
        )
        checkpoints.append(
            _checkpoint_payload(
                row,
                variants=variants,
                regime_memory=regimes_by_capture.get(key or ""),
            )
        )

    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "generated_at": _iso(generated),
        "airport": AIRPORT,
        "airport_timezone": AIRPORT_TIMEZONE,
        "application_version": __version__,
        "forecast_engine_baseline": ENGINE_BASELINE,
        "classification": "READ-ONLY DAILY ANALYSIS EXPORT",
        "research_only": True,
        "contains_credentials": False,
        "writes_production_database": False,
        "window": {
            "first_target_date": first_target.isoformat(),
            "last_target_date": last_target.isoformat(),
            "days_requested": safe_days,
        },
        "actuals": [_actual_payload(row) for row in actuals],
        "observations": [_observation_payload(row) for row in observations],
        "model_forecasts": [_forecast_payload(row) for row in forecasts],
        "taf_revision_journal": [_taf_payload(row) for row in tafs],
        "checkpoints": checkpoints,
        "provider_calls": [
            {
                "local_date": row.local_date.isoformat(),
                "provider": row.provider,
                "checkpoint": row.checkpoint_label,
                "target_at": _iso(row.target_at),
                "attempted_at": _iso(row.attempted_at),
                "status": row.status,
                "rows_written": row.rows_written,
                "reason": _safe_error_text(row.reason),
            }
            for row in provider_calls
        ],
        "collector_runs": [
            {
                "run_ref": _public_ref(row.run_id),
                "scheduled_at": _iso(row.scheduled_at),
                "event_created_at": _iso(row.event_created_at),
                "queue_started_at": _iso(row.queue_started_at),
                "started_at": _iso(row.started_at),
                "ended_at": _iso(row.ended_at),
                "trigger": row.trigger,
                "overall_status": row.overall_status,
                "scheduler_drift_seconds": row.scheduler_drift_seconds,
                "trigger_delay_seconds": row.trigger_delay_seconds,
                "queue_delay_seconds": row.queue_delay_seconds,
                "execution_seconds": row.execution_seconds,
                "persistence_status": row.persistence_status,
                "error_reason": _safe_error_text(row.error_reason),
            }
            for row in collection_runs
        ],
        "collector_coverage": [
            {
                "run_ref": _public_ref(row.run_id),
                "data_type": row.data_type,
                "status": row.status,
                "scheduled_at": _iso(row.scheduled_at),
                "latest_source_at": _iso(row.latest_source_at),
                "rows_read": row.rows_read,
                "rows_written": row.rows_written,
                "source_age_minutes": row.source_age_minutes,
                "duration_seconds": row.duration_seconds,
                "attempts": row.attempts,
                "metrics": _json_value(row.metrics_json, {}),
                "reason": _safe_error_text(row.reason),
            }
            for row in coverage_rows
        ],
    }


def write_export(path: Path, payload: dict[str, Any]) -> None:
    assert_export_safe(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def set_read_only(session) -> None:
    """Make PostgreSQL export transactions explicitly read-only."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION READ ONLY"))
