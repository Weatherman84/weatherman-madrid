from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.validate_madrid_daily_analysis_export import validate_export


def payload(generated_at: datetime) -> dict:
    return {
        "classification": "READ-ONLY DAILY ANALYSIS EXPORT",
        "airport": "LEMD",
        "contains_credentials": False,
        "writes_production_database": False,
        "generated_at": generated_at.isoformat(),
        "window": {"last_target_date": "2026-08-28"},
    }


def test_current_export_passes_fail_closed_validation(tmp_path) -> None:
    now = datetime(2026, 8, 28, 19, 20, tzinfo=timezone.utc)
    path = tmp_path / "daily-analysis-latest.json"
    path.write_text(json.dumps(payload(now - timedelta(minutes=2))), encoding="utf-8")

    validated = validate_export(path, now=now, max_age_minutes=10)

    assert validated["airport"] == "LEMD"


def test_stale_export_fails_instead_of_green_noop(tmp_path) -> None:
    now = datetime(2026, 8, 28, 19, 20, tzinfo=timezone.utc)
    path = tmp_path / "daily-analysis-latest.json"
    path.write_text(json.dumps(payload(now - timedelta(hours=5))), encoding="utf-8")

    with pytest.raises(ValueError, match="stale"):
        validate_export(path, now=now, max_age_minutes=10)
