"""Read-only cockpit projections. Forecast calculations and collectors stay untouched."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy import Date, DateTime, select

from .analytics import fixed_checkpoint_reliability
from .actual_quality import settlement_grade_actuals
from .db import (
    DailyActual, Forecast, ForecastSnapshot, ForecastVariantSnapshot, HourlyForecast,
    MarketSnapshot, Observation, ProviderCall, Session, SignalSnapshot, TafReport,
)

# Every column read by checkpoint_rows, fixed_checkpoint_reliability and
# enrich_nowcast_with_regime_memory. features_json is essential analog evidence.
SNAPSHOT_COLUMNS = (
    "airport", "target_date", "captured_at", "final_forecast_c", "features_json",
    "day_phase", "hours_to_peak", "taf_adjustment_c", "taf_max_temp_c", "taf_conflict",
    "raw_spread_c", "final_spread_c",
    "temp_anchor_adjustment_c", "checkpoint_label", "checkpoint_recorded_at",
    "checkpoint_reconstructed", "checkpoint_status", "latest_metar_at", "freshness_status",
    "cloud_adjustment_c", "radiation_adjustment_c", "wind_adjustment_c",
    "late_dry_mixing_adjustment_c", "failed_convection_adjustment_c",
    "clear_sky_override_adjustment_c", "rapid_heat_ramp_active",
    "regional_cluster_active", "persistent_hot_active", "phase_vs_amplitude_active",
    "maritime_advection_active",
)
VARIANT_COLUMNS = (
    "airport", "target_date", "captured_at", "timing", "variant", "factor",
    "forecast_c", "probabilities_json",
)


def local_day_bounds(target, zone: str = "Europe/Madrid"):
    """Half-open local calendar day, including 23/25-hour DST transition days."""
    start = datetime.combine(target, datetime.min.time(), ZoneInfo(zone))
    end = datetime.combine(target + timedelta(days=1), datetime.min.time(), ZoneInfo(zone))
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def read_projection(bind, model, *conditions, columns=None):
    names = columns or tuple(column.name for column in model.__table__.columns)
    statement = select(*(getattr(model, name) for name in names)).where(*conditions)
    frame = pd.read_sql(statement, bind)
    for name in names:
        kind = model.__table__.columns[name].type
        if isinstance(kind, DateTime):
            frame[name] = pd.to_datetime(frame[name], utc=True, errors="coerce")
        elif isinstance(kind, Date):
            frame[name] = pd.to_datetime(frame[name], errors="coerce").dt.date
    return frame


def _stamp(data):
    loaded = datetime.now(timezone.utc).isoformat()
    for frame in data.values():
        frame.attrs["loaded_at"] = loaded
    return data


@st.cache_data(ttl=300, max_entries=8, show_spinner=False)
def load_madrid_data(target, airport: str, zone: str):
    start, end = local_day_bounds(target, zone)
    with Session() as session:
        bind = session.connection()

        def read(model, *conditions, columns=None):
            return read_projection(bind, model, model.airport == airport,
                                   *conditions, columns=columns)

        return _stamp({
            "forecasts": read(Forecast, Forecast.target_date == target),
            # Two prior days remain available to post-convective / nowcast logic.
            "observations": read(Observation, Observation.observed_at >= start - timedelta(days=2),
                                 Observation.observed_at < end),
            "hourly": read(HourlyForecast, HourlyForecast.valid_at >= start,
                           HourlyForecast.valid_at <= end),
            "markets": read(MarketSnapshot, MarketSnapshot.target_date == target),
            "signals": read(SignalSnapshot, SignalSnapshot.target_date == target,
                            columns=("target_date", "captured_at", "bucket_label", "model_probability")),
            "snapshots": read(ForecastSnapshot, ForecastSnapshot.target_date == target,
                              columns=SNAPSHOT_COLUMNS),
            "variants": read(ForecastVariantSnapshot, ForecastVariantSnapshot.target_date == target,
                             columns=VARIANT_COLUMNS),
            "tafs": read(TafReport, TafReport.issue_time >= start - timedelta(days=2)),
        })


@st.cache_data(ttl=300, max_entries=8, show_spinner=False)
def load_madrid_history(target, airport: str):
    with Session() as session:
        bind = session.connection()

        def read(model, days, *, columns=None, conditions=()):
            return read_projection(
                bind, model, model.airport == airport,
                model.target_date >= target - timedelta(days=days),
                model.target_date < target, *conditions, columns=columns,
            )

        return _stamp({
            # Earlier non-D1 runs are not used by build_live_nowcast. Keep the
            # entire 90-day D1 cohort; selection/calibration remains in the engine.
            "forecasts": read(Forecast, 90, conditions=(Forecast.horizon == "D-1",)),
            "actuals": read_projection(bind, DailyActual, DailyActual.airport == airport,
                                       DailyActual.target_date >= target - timedelta(days=400)),
            "snapshots": read(ForecastSnapshot, 120, columns=SNAPSHOT_COLUMNS),
            "variants": read(ForecastVariantSnapshot, 90, columns=VARIANT_COLUMNS),
        })


def combine_cockpit_data(current, history):
    result = dict(current)
    for name, frame in history.items():
        if name in result:
            frames = [part for part in (frame, result[name]) if not part.empty]
            result[name] = pd.concat(frames, ignore_index=True) if frames else frame.copy()
        else:
            result[name] = frame
    return result


def temperature_bucket(value):
    """Round positive temperature centers explicitly, without bankers rounding."""
    parsed = pd.to_numeric(value, errors="coerce")
    return math.floor(float(parsed) + 0.5) if pd.notna(parsed) else None


def champion_bucket_summary(probabilities_json):
    """Return modal and runner-up buckets from the stored Champion distribution."""
    try:
        payload = (
            json.loads(probabilities_json)
            if isinstance(probabilities_json, str)
            else probabilities_json
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    values = []
    for bucket, probability in payload.items():
        bucket_value = pd.to_numeric(bucket, errors="coerce")
        probability_value = pd.to_numeric(probability, errors="coerce")
        if pd.notna(bucket_value) and pd.notna(probability_value):
            values.append((int(bucket_value), float(probability_value)))
    if not values:
        return None
    values.sort(key=lambda item: (-item[1], item[0]))
    modal = values[0]
    runner_up = values[1] if len(values) > 1 else None
    return {
        "modal_bucket": modal[0],
        "modal_probability": modal[1],
        "runner_up_bucket": runner_up[0] if runner_up else None,
        "runner_up_probability": runner_up[1] if runner_up else None,
        "top_gap_pp": round((modal[1] - runner_up[1]) * 100, 10) if runner_up else None,
    }


def champion_variant_lookup(variants):
    """Index the persisted Champion distributions by their immutable capture time."""
    if variants.empty:
        return {}
    frame = variants[variants.variant.astype(str).eq("Champion")].copy()
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    frame = frame.dropna(subset=["captured_at"]).sort_values("captured_at")
    return {
        (str(row.airport), row.target_date, pd.Timestamp(row.captured_at)): (
            champion_bucket_summary(row.probabilities_json)
        )
        for row in frame.itertuples()
    }


@st.cache_data(ttl=300, max_entries=8, show_spinner=False)
def checkpoint_reliability(snapshots, actuals, variants):
    """Cache compact metrics from already loaded evidence; no additional SQL."""
    result = fixed_checkpoint_reliability(snapshots, actuals).copy()
    result["center_bucket_hit"] = result["exact_bucket"]
    result["modal_bucket_hit"] = None
    result["modal_bucket_n"] = 0
    if snapshots.empty or variants.empty or actuals.empty:
        return result

    labels = set(result.checkpoint.astype(str))
    selected = snapshots[snapshots.checkpoint_label.astype(str).isin(labels)].copy()
    selected["target_date"] = pd.to_datetime(
        selected.target_date, errors="coerce"
    ).dt.date
    selected["captured_at"] = pd.to_datetime(
        selected.captured_at, utc=True, errors="coerce"
    )
    selected = selected.sort_values("captured_at").drop_duplicates(
        ["airport", "target_date", "checkpoint_label"], keep="last"
    )
    reconstructed = selected.checkpoint_status.fillna("").astype(str).str.contains(
        "reconstructed", case=False
    ) | selected.checkpoint_reconstructed.fillna(False).astype(bool)
    scheduled = selected.checkpoint_status.fillna("").astype(str).str.contains(
        "scheduled", case=False
    )
    live = selected.checkpoint_label.astype(str).str.startswith(("First Live", "Late Live"))
    post_peak = pd.to_numeric(selected.hours_to_peak, errors="coerce").lt(0)
    selected = selected[scheduled & ~reconstructed & ~(live & post_peak)].copy()

    champions = variants[variants.variant.astype(str).eq("Champion")].copy()
    champions["target_date"] = pd.to_datetime(
        champions.target_date, errors="coerce"
    ).dt.date
    champions["captured_at"] = pd.to_datetime(
        champions.captured_at, utc=True, errors="coerce"
    )
    champions = champions.sort_values("captured_at").drop_duplicates(
        ["airport", "target_date", "captured_at"], keep="last"
    )
    selected = selected.merge(
        champions[["airport", "target_date", "captured_at", "probabilities_json"]],
        on=["airport", "target_date", "captured_at"],
        how="left",
    )
    final_actuals = settlement_grade_actuals(actuals).copy()
    if final_actuals.empty:
        return result
    final_actuals["target_date"] = pd.to_datetime(
        final_actuals.target_date, errors="coerce"
    ).dt.date
    final_actuals["actual_bucket"] = final_actuals.max_temp_c.map(temperature_bucket)
    scored = selected.merge(
        final_actuals[["airport", "target_date", "actual_bucket"]],
        on=["airport", "target_date"],
        how="inner",
    )
    scored["modal_bucket"] = scored.probabilities_json.map(
        lambda value: (champion_bucket_summary(value) or {}).get("modal_bucket")
    )
    scored = scored.dropna(subset=["modal_bucket", "actual_bucket"])
    for checkpoint, group in scored.groupby("checkpoint_label"):
        mask = result.checkpoint.eq(checkpoint)
        result.loc[mask, "modal_bucket_hit"] = float(
            (group.modal_bucket.astype(int) == group.actual_bucket.astype(int)).mean()
        )
        result.loc[mask, "modal_bucket_n"] = int(len(group))
    return result


@st.cache_data(ttl=300, max_entries=8, show_spinner=False)
def load_cockpit_details(target, airport: str, zone: str, group: str):
    start, _ = local_day_bounds(target, zone)
    with Session() as session:
        bind = session.connection()
        if group == "providers":
            return _stamp({"provider_calls": read_projection(
                bind, ProviderCall, ProviderCall.airport == airport,
                ProviderCall.attempted_at >= start - timedelta(days=1),
                columns=("checkpoint_label", "attempted_at", "status", "rows_written"),
            )})
        if group != "history":
            raise ValueError("Unknown cockpit detail group")
        return _stamp({model.__tablename__: read_projection(
            bind, model, model.airport == airport,
            model.target_date >= target - timedelta(days=days),
        ) for model, days in ((ForecastSnapshot, 120), (ForecastVariantSnapshot, 90))})


def invalidate_cockpit_cache(target, airport: str, zone: str):
    """Invalidate the refreshed target, leaving other dates and AEMET untouched."""
    load_madrid_data.clear(target, airport, zone)
    load_madrid_history.clear(target, airport)
    for group in ("providers", "history"):
        load_cockpit_details.clear(target, airport, zone, group)


def transfer_diagnostics(**groups):
    """Rows and in-memory payload estimates, not measured Neon wire traffic."""
    return pd.DataFrame([
        {"Group": group, "Dataset": name, "Rows": len(frame), "Columns": len(frame.columns),
         "Approx. in-memory KiB": round(frame.memory_usage(deep=True).sum() / 1024, 1),
         "Loaded at UTC": frame.attrs.get("loaded_at", "—")}
        for group, data in groups.items() for name, frame in data.items()
    ])
