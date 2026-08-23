from types import SimpleNamespace

import pandas as pd

from weatherman.live_display import (
    challenger_rows,
    forecast_chain_rows,
    forecast_driver_rows,
)


def sample_nowcast():
    memory = SimpleNamespace(
        analog_count=5,
        best_similarity=0.78,
        center_adjustment_c=0.30,
        applied_to_champion=False,
        challenger_ready=True,
    )
    return SimpleNamespace(
        current=pd.DataFrame([{"model": "ECMWF"}, {"model": "HARMONIE"}]),
        weighted_raw_mean=27.8,
        weighted_raw_spread=0.8,
        corrected=SimpleNamespace(mean=27.4),
        metar_conditioned_mean=27.7,
        final_forecast_mean=27.8,
        final_forecast_spread=0.9,
        current_observed_temp=25.0,
        heating_rate=0.8,
        radiation_wm2=500.0,
        wind_speed_kph=18.0,
        wind_direction_deg=250.0,
        wind_source="METAR",
        taf_guidance=SimpleNamespace(
            max_temp_c=28.0,
            agreement="Supports model",
            cloud_risk="No significant cloud near peak",
            center_adjustment_c=0.10,
            spread_addition_c=0.10,
        ),
        future_outlook=SimpleNamespace(
            status="POST-RAIN REHEATING WATCH",
            post_rain_reheating_watch=True,
            challenger_adjustment_c=0.25,
        ),
        regime_memory=memory,
        day_status=SimpleNamespace(
            label="Heating window open",
            is_locked=False,
            minimum_bucket=25,
        ),
        live_features={
            "effective_temperature_residual_c": 0.6,
            "observed_cloud_cover_pct": 20.0,
            "observed_dryness_c": 10.0,
        },
        adjustment_contributions={
            "temperature_anchor": 0.30,
            "heating_rate": 0.10,
            "dryness": 0.10,
            "cloud": 0.05,
            "wind": -0.10,
            "total": 0.45,
        },
        challenger_variants={
            "Without Maritime Advection": {
                "forecast_mean_c": 28.2,
                "spread_c": 1.0,
                "probabilities": {28: 0.6, 29: 0.4},
            },
            "Analog Memory Challenger": {
                "forecast_mean_c": 28.1,
                "spread_c": 1.0,
                "probabilities": {28: 0.4, 29: 0.6},
            },
        },
    )


def test_forecast_chain_ends_with_unambiguous_champion_label():
    rows = forecast_chain_rows(sample_nowcast())
    assert [row["Stage"] for row in rows] == [
        "Raw ensemble",
        "Bias-corrected",
        "Live weather-adjusted",
        "Champion",
    ]
    assert rows[-1]["Forecast"] == "27.80 °C"


def test_challengers_distinguish_factor_tests_from_research_alternatives():
    rows = challenger_rows(sample_nowcast())
    by_name = {row["Variant"]: row for row in rows}
    assert by_name["Without Maritime Advection"]["Role"] == "Live-factor test"
    assert by_name["Analog Memory Challenger"]["Role"] == "Research only"
    assert "Shadow alternative +0.30 °C" in by_name["Analog Memory Challenger"][
        "Interpretation"
    ]


def test_forecast_drivers_separate_live_and_shadow_effects():
    rows = forecast_driver_rows(sample_nowcast())
    by_area = {row["Area"]: row for row in rows}
    assert by_area["Future outlook"]["Effect"] == "Champion +0.00 °C · shadow +0.25 °C"
    assert by_area["Historical analog Challenger"]["Effect"] == (
        "Live +0.00 °C · shadow +0.30 °C"
    )
    assert by_area["Day constraints"]["Effect"] == "Buckets below 25 °C removed"
