from datetime import datetime, timezone

import pytest

from weatherman.metar_live import metar_release_guard


def utc(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 21, hour, minute, second, tzinfo=timezone.utc)


def test_guard_is_clear_before_the_next_release_window():
    state = metar_release_guard(utc(13, 58), utc(13, 30), [0, 30])

    assert not state.is_pending
    assert state.next_expected_at == utc(14, 0)


def test_guard_starts_one_minute_before_a_due_report():
    state = metar_release_guard(utc(13, 59, 30), utc(13, 30), [0, 30])

    assert state.is_pending
    assert state.expected_at == utc(14, 0)


def test_guard_clears_as_soon_as_the_due_report_is_received():
    state = metar_release_guard(utc(14, 1), utc(14, 0), [0, 30])

    assert not state.is_pending
    assert state.next_expected_at == utc(14, 30)


def test_guard_supports_airport_specific_minutes():
    waiting = metar_release_guard(utc(14, 26), utc(13, 55), [25, 55])
    received = metar_release_guard(utc(14, 26), utc(14, 25), [25, 55])

    assert waiting.is_pending
    assert waiting.expected_at == utc(14, 25)
    assert not received.is_pending


def test_guard_rejects_invalid_schedules():
    with pytest.raises(ValueError):
        metar_release_guard(utc(14, 0), None, [])
