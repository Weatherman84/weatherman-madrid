from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(
        tzinfo=timezone.utc
    )


def validate_export(
    path: Path,
    *,
    now: datetime | None = None,
    max_age_minutes: float = 10,
) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("classification") != "READ-ONLY DAILY ANALYSIS EXPORT":
        raise ValueError("Unexpected export classification")
    if payload.get("airport") != "LEMD":
        raise ValueError("Export is not scoped to LEMD")
    if payload.get("contains_credentials") is not False:
        raise ValueError("Export credential safety flag is not false")
    if payload.get("writes_production_database") is not False:
        raise ValueError("Export production-write safety flag is not false")

    current = _utc(now or datetime.now(timezone.utc))
    generated_raw = str(payload.get("generated_at") or "")
    if not generated_raw:
        raise ValueError("Export has no generated_at timestamp")
    generated = _utc(datetime.fromisoformat(generated_raw.replace("Z", "+00:00")))
    age_minutes = (current - generated).total_seconds() / 60
    if age_minutes < -2:
        raise ValueError("Export generated_at is implausibly in the future")
    if age_minutes > max(1.0, float(max_age_minutes)):
        raise ValueError(
            f"Export is stale: generated_at age is {age_minutes:.1f} minutes"
        )

    madrid_today = current.astimezone(ZoneInfo("Europe/Madrid")).date().isoformat()
    last_target_date = str((payload.get("window") or {}).get("last_target_date") or "")
    if last_target_date != madrid_today:
        raise ValueError(
            f"Export window ends at {last_target_date or 'missing'}, expected {madrid_today}"
        )
    return payload
