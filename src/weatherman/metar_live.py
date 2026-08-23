from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class MetarReleaseGuard:
    """State around the next routine METAR publication window."""

    is_pending: bool
    expected_at: datetime
    next_expected_at: datetime
    seconds_until_next: float


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def metar_release_guard(
    now: datetime,
    latest_observation: datetime | None,
    schedule_minutes: list[int] | tuple[int, ...],
    *,
    lead_seconds: int = 60,
) -> MetarReleaseGuard:
    """Flag the period after a routine report is due but before it is received.

    ``lead_seconds`` starts the guard shortly before the nominal observation time.
    This prevents a trader from acting on the previous report while the next report
    is already being generated or disseminated.
    """

    minutes = sorted({int(value) for value in schedule_minutes})
    if not minutes or any(value < 0 or value > 59 for value in minutes):
        raise ValueError("schedule_minutes must contain minute values from 0 to 59")

    now_utc = _as_utc(now)
    horizon = now_utc + timedelta(seconds=max(0, lead_seconds))
    candidates: list[datetime] = []
    for hour_offset in range(-2, 3):
        hour = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(
            hours=hour_offset
        )
        candidates.extend(hour.replace(minute=minute) for minute in minutes)

    due = max(value for value in candidates if value <= horizon)
    next_due = min(value for value in candidates if value > horizon)
    latest_utc = _as_utc(latest_observation) if latest_observation is not None else None
    pending = latest_utc is None or latest_utc < due
    return MetarReleaseGuard(
        is_pending=pending,
        expected_at=due,
        next_expected_at=next_due,
        seconds_until_next=(next_due - now_utc).total_seconds(),
    )
