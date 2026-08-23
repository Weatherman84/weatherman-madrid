from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from weatherman.nowcast import (
    _cap_overlapping_positive_sky_contributions,
    build_future_outlook,
    phase_vs_amplitude_regime,
)


def test_amsterdam_sky_overlap_guard_caps_joint_positive_effect():
    contributions = {
        "temperature_anchor": 0.40,
        "dryness": 0.10,
        "cloud": 0.10,
        "radiation": 0.08,
        "late_dry_mixing": 0.30,
        "clear_sky_override": 0.38,
        "wind": 0.0,
    }
    guarded, removed = _cap_overlapping_positive_sky_contributions(
        contributions,
        {"positive_sky_cap_c": 0.35},
    )
    sky_names = (
        "dryness",
        "cloud",
        "radiation",
        "late_dry_mixing",
        "clear_sky_override",
    )
    assert round(sum(guarded[name] for name in sky_names), 2) == 0.35
    assert round(removed, 2) == 0.61
    assert guarded["temperature_anchor"] == 0.40


def test_munich_unconfirmed_phase_changes_spread_not_center_anchor():
    as_of = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    hourly = pd.DataFrame(
        [
            {
                "model": "ICON-EU",
                "run_at": as_of - timedelta(hours=1),
                "valid_at": as_of + timedelta(hours=offset),
                "temp_c": temperature,
            }
            for offset, temperature in [(-2, 23.0), (-1, 25.0), (0, 27.0), (1, 29.0)]
        ]
    )
    observations = pd.DataFrame(
        [
            {
                "observed_at": as_of + timedelta(hours=offset),
                "temp_c": temperature,
            }
            for offset, temperature in [(-2, 25.0), (-1, 27.0), (0, 29.0)]
        ]
    )
    result = phase_vs_amplitude_regime(
        hourly,
        observations,
        timezone_name="Europe/Berlin",
        target=date(2026, 7, 21),
        as_of=as_of,
        hours_to_peak=3.0,
        profile={
            "enabled": True,
            "minimum_reports": 3,
            "minimum_phase_shift_hours": 0.75,
            "minimum_rmse_gain_c": 0.30,
            "maximum_phase_rmse_c": 1.0,
            "center_minimum_reports": 5,
            "center_minimum_rmse_gain_c": 0.55,
            "center_maximum_phase_rmse_c": 0.65,
            "phase_anchor_blend": 0.75,
            "unconfirmed_spread_addition_c": 0.15,
            "unconfirmed_confidence_multiplier": 0.92,
        },
    )
    assert result["active"]
    assert not result["center_active"]
    assert result["anchor_blend"] == 0.0
    assert result["spread_addition_c"] == 0.15
    assert result["confidence_multiplier"] == 0.92


def test_munich_cloud_clearance_outlook_is_shadow_only():
    clearing_at = datetime(2026, 7, 21, 13, tzinfo=timezone.utc)
    outlook = build_future_outlook(
        taf_guidance=SimpleNamespace(
            post_rain_reheating_predicted=False,
            cloud_clearance_reheating_predicted=True,
            precipitation_end_at=None,
            clearing_at=clearing_at,
        ),
        remaining_rise_c=1.2,
        future_radiation_max=650.0,
        expected_peak_at=datetime(2026, 7, 21, 15, tzinfo=timezone.utc),
        hours_to_peak=3.0,
        timezone_name="Europe/Berlin",
        profile={"cloud_clearance_challenger": True},
    )
    assert outlook.reheating_watch
    assert outlook.cloud_clearance_reheating_watch
    assert not outlook.post_rain_reheating_watch
    assert outlook.challenger_name == "Cloud-Clearance Reheating Challenger"
    assert outlook.challenger_factor == "cloud_clearance_reheating"
    assert outlook.challenger_adjustment_c > 0


def test_cloud_clearance_challenger_is_disabled_without_airport_profile():
    outlook = build_future_outlook(
        taf_guidance=SimpleNamespace(
            post_rain_reheating_predicted=False,
            cloud_clearance_reheating_predicted=True,
            precipitation_end_at=None,
            clearing_at=datetime(2026, 7, 21, 13, tzinfo=timezone.utc),
        ),
        remaining_rise_c=1.2,
        future_radiation_max=650.0,
        expected_peak_at=datetime(2026, 7, 21, 15, tzinfo=timezone.utc),
        hours_to_peak=3.0,
        timezone_name="Europe/Amsterdam",
    )
    assert not outlook.reheating_watch
    assert outlook.challenger_name is None
