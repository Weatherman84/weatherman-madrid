from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from weatherman import providers
from weatherman.live_ui import model_maxima_diagnostics
from weatherman.nowcast import fixed_d1_training_sample, temperature_anchor_profile


def test_open_meteo_forecast_attaches_authoritative_model_run_metadata(monkeypatch):
    def fake_get(url, *_args, **_kwargs):
        if str(url).endswith("/static/meta.json"):
            return {
                "last_run_initialisation_time": 1_785_817_200,
                "last_run_availability_time": 1_785_821_400,
            }
        return {
            "daily": {
                "time": ["2026-08-04"],
                "temperature_2m_max": [34.4],
            }
        }

    monkeypatch.setattr(providers, "_get", fake_get)
    rows = providers.open_meteo_forecast(
        {
            "latitude": 40.466,
            "longitude": -3.555,
            "timezone": "Europe/Madrid",
        },
        "meteofrance_arome_france_hd",
        days=1,
    )
    assert rows[0]["max_temp_c"] == 34.4
    assert rows[0]["model_run_at"] == datetime.fromtimestamp(
        1_785_817_200, tz=timezone.utc
    )
    assert rows[0]["available_at"] == datetime.fromtimestamp(
        1_785_821_400, tz=timezone.utc
    )
    assert "authoritative" in rows[0]["provenance_status"]


def test_fixed_d1_training_uses_last_value_available_by_2000_local():
    target = date(2026, 8, 4)
    rows = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": "AROME-HD",
                "run_at": run_at,
                "target_date": target,
                "max_temp_c": value,
                "horizon": "D-1",
            }
            for run_at, value in [
                (datetime(2026, 8, 3, 15, tzinfo=timezone.utc), 34.0),
                (datetime(2026, 8, 3, 17, 30, tzinfo=timezone.utc), 34.4),
                (datetime(2026, 8, 3, 19, tzinfo=timezone.utc), 35.0),
            ]
        ]
    )
    selected = fixed_d1_training_sample(rows, "Europe/Madrid")
    assert len(selected) == 1
    assert selected.iloc[0].max_temp_c == 34.4


def test_temperature_anchor_gain_ramps_toward_peak_without_a_morning_jump():
    residuals = pd.DataFrame({"residual_c": [-1.0, -1.1, -1.2]})
    _, early_gain, streak, _ = temperature_anchor_profile(residuals, None, 6.0)
    _, late_gain, _, _ = temperature_anchor_profile(residuals, None, 1.0)
    assert streak == 3
    assert early_gain == 0.20
    assert late_gain == 0.62


def test_advanced_model_maxima_table_contains_all_three_local_times():
    run_at = datetime(2026, 8, 4, 3, tzinfo=timezone.utc)
    available_at = run_at + timedelta(hours=2, minutes=38)
    fetched_at = available_at + timedelta(minutes=12)
    freshness = pd.DataFrame(
        [
            {
                "model": "meteofrance_arome_france_hd",
                "max_temp_c": 34.4,
                "model_run_at": run_at,
                "available_at": available_at,
                "fetched_at": fetched_at,
                "age_minutes": 8.0,
                "used_in_forecast": True,
                "provenance_status": "Open-Meteo authoritative model metadata",
            }
        ]
    )
    current = freshness.assign(
        corrected_max=34.6,
        model_weight=0.25,
        d1_bias=-0.2,
    )
    table = model_maxima_diagnostics(
        SimpleNamespace(model_freshness=freshness, current=current),
        "Europe/Madrid",
    )
    assert table.loc[0, "Model run · local"] == "04.08.2026 05:00"
    assert table.loc[0, "Provider available · local"] == "04.08.2026 07:38"
    assert table.loc[0, "Fetched · local"] == "04.08.2026 07:50"
    assert table.loc[0, "Raw max °C"] == 34.4
    assert table.loc[0, "Corrected max °C"] == 34.6
