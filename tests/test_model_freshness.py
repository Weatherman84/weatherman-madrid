from datetime import datetime, timedelta, timezone

from weatherman.model_freshness import assess_model_freshness


def assess(model: str, age_minutes: int):
    now = datetime(2026, 8, 29, 10, tzinfo=timezone.utc)
    return assess_model_freshness(
        model,
        as_of=now,
        available_at=now - timedelta(minutes=age_minutes),
    )


def test_six_hour_model_remains_current_beyond_90_minutes() -> None:
    result = assess("gfs_global", 210)

    assert result is not None
    assert result.status == "current_latest_run"
    assert result.usable
    assert result.update_interval_minutes == 360


def test_three_hour_model_uses_normal_publication_tolerance() -> None:
    result = assess("meteofrance_arome_france_hd", 260)

    assert result is not None
    assert result.status == "awaiting_next_run"
    assert result.usable
    assert result.next_expected_at == datetime(
        2026, 8, 29, 8, 40, tzinfo=timezone.utc
    )


def test_model_is_excluded_when_expected_successor_is_missing() -> None:
    result = assess("icon_eu", 280)

    assert result is not None
    assert result.status == "missing_expected_run"
    assert not result.usable
    assert result.expected_updates_missed == 1


def test_multiple_missed_cycles_are_hard_stale() -> None:
    result = assess("ecmwf_ifs025", 900)

    assert result is not None
    assert result.status == "hard_stale"
    assert not result.usable
    assert result.expected_updates_missed >= 2
