import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from weatherman.analytics import paired_d1_d0_reliability
from weatherman.nowcast import post_convective_uncertainty, rapid_heat_ramp_regime
from weatherman.regime_memory import _known_regime_states, assess_anchor_transfer
from weatherman.regime_profiles import continuous_regime_profiles


def test_universal_profiles_enable_shared_regimes_but_keep_maritime_applicability():
    inland = continuous_regime_profiles({"timezone": "Europe/Warsaw"})
    coastal = continuous_regime_profiles(
        {
            "timezone": "Europe/Amsterdam",
            "maritime_advection": {
                "enabled": True,
                "maritime_sectors": [[220, 340]],
            },
        }
    )
    assert inland["heat"]["persistent_hot"]["enabled"]
    assert inland["phase"]["enabled"]
    assert inland["post_convective"]["enabled"]
    assert inland["maritime_advection"] is None
    assert coastal["maritime_advection"]["maritime_sectors"] == [[220, 340]]
    assert coastal["heat"]["persistent_hot"]["minimum_latest_anomaly_c"] == 1.5


def test_regime_evidence_grows_before_the_old_binary_threshold():
    target = date(2026, 8, 4)
    actuals = pd.DataFrame(
        [
            {"target_date": target - timedelta(days=2), "max_temp_c": 25.0},
            {"target_date": target - timedelta(days=1), "max_temp_c": 27.0},
        ]
    )
    regime = rapid_heat_ramp_regime(
        actuals,
        target=target,
        forecast_mean=29.25,
        profile={"one_day_threshold_c": 3.0, "gradual_start_fraction": 0.5},
    )
    assert regime["active"]
    assert 0 < float(regime["strength"]) < 1
    assert 0.45 < float(regime["bias_multiplier"]) < 1


def test_single_recent_convective_report_creates_partial_not_full_uncertainty():
    as_of = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
    observations = pd.DataFrame(
        [
            {
                "observed_at": as_of - timedelta(hours=1),
                "raw": "EPWA 041100Z VCTS SCT030CB",
            }
        ]
    )
    result = post_convective_uncertainty(
        observations,
        as_of,
        {
            "enabled": True,
            "window_hours": 48,
            "full_strength_hours": 12,
            "minimum_reports": 2,
            "spread_multiplier": 1.4,
            "confidence_multiplier": 0.88,
        },
    )
    assert result["active"]
    assert float(result["strength"]) == 0.5
    assert float(result["spread_multiplier"]) == 1.2


def test_post_convective_state_without_rapid_heat_ramp_is_independent():
    target = date(2026, 8, 5)
    nowcast = SimpleNamespace(
        live_features={
            "post_convective_uncertainty_active": 1.0,
            "post_convective_reports_48h": 1.0,
        },
        taf_guidance=None,
        wind_direction_deg=270.0,
        wind_speed_kph=16.0,
    )
    states = _known_regime_states(
        nowcast,
        pd.DataFrame(),
        airport_profile={
            "maritime_advection": {
                "enabled": True,
                "maritime_sectors": [[220, 340]],
            }
        },
        timezone_name="Europe/Amsterdam",
        target=target,
        as_of=datetime(2026, 8, 5, 10, tzinfo=timezone.utc),
    )

    names = {state.name for state in states}
    assert "Post-Convective Uncertainty" in names
    assert "Rapid Heat Ramp" not in names


def test_rapid_heat_ramp_state_does_not_depend_on_post_convective_state():
    target = date(2026, 8, 5)
    nowcast = SimpleNamespace(
        live_features={
            "rapid_heat_ramp_active": 1.0,
            "rapid_heat_ramp_forecast_vs_latest_c": 2.5,
            "heating_rate_surprise_cph": 0.5,
        },
        taf_guidance=None,
        wind_direction_deg=120.0,
        wind_speed_kph=10.0,
    )
    states = _known_regime_states(
        nowcast,
        pd.DataFrame(),
        airport_profile={},
        timezone_name="Europe/Amsterdam",
        target=target,
        as_of=datetime(2026, 8, 5, 10, tzinfo=timezone.utc),
    )

    rapid = next(state for state in states if state.name == "Rapid Heat Ramp")
    assert rapid.status == "CONFIRMED"
    assert "METAR heating supports the transition" in rapid.supports


