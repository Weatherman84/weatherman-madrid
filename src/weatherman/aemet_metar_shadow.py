from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd


SHADOW_CALIBRATION_STATUS = "insufficient_oos_data"
SERIES_GAP_ROLE = "series_difference_not_sensor_bias"


def _timestamp(value: Any) -> datetime | None:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).to_pydatetime()


def _number(value: Any) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return float(parsed) if pd.notna(parsed) else None


def _aemet_observations(payload: dict[str, Any]) -> list[tuple[datetime, float]]:
    rows: list[tuple[datetime, float]] = []
    for item in payload.get("observations") or []:
        observed_at = _timestamp(item.get("observed_at"))
        temperature = _number(item.get("temperature_c"))
        if observed_at is not None and temperature is not None:
            rows.append((observed_at, temperature))
    return sorted(set(rows))


def _metar_observations(metars: list[dict[str, Any]] | None) -> list[tuple[datetime, float]]:
    rows: list[tuple[datetime, float]] = []
    for item in metars or []:
        observed_at = _timestamp(item.get("observed_at"))
        temperature = _number(item.get("temp_c"))
        if observed_at is not None and temperature is not None:
            rows.append((observed_at, temperature))
    return sorted(set(rows))


def time_aligned_series_comparisons(
    payload: dict[str, Any],
    metars: list[dict[str, Any]] | None,
    *,
    maximum_gap_minutes: float = 12.0,
) -> list[dict[str, Any]]:
    """Compare nearby timestamps without treating the result as sensor calibration."""

    aemet = _aemet_observations(payload)
    comparisons: list[dict[str, Any]] = []
    for metar_at, metar_c in _metar_observations(metars):
        if not aemet:
            break
        aemet_at, aemet_c = min(
            aemet,
            key=lambda row: abs((row[0] - metar_at).total_seconds()),
        )
        gap_minutes = abs((aemet_at - metar_at).total_seconds()) / 60
        if gap_minutes > maximum_gap_minutes:
            continue
        comparisons.append(
            {
                "metar_observed_at": metar_at.isoformat(),
                "metar_temperature_c": metar_c,
                "aemet_observed_at": aemet_at.isoformat(),
                "aemet_temperature_c": aemet_c,
                "timestamp_gap_minutes": round(gap_minutes, 2),
                "aemet_minus_metar_c": round(aemet_c - metar_c, 2),
                "difference_role": SERIES_GAP_ROLE,
            }
        )
    return comparisons


