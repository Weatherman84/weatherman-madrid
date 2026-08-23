from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def post_peak_diagnostic(nowcast: Any, captured_at: datetime) -> dict[str, object]:
    """Describe a soft research gate without changing production probabilities.

    The diagnostic records the repeated failure mode where radiation is the only
    remaining reason for an open upper tail.  It never changes the Champion, the
    day status, OOS counters, or promotion state.
    """
    observed_max = getattr(nowcast, "observed_max", None)
    probabilities = dict(getattr(nowcast, "probabilities", {}) or {})
    upper_tail_probability = (
        sum(float(probability) for bucket, probability in probabilities.items() if bucket > observed_max)
        if observed_max is not None
        else None
    )
    peak_at = _utc(getattr(nowcast, "expected_peak_at", None))
    captured = _utc(captured_at) or captured_at
    minutes_since_peak = (
        max(0.0, (captured - peak_at).total_seconds() / 60.0)
        if peak_at is not None and captured >= peak_at
        else None
    )
    remaining_rise = getattr(nowcast, "remaining_rise_c", None)
    heating_rate = getattr(nowcast, "heating_rate", None)
    radiation = getattr(nowcast, "future_radiation_max", None)
    status = getattr(nowcast, "day_status", None)
    features = dict(getattr(nowcast, "live_features", {}) or {})
    post_peak = minutes_since_peak is not None
    no_model_rise = remaining_rise is not None and float(remaining_rise) <= 0.1
    non_heating = heating_rate is not None and float(heating_rate) <= 0.2
    radiation_only_candidate = bool(
        post_peak
        and no_model_rise
        and non_heating
        and radiation is not None
        and float(radiation) > 50.0
        and status is not None
        and not bool(getattr(status, "is_locked", False))
    )
    return {
        "research_only": True,
        "production_changed": False,
        "captured_at": captured.isoformat(),
        "minutes_since_model_peak": minutes_since_peak,
        "remaining_model_rise_c": remaining_rise,
        "heating_rate_c_per_hour": heating_rate,
        "future_radiation_max_wm2": radiation,
        "observed_max_c": observed_max,
        "upper_tail_probability": upper_tail_probability,
        "day_phase": getattr(status, "phase", None),
        "day_status": getattr(status, "label", None),
        "radiation_only_candidate": radiation_only_candidate,
        "post_convective_active": bool(features.get("post_convective_uncertainty_active", 0)),
        "reheating_watch": bool(
            features.get("post_rain_reheating_watch", 0)
            or features.get("future_reheating_watch", 0)
        ),
    }
