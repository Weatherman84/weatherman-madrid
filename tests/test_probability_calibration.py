from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from weatherman.probability_calibration import (
    build_probability_calibration,
    calibrate_distribution,
    calibration_checkpoint,
)


def _history(days: int = 70, *, post_peak_days: int = 0):
    start = date(2026, 8, 1)
    snapshots = []
    variants = []
    actuals = []
    for index in range(days):
        target = start + timedelta(days=index)
        captured = datetime.combine(
            target, datetime.min.time(), timezone.utc
        ) + timedelta(hours=14)
        # Deliberately overconfident warm distribution. The observed frequency
        # is 70% for 14 °C and 30% for 15 °C.
        actual = 14.0 if index % 10 < 7 else 15.0
        snapshots.append(
            {
                "airport": "LEMD",
                "target_date": target,
                "captured_at": captured,
                "checkpoint_label": "Late Live @16:00",
                "checkpoint_status": "scheduled-causal",
                "checkpoint_reconstructed": False,
                "hours_to_peak": -1.0 if index >= days - post_peak_days else 1.0,
            }
        )
        variants.append(
            {
                "airport": "LEMD",
                "target_date": target,
                "captured_at": captured,
                "variant": "Champion",
                "probabilities_json": json.dumps({"14": 0.20, "15": 0.80}),
            }
        )
        actuals.append(
            {
                "airport": "LEMD",
                "target_date": target,
                "max_temp_c": actual,
                "source": "stored-metar-station",
            }
        )
    return pd.DataFrame(snapshots), pd.DataFrame(variants), pd.DataFrame(actuals)


def test_calibration_checkpoint_uses_local_fixed_decision_phase() -> None:
    target = date(2026, 11, 15)
    assert calibration_checkpoint(
        target, datetime(2026, 11, 15, 15, 30, tzinfo=timezone.utc)
    ) == "Late Live @16:00"
    assert calibration_checkpoint(
        target, datetime(2026, 11, 15, 11, 30, tzinfo=timezone.utc)
    ) == "First Live @12:00"


def test_ordinal_calibration_moves_mass_toward_observed_frequency() -> None:
    snapshots, variants, actuals = _history()
    report = build_probability_calibration(
        snapshots,
        variants,
        actuals,
        current_probabilities={14: 0.20, 15: 0.80},
        target=date(2026, 11, 15),
        captured_at=datetime(2026, 11, 15, 15, 30, tzinfo=timezone.utc),
        observed_floor=14,
        hours_to_peak=1.0,
    )
    assert report.evidence_days == 70
    assert report.validation_days == 40
    assert report.probabilities[14] > 0.50
    assert report.brier_gain is not None and report.brier_gain > 0
    assert report.available is True


def test_post_peak_estimate_stays_blocked_without_matching_oos_days() -> None:
    snapshots, variants, actuals = _history(post_peak_days=5)
    report = build_probability_calibration(
        snapshots,
        variants,
        actuals,
        current_probabilities={14: 0.20, 15: 0.80},
        target=date(2026, 11, 15),
        captured_at=datetime(2026, 11, 15, 15, 30, tzinfo=timezone.utc),
        observed_floor=14,
        hours_to_peak=-1.0,
    )
    assert report.probabilities[14] > 0.50
    assert report.available is False
    assert any("matched post-peak days 5/30" in item for item in report.blockers)


def test_physical_observed_floor_cannot_regain_lower_buckets() -> None:
    snapshots, variants, actuals = _history()
    training = []
    # Reuse the public report path to make sure the floor is also respected in
    # direct calibration when lower buckets exist in the source distribution.
    from weatherman.probability_calibration import calibration_records

    training = calibration_records(
        snapshots,
        variants,
        actuals,
        checkpoint="Late Live @16:00",
        before_date=date(2026, 11, 15),
    )
    calibrated = calibrate_distribution(
        {13: 0.10, 14: 0.20, 15: 0.70}, training, observed_floor=14
    )
    assert 13 not in calibrated
    assert abs(sum(calibrated.values()) - 1.0) < 1e-12