def ground_truth_comparison(
    payload: dict[str, Any],
    metars: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Keep physical, reported and market-resolution targets strictly separate."""

    metar_rows = _metar_observations(metars)
    metar_max_c = max((temperature for _, temperature in metar_rows), default=None)
    metar_max_times = [
        observed_at.isoformat()
        for observed_at, temperature in metar_rows
        if metar_max_c is not None and temperature == metar_max_c
    ]
    physical = payload.get("physical_tmax") or {}
    physical_max_c = _number(physical.get("value_c"))
    daily_gap = (
        round(physical_max_c - metar_max_c, 2)
        if physical_max_c is not None and metar_max_c is not None
        else None
    )
    return {
        "stored_metar_max": {
            "value_c": metar_max_c,
            "observed_at": metar_max_times,
            "role": "highest_integer_metar_value_in_local_archive",
        },
        "aemet_physical_tmax": {
            "value_c": physical_max_c,
            "observed_at": physical.get("observed_at"),
            "role": "independent_physical_station_maximum",
        },
        "market_resolution_actual": None,
        "market_resolution_status": "unverified-source-and-rounding-rule",
        "daily_max_series_gap_c": daily_gap,
        "daily_max_series_gap_role": SERIES_GAP_ROLE,
        "daily_max_series_gap_warning": (
            "The gap combines possible station, sensor, timing and reporting differences; "
            "it is not a calibrated sensor bias."
        ),
        "time_aligned_series_comparisons": time_aligned_series_comparisons(
            payload,
            metars,
        ),
    }


def physical_stall_shadow(payload: dict[str, Any]) -> dict[str, Any]:
    """Observation-only stall state; deliberately not a calibrated probability."""

    observations = _aemet_observations(payload)
    result: dict[str, Any] = {
        "name": "physical_stall_shadow",
        "research_only": True,
        "calibration_status": SHADOW_CALIBRATION_STATUS,
        "probability": None,
        "champion_impact_c": 0.0,
        "bucket_probability_impact": None,
        "inputs_missing": [
            "remaining_radiation",
            "remaining_model_rise",
            "cloud_evolution",
            "wind_and_gust_evolution",
        ],
    }
    if len(observations) < 3:
        result.update(
            {
                "stall_level": "insufficient_data",
                "interpretation": "Fewer than three physical observations are available.",
            }
        )
        return result

    latest_at = observations[-1][0]
    recent = [row for row in observations if row[0] >= latest_at - timedelta(minutes=60)]
    if len(recent) < 3:
        recent = observations[-3:]
    elapsed_hours = (recent[-1][0] - recent[0][0]).total_seconds() / 3600
    if elapsed_hours <= 0:
        result.update(
            {
                "stall_level": "insufficient_data",
                "interpretation": "Physical observation timestamps do not span time.",
            }
        )
        return result

    rate = (recent[-1][1] - recent[0][1]) / elapsed_hours
    recent_max = max(value for _, value in recent)
    below_recent_max = recent_max - recent[-1][1]
    if rate > 0.25:
        level = "low"
        interpretation = "The physical series is still rising; a stall is not supported."
    elif rate <= 0 and below_recent_max >= 0.2:
        level = "high"
        interpretation = "The physical series has turned down from its recent maximum."
    else:
        level = "medium"
        interpretation = "The physical trend is flat or ambiguous without full weather context."
    result.update(
        {
            "stall_level": level,
            "temperature_rate_c_per_hour": round(rate, 3),
            "window_minutes": round(elapsed_hours * 60, 1),
            "latest_temperature_c": recent[-1][1],
            "latest_observed_at": recent[-1][0].isoformat(),
            "below_recent_max_c": round(below_recent_max, 2),
            "interpretation": interpretation,
        }
    )
    return result


def _next_routine_metar(after: datetime, schedule_minutes: tuple[int, ...]) -> datetime:
    base = after.astimezone(timezone.utc).replace(second=0, microsecond=0)
    candidates = [
        base.replace(minute=minute) + timedelta(hours=hour_offset)
        for hour_offset in range(0, 2)
        for minute in schedule_minutes
    ]
    return min(candidate for candidate in candidates if candidate > after)


def metar_bucket_persistence_shadow(
    payload: dict[str, Any],
    metars: list[dict[str, Any]] | None,
    *,
    schedule_minutes: tuple[int, ...] = (0, 30),
) -> dict[str, Any]:
    """Describe METAR-bucket persistence without inventing a probability."""

    physical = physical_stall_shadow(payload)
    aemet = _aemet_observations(payload)
    metar_rows = _metar_observations(metars)
    result: dict[str, Any] = {
        "name": "metar_bucket_persistence_shadow",
        "research_only": True,
        "calibration_status": SHADOW_CALIBRATION_STATUS,
        "probability": None,
        "champion_impact_c": 0.0,
        "bucket_probability_impact": None,
        "market_resolution_impact": None,
        "physical_stall_level": physical.get("stall_level"),
        "warning": (
            "AEMET 3129 and LEMD METAR may use different stations, sensors, timing or "
            "processing. This signal is not a market-resolution forecast."
        ),
    }
    if not aemet or not metar_rows:
        result.update(
            {
                "persistence_state": "insufficient_data",
                "interpretation": "Both physical and METAR observations are required.",
            }
        )
        return result

    latest_physical_at, latest_physical_c = aemet[-1]
    latest_metar_at, latest_metar_c = metar_rows[-1]
    metar_max_c = max(value for _, value in metar_rows)
    next_metar = _next_routine_metar(
        max(latest_physical_at, latest_metar_at),
        schedule_minutes,
    )
    if physical.get("stall_level") == "low":
        state = "next_metar_bucket_still_physically_possible"
        interpretation = (
            "Physical warming continues, so repeated integer METAR values do not prove a stall."
        )
    elif physical.get("stall_level") == "high":
        state = "current_metar_bucket_persistence_supported"
        interpretation = (
            "The physical series has turned down, which supports persistence of the current "
            "reported METAR maximum but does not determine market resolution."
        )
    else:
        state = "indeterminate"
        interpretation = "The available observations do not resolve METAR-bucket persistence."
    result.update(
        {
            "persistence_state": state,
            "stored_metar_max_c": metar_max_c,
            "latest_metar_temperature_c": latest_metar_c,
            "latest_metar_observed_at": latest_metar_at.isoformat(),
            "latest_aemet_temperature_c": latest_physical_c,
            "latest_aemet_observed_at": latest_physical_at.isoformat(),
            "next_nominal_metar_at": next_metar.isoformat(),
            "routine_metar_schedule_minutes": list(schedule_minutes),
            "interpretation": interpretation,
        }
    )
    return result


def build_shadow_diagnostics(
    payload: dict[str, Any],
    metars: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    return {
        "classification": "RESEARCH-ONLY UNCALIBRATED SHADOW DIAGNOSTICS",
        "ground_truth": ground_truth_comparison(payload, metars),
        "physical_stall": physical_stall_shadow(payload),
        "metar_bucket_persistence": metar_bucket_persistence_shadow(payload, metars),
    }
