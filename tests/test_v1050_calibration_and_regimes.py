from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd

from weatherman.live_ui import _top_bucket_summary
from weatherman.nowcast import (
    _cap_positive_live_adjustment,
    complete_metar_actuals,
    persistent_hot_regime,
    recent_warm_bias_challenger,
)


def test_complete_stored_metars_backfill_a_missing_prior_daily_actual():
    observations = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "observed_at": datetime(2026, 8, 2, hour, tzinfo=timezone.utc),
                "temp_c": temperature,
            }
            for hour, temperature in [
                (5, 19),
                (7, 22),
                (9, 27),
                (11, 31),
                (13, 35),
                (15, 37),
                (17, 34),
                (19, 29),
            ]
        ]
    )
    result = complete_metar_actuals(
        observations,
        airport_code="LEMD",
        timezone_name="Europe/Madrid",
        target=date(2026, 8, 3),
        as_of=datetime(2026, 8, 3, 11, tzinfo=timezone.utc),
        critical_window_local=["12:30", "18:30"],
    )
    assert result.iloc[0].target_date == date(2026, 8, 2)
    assert result.iloc[0].max_temp_c == 37
    assert result.iloc[0].source == "stored-metar-fallback"


def test_persistent_hot_rejects_stale_or_decisively_cooler_follow_up():
    profile = {
        "persistent_hot": {
            "enabled": True,
            "minimum_latest_actual_c": 37.0,
            "maximum_actual_age_days": 1,
            "maximum_forecast_drop_c": 2.5,
            "minimum_recent_warm_error_c": 0.6,
            "minimum_confirmations": 2,
        }
    }
    scored = pd.DataFrame(
        [{"target_date": date(2026, 8, 1), "model": "AROME-HD", "error": -1.2}]
    )
    stale = persistent_hot_regime(
        pd.DataFrame([{"target_date": date(2026, 8, 1), "max_temp_c": 39.0}]),
        scored,
        target=date(2026, 8, 3),
        forecast_mean=34.0,
        taf_guidance=None,
        profile=profile,
    )
    assert not stale["active"]
    assert stale["latest_actual_c"] is None

    fresh_but_cooler = persistent_hot_regime(
        pd.DataFrame([{"target_date": date(2026, 8, 2), "max_temp_c": 37.0}]),
        scored,
        target=date(2026, 8, 3),
        forecast_mean=33.0,
        taf_guidance=None,
        profile=profile,
    )
    assert not fresh_but_cooler["active"]
    assert fresh_but_cooler["forecast_vs_latest_c"] == -4.0


def test_airport_positive_live_cap_preserves_cooling_evidence():
    guarded, removed = _cap_positive_live_adjustment(
        {
            "temperature_anchor": 1.0,
            "heating_rate": 0.3,
            "clear_sky_override": 0.2,
            "wind": -0.2,
        },
        {"positive_total_cap_c": 0.75},
    )
    assert round(sum(guarded.values()), 2) == 0.75
    assert guarded["wind"] == -0.2
    assert round(removed, 2) == 0.55


def test_munich_recent_warm_bias_requires_weather_confirmation_and_stays_shadow_only():
    rows = []
    for day in range(1, 14):
        error = 0.0 if day <= 10 else -1.5
        for model in ("GFS", "ICON-EU"):
            rows.append(
                {
                    "target_date": date(2026, 7, day),
                    "model": model,
                    "error": error,
                }
            )
    taf = SimpleNamespace(
        cloud_risk="No significant cloud near peak",
        precipitation_risk=False,
        thunderstorm_risk=False,
    )
    profile = {
        "enabled": True,
        "minimum_consecutive_days": 3,
        "minimum_daily_residual_c": 0.25,
        "minimum_residual_c": 0.8,
        "minimum_temp_850_c": 18.0,
        "minimum_radiation_wm2": 650.0,
        "maximum_adjustment_c": 1.5,
    }
    confirmed = recent_warm_bias_challenger(
        pd.DataFrame(rows),
        target=date(2026, 7, 14),
        taf_guidance=taf,
        temp_850_c=21.0,
        radiation_wm2=820.0,
        post_convective_active=False,
        profile=profile,
    )
    assert confirmed["active"]
    assert 1.0 < float(confirmed["adjustment_c"]) < 1.5

    convective = recent_warm_bias_challenger(
        pd.DataFrame(rows),
        target=date(2026, 7, 14),
        taf_guidance=taf,
        temp_850_c=21.0,
        radiation_wm2=820.0,
        post_convective_active=True,
        profile=profile,
    )
    assert not convective["active"]
    assert convective["adjustment_c"] == 0


def test_exact_temperature_and_open_polymarket_bucket_are_labelled_separately():
    probabilities = {36: 0.28, 37: 0.24, 38: 0.11, 39: 0.04, 35: 0.20, 34: 0.13}
    markets = pd.DataFrame(
        [
            {
                "market_id": "36",
                "bucket_label": "36°C",
                "bucket_low_c": 36,
                "bucket_high_c": 36,
                "yes_price": 0.30,
                "best_ask": 0.31,
            },
            {
                "market_id": "37+",
                "bucket_label": "37°C or higher",
                "bucket_low_c": 37,
                "bucket_high_c": None,
                "yes_price": 0.35,
                "best_ask": 0.36,
            },
        ]
    )
    exact, exact_probability, market_bucket, market_probability, market_is_exact = (
        _top_bucket_summary(probabilities, markets)
    )
    assert exact == 36
    assert exact_probability == 0.28
    assert market_bucket == "37°C or higher"
    assert round(market_probability, 2) == 0.39
    assert not market_is_exact
