from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class ModelFreshnessProfile:
    update_interval_minutes: int
    publication_tolerance_minutes: int


@dataclass(frozen=True)
class ModelFreshnessAssessment:
    status: str
    usable: bool
    age_minutes: float
    reference_at: datetime
    reference_kind: str
    next_expected_at: datetime
    update_interval_minutes: int
    publication_tolerance_minutes: int
    expected_updates_missed: int


# Operational update intervals documented by Open-Meteo for the exact model
# families used by Weatherman. The tolerance covers normal upstream publication
# and eventual-consistency delay; it is not added repeatedly for missed cycles.
_SIX_HOURLY = ModelFreshnessProfile(360, 90)
_THREE_HOURLY = ModelFreshnessProfile(180, 90)
_HOURLY = ModelFreshnessProfile(60, 45)
_METEOBLUE = ModelFreshnessProfile(360, 120)


def model_freshness_profile(
    model: str,
    *,
    fallback_interval_minutes: int = 90,
) -> ModelFreshnessProfile:
    """Return the expected production cadence for one stored model name."""
    name = str(model).strip().casefold()
    if "meteoblue" in name:
        # mLM integrates models with different cycles. Six hours plus a wider
        # publication window is deliberately conservative without allowing a
        # prior-day response to remain a production input.
        return _METEOBLUE
    if "harmonie_arome_europe" in name or "harmonie_arome_netherlands" in name:
        return _HOURLY
    if "arome" in name or name == "icon_eu" or "icon-eu" in name:
        return _THREE_HOURLY
    if any(
        family in name
        for family in ("ecmwf", "ifs", "gfs", "icon", "ukmo", "arpege")
    ):
        return _SIX_HOURLY
    interval = max(1, int(fallback_interval_minutes))
    return ModelFreshnessProfile(interval, min(90, max(30, interval // 2)))


def _utc(value: object) -> datetime | None:
    if value is None:
        return None
    converter = getattr(value, "to_pydatetime", None)
    if callable(converter):
        value = converter()
    if not isinstance(value, datetime):
        return None
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def assess_model_freshness(
    model: str,
    *,
    as_of: datetime,
    available_at: object = None,
    fetched_at: object = None,
    run_at: object = None,
    fallback_interval_minutes: int = 90,
) -> ModelFreshnessAssessment | None:
    """Classify the latest causal run against its model-specific update cycle."""
    reference_at = None
    reference_kind = ""
    for kind, value in (
        ("provider_available_at", available_at),
        ("fetched_at", fetched_at),
        ("stored_run_at", run_at),
    ):
        parsed = _utc(value)
        if parsed is not None:
            reference_at = parsed
            reference_kind = kind
            break
    if reference_at is None:
        return None

    current = _utc(as_of)
    if current is None:
        raise ValueError("as_of must be a datetime")
    profile = model_freshness_profile(
        model,
        fallback_interval_minutes=fallback_interval_minutes,
    )
    age_minutes = max(0.0, (current - reference_at).total_seconds() / 60)
    cadence = profile.update_interval_minutes
    tolerance = profile.publication_tolerance_minutes
    next_expected_at = reference_at + timedelta(minutes=cadence)

    if age_minutes <= cadence:
        status = "current_latest_run"
        usable = True
        missed = 0
    elif age_minutes <= cadence + tolerance:
        status = "awaiting_next_run"
        usable = True
        missed = 0
    elif age_minutes <= 2 * cadence + tolerance:
        status = "missing_expected_run"
        usable = False
        missed = 1
    else:
        status = "hard_stale"
        usable = False
        missed = max(2, int((age_minutes - tolerance) // cadence))

    return ModelFreshnessAssessment(
        status=status,
        usable=usable,
        age_minutes=age_minutes,
        reference_at=reference_at,
        reference_kind=reference_kind,
        next_expected_at=next_expected_at,
        update_interval_minutes=cadence,
        publication_tolerance_minutes=tolerance,
        expected_updates_missed=missed,
    )