def test_anchor_transfer_learns_airport_peak_bucket_as_shadow_only():
    target = date(2026, 8, 20)
    snapshots = []
    actuals = []
    for offset in range(1, 9):
        historic_day = target - timedelta(days=offset)
        snapshots.append(
            {
                "airport": "LEMD",
                "target_date": historic_day,
                "captured_at": datetime.combine(
                    historic_day, datetime.min.time(), tzinfo=timezone.utc
                )
                + timedelta(hours=9),
                "final_forecast_c": 29.0,
                "temp_anchor_adjustment_c": -0.2,
                "hours_to_peak": 6.0,
                "features_json": json.dumps(
                    {
                        "effective_temperature_residual_c": -1.0,
                        "temperature_anchor_streak": 3,
                    }
                ),
            }
        )
        actuals.append(
            {
                "airport": "LEMD",
                "target_date": historic_day,
                "max_temp_c": 29.2,
                "source": "metar-final",
            }
        )
    nowcast = SimpleNamespace(
        current=pd.DataFrame([{"airport": "LEMD"}]),
        hours_to_peak=6.0,
        live_features={
            "effective_temperature_residual_c": -1.0,
            "temperature_anchor_gain": 0.20,
            "temperature_anchor_streak": 3,
        },
        adjustment_contributions={"temperature_anchor": -0.20, "total": -0.20},
        final_forecast_mean=29.0,
    )
    assessment = assess_anchor_transfer(
        nowcast,
        pd.DataFrame(snapshots),
        pd.DataFrame(actuals),
        pd.DataFrame(),
        pd.DataFrame(),
        airport_profile={"critical_window_local": ["12:30", "18:30"]},
        timezone_name="Europe/Madrid",
        target=target,
        as_of=datetime(2026, 8, 20, 8, tzinfo=timezone.utc),
        config={"anchor_minimum_training_days": 5},
    )
    assert assessment.ready
    assert assessment.history_days == 8
    assert 0 < assessment.learned_gain < assessment.prior_gain
    assert assessment.forecast_delta_c > 0
    assert not assessment.applied_to_champion


def test_paired_reliability_isolates_anchor_from_complete_d0_nowcast():
    zone = ZoneInfo("Europe/Berlin")
    snapshots = []
    actuals = []
    for day_number in (1, 2):
        target = date(2026, 8, day_number)
        d1_local = datetime(target.year, target.month, target.day, 19, 50, tzinfo=zone) - timedelta(
            days=1
        )
        d0_local = datetime(target.year, target.month, target.day, 9, 50, tzinfo=zone)
        snapshots.extend(
            [
                {
                    "airport": "EDDM",
                    "target_date": target,
                    "captured_at": d1_local.astimezone(timezone.utc),
                    "timing": "D-1 or earlier",
                    "final_forecast_c": 30.0,
                    "live_adjustment_c": 0.0,
                    "temp_anchor_adjustment_c": 0.0,
                },
                {
                    "airport": "EDDM",
                    "target_date": target,
                    "captured_at": d0_local.astimezone(timezone.utc),
                    "timing": "D0 morning",
                    "final_forecast_c": 28.8,
                    "live_adjustment_c": -1.2,
                    "temp_anchor_adjustment_c": -1.0,
                },
            ]
        )
        actuals.append(
            {"airport": "EDDM", "target_date": target, "max_temp_c": 30.0}
        )
    result = paired_d1_d0_reliability(
        pd.DataFrame(snapshots),
        pd.DataFrame(actuals),
        {"EDDM": "Europe/Berlin"},
    ).set_index("stage")
    assert result.loc["D-1 @20 Champion", "mae"] == 0
    assert result.loc["D0 @10 before live factors", "mae"] == 0
    assert result.loc["D0 @10 with Anchor only", "mae"] == 1
    assert round(float(result.loc["D0 @10 complete Nowcast", "mae"]), 1) == 1.2
