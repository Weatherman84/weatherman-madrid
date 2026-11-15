from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from weatherman.probability_safety import assess_trade_probability


def _nowcast(*, peak_hour: int, locked: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        probabilities={14: 0.20, 15: 0.50, 16: 0.30},
        observed_max=14.0,
        expected_peak_at=datetime(2026, 11, 15, peak_hour, tzinfo=timezone.utc),
        day_status=SimpleNamespace(is_locked=locked),
        challenger_variants={
            "Post-Peak Upper-Tail Challenger": {
                "probabilities": {14: 0.65, 15: 0.25, 16: 0.10}
            }
        },
    )


def test_uncalibrated_pre_peak_mass_is_not_trade_probability() -> None:
    assessment = assess_trade_probability(
        _nowcast(peak_hour=17),
        datetime(2026, 11, 15, 15, tzinfo=timezone.utc),
        calibration_gate_passed=False,
    )
    assert assessment.available is False
    assert assessment.status == "UNCALIBRATED"
    assert assessment.post_peak is False


def test_post_peak_guard_compares_champion_and_shadow_without_promotion() -> None:
    assessment = assess_trade_probability(
        _nowcast(peak_hour=15),
        datetime(2026, 11, 15, 15, 30, tzinfo=timezone.utc),
        calibration_gate_passed=True,
    )
    assert assessment.available is False
    assert assessment.status == "POST_PEAK_UNVALIDATED"
    assert assessment.minutes_since_model_peak == 30
    assert assessment.champion_upper_tail_probability == 0.8
    assert assessment.shadow_upper_tail_probability == 0.35
    assert "5/30" in assessment.explanation


def test_pre_peak_probability_requires_explicit_calibration_gate() -> None:
    assessment = assess_trade_probability(
        _nowcast(peak_hour=17),
        datetime(2026, 11, 15, 15, tzinfo=timezone.utc),
        calibration_gate_passed=True,
    )
    assert assessment.available is True
    assert assessment.status == "AVAILABLE"
