import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from weatherman.nowcast import build_live_nowcast, rapid_heat_ramp_regime


def test_stale_meteoblue_is_omitted_when_current_models_are_available():
    as_of = datetime(2026, 7, 30, 8, tzinfo=timezone.utc)
    target = as_of.date()
    forecasts = pd.DataFrame(
        [
            {
                "airport": "EHAM",
                "model": model,
                "run_at": as_of - age,
                "fetched_at": as_of - age,
                "target_date": target,
                "max_temp_c": maximum,
                "source": source,
                "horizon": "D0-morning",
            }
            for model, maximum, source, age in [
                ("ecmwf", 28.0, "open-meteo", timedelta(minutes=20)),
                ("icon_eu", 29.0, "open-meteo", timedelta(minutes=20)),
                ("harmonie", 30.0, "open-meteo", timedelta(minutes=20)),
                ("meteoblue", 23.0, "meteoblue", timedelta(hours=9)),
            ]
        ]
    )
    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=pd.DataFrame(),
        hourly=pd.DataFrame(),
        markets=pd.DataFrame(),
        timezone_name="Europe/Amsterdam",
        target=target,
        as_of=as_of,
    )
    assert result is not None
    assert not result.forecast_data_stale
    assert result.fresh_model_count == 3
    assert result.stale_models == ("meteoblue",)
    assert set(result.current.model) == {"ecmwf", "icon_eu", "harmonie"}
    assert result.raw_model_mean == 29.0
    meteoblue = result.model_freshness[result.model_freshness.model == "meteoblue"].iloc[0]
    assert not bool(meteoblue.used_in_forecast)


def test_models_missing_multiple_expected_runs_cannot_create_a_live_champion():
    as_of = datetime(2026, 7, 30, 8, tzinfo=timezone.utc)
    target = as_of.date()
    forecasts = pd.DataFrame(
        [
            {
                "airport": "EHAM",
                "model": model,
                "run_at": as_of - timedelta(hours=15),
                "fetched_at": as_of - timedelta(hours=15),
                "target_date": target,
                "max_temp_c": maximum,
                "source": "open-meteo",
                "horizon": "D0-morning",
            }
            for model, maximum in [("ecmwf", 28.0), ("icon_eu", 29.0)]
        ]
    )
    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=pd.DataFrame(),
        hourly=pd.DataFrame(),
        markets=pd.DataFrame(),
        timezone_name="Europe/Amsterdam",
        target=target,
        as_of=as_of,
    )
    assert result is None


def test_latest_causal_gfs_and_arome_runs_survive_the_old_90_minute_limit():
    as_of = datetime(2026, 8, 29, 10, tzinfo=timezone.utc)
    target = as_of.date()
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": "gfs_global",
                "run_at": as_of - timedelta(minutes=210),
                "available_at": as_of - timedelta(minutes=210),
                "fetched_at": as_of - timedelta(minutes=20),
                "target_date": target,
                "max_temp_c": 33.0,
                "source": "open-meteo",
                "horizon": "Live",
            },
            {
                "airport": "LEMD",
                "model": "meteofrance_arome_france",
                "run_at": as_of - timedelta(minutes=260),
                "available_at": as_of - timedelta(minutes=260),
                "fetched_at": as_of - timedelta(minutes=20),
                "target_date": target,
                "max_temp_c": 34.0,
                "source": "open-meteo",
                "horizon": "Live",
            },
        ]
    )

    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=pd.DataFrame(),
        hourly=pd.DataFrame(),
        markets=pd.DataFrame(),
        timezone_name="Europe/Madrid",
        target=target,
        as_of=as_of,
    )

    assert result is not None
    assert set(result.current.model) == {
        "gfs_global",
        "meteofrance_arome_france",
    }
    states = result.model_freshness.set_index("model").freshness_state.to_dict()
    assert states["gfs_global"] == "current_latest_run"
    assert states["meteofrance_arome_france"] == "awaiting_next_run"


def test_one_fresh_model_is_diagnostic_only_and_stale_model_is_never_used():
    as_of = datetime(2026, 7, 30, 8, tzinfo=timezone.utc)
    target = as_of.date()
    forecasts = pd.DataFrame(
        [
            {
                "airport": "EHAM",
                "model": "ecmwf",
                "run_at": as_of - timedelta(minutes=20),
                "fetched_at": as_of - timedelta(minutes=20),
                "target_date": target,
                "max_temp_c": 28.0,
                "source": "open-meteo",
                "horizon": "D0-morning",
            },
            {
                "airport": "EHAM",
                "model": "meteoblue",
                "run_at": as_of - timedelta(hours=40),
                "fetched_at": as_of - timedelta(hours=40),
                "target_date": target,
                "max_temp_c": 35.0,
                "source": "meteoblue",
                "horizon": "D0-morning",
            },
        ]
    )
    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=pd.DataFrame(),
        hourly=pd.DataFrame(),
        markets=pd.DataFrame(),
        timezone_name="Europe/Amsterdam",
        target=target,
        as_of=as_of,
    )
    assert result is not None
    assert result.forecast_data_stale
    assert result.fresh_model_count == 1
    assert result.raw_model_mean == 28.0
    assert set(result.current.model) == {"ecmwf"}
    stale = result.model_freshness[result.model_freshness.model == "meteoblue"].iloc[0]
    assert not bool(stale.used_in_forecast)


def test_shared_nowcast_locks_completed_evening_peak():
    as_of = datetime(2026, 7, 20, 21, tzinfo=ZoneInfo("Europe/Madrid"))
    as_of_utc = as_of.astimezone(timezone.utc)
    target = as_of.date()
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": model,
                "run_at": as_of_utc - timedelta(minutes=20),
                "target_date": target,
                "max_temp_c": maximum,
                "source": "open-meteo",
                "horizon": "Live",
            }
            for model, maximum in [("ECMWF", 36.0), ("GFS", 37.0)]
        ]
    )
    observations = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "observed_at": as_of_utc - timedelta(hours=2),
                "temp_c": 35.0,
                "dewpoint_c": 15.0,
            },
            {
                "airport": "LEMD",
                "observed_at": as_of_utc - timedelta(minutes=10),
                "temp_c": 32.0,
                "dewpoint_c": 15.0,
            },
        ]
    )
    hourly = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": model,
                "run_at": as_of_utc - timedelta(minutes=20),
                "valid_at": as_of_utc,
                "temp_c": 32.0,
                "cloud_cover": 0.0,
                "temp_850hpa_c": 18.0,
                "radiation_wm2": 0.0,
            }
            for model in ["ECMWF", "GFS"]
        ]
    )
    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=observations,
        hourly=hourly,
        markets=pd.DataFrame(),
        timezone_name="Europe/Madrid",
        target=target,
        as_of=as_of,
    )
    assert result is not None
    assert result.day_status.label == "Peak locked"
    assert result.probabilities == {35: 1.0}
    assert result.remaining_rise_c == 0


def test_taf_conflict_broadens_and_cautiously_lowers_live_distribution():
    as_of = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    target = as_of.date()
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": model,
                "run_at": as_of - timedelta(minutes=20),
                "target_date": target,
                "max_temp_c": maximum,
                "source": "open-meteo",
                "horizon": "Live",
            }
            for model, maximum in [("ECMWF", 39.0), ("GFS", 39.2)]
        ]
    )
    hourly = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": model,
                "run_at": as_of - timedelta(minutes=20),
                "valid_at": as_of,
                "temp_c": 34.0,
                "cloud_cover": 10.0,
                "temp_850hpa_c": 20.0,
                "radiation_wm2": 700.0,
                "wind_kph": 10.0,
                "wind_direction": 180.0,
            }
            for model in ["ECMWF", "GFS"]
        ]
    )
    common = {
        "forecasts": forecasts,
        "actuals": pd.DataFrame(),
        "observations": pd.DataFrame(),
        "hourly": hourly,
        "markets": pd.DataFrame(),
        "timezone_name": "Europe/Madrid",
        "target": target,
        "as_of": as_of,
    }
    without_taf = build_live_nowcast(**common)
    tafs = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "issue_time": as_of - timedelta(hours=1),
                "collected_at": as_of - timedelta(minutes=55),
                "valid_from": datetime(2026, 7, 21, 0, tzinfo=timezone.utc),
                "valid_to": datetime(2026, 7, 22, 6, tzinfo=timezone.utc),
                "raw_taf": "TAF LEMD TX36/2116Z TEMPO TSRA BKN030CB",
                "is_amended": False,
                "is_corrected": False,
                "max_temp_c": 36.0,
                "max_temp_at": datetime(2026, 7, 21, 16, tzinfo=timezone.utc),
                "periods_json": json.dumps(
                    [
                        {
                            "time_from": "2026-07-21T10:00:00+00:00",
                            "time_to": "2026-07-21T18:00:00+00:00",
                            "change": "TEMPO",
                            "weather": "TSRA",
                            "clouds": [{"cover": "BKN", "base": 3000, "type": "CB"}],
                        }
                    ]
                ),
            }
        ]
    )
    with_taf = build_live_nowcast(**common, tafs=tafs)
    assert without_taf is not None and with_taf is not None
    assert with_taf.corrected.mean == without_taf.corrected.mean
    assert with_taf.taf_guidance is not None
    assert with_taf.taf_guidance.agreement == "Contradicts model"
    mean_without = sum(k * v for k, v in without_taf.probabilities.items())
    mean_with = sum(k * v for k, v in with_taf.probabilities.items())
    assert mean_with < mean_without
    assert with_taf.metar_conditioned_mean == without_taf.metar_conditioned_mean
    assert abs(with_taf.taf_adjustment_c) <= 0.25
    assert with_taf.forecast_confidence < without_taf.forecast_confidence


def test_post_rain_reheating_is_forward_looking_and_shadow_only():
    as_of = datetime(2026, 7, 21, 10, tzinfo=timezone.utc)
    target = as_of.date()
    forecasts = pd.DataFrame(
        [
            {
                "airport": "EHAM",
                "model": model,
                "run_at": as_of - timedelta(minutes=20),
                "target_date": target,
                "max_temp_c": maximum,
                "source": "open-meteo",
                "horizon": "Live",
            }
            for model, maximum in [("ECMWF", 27.0), ("HARMONIE", 27.4)]
        ]
    )
    hourly = pd.DataFrame(
        [
            {
                "airport": "EHAM",
                "model": model,
                "run_at": as_of - timedelta(minutes=20),
                "valid_at": valid_at,
                "temp_c": temp_c,
                "dewpoint_c": 14.0,
                "cloud_cover": cloud,
                "temp_850hpa_c": 13.0,
                "radiation_wm2": radiation,
                "wind_kph": 12.0,
                "wind_direction": 240.0,
            }
            for model in ["ECMWF", "HARMONIE"]
            for valid_at, temp_c, cloud, radiation in [
                (as_of, 23.0, 90.0, 120.0),
                (as_of + timedelta(hours=2), 24.0, 45.0, 420.0),
                (as_of + timedelta(hours=4), 27.0, 20.0, 650.0),
            ]
        ]
    )
    tafs = pd.DataFrame(
        [
            {
                "airport": "EHAM",
                "issue_time": as_of - timedelta(hours=1),
                "collected_at": as_of - timedelta(minutes=55),
                "valid_from": datetime(2026, 7, 21, 0, tzinfo=timezone.utc),
                "valid_to": datetime(2026, 7, 22, 6, tzinfo=timezone.utc),
                "raw_taf": "TAF EHAM TEMPO SHRA BKN025 BECMG SCT040 TX27/2114Z",
                "is_amended": False,
                "is_corrected": False,
                "max_temp_c": 27.0,
                "max_temp_at": datetime(2026, 7, 21, 14, tzinfo=timezone.utc),
                "periods_json": json.dumps(
                    [
                        {
                            "time_from": "2026-07-21T09:00:00+00:00",
                            "time_to": "2026-07-21T12:00:00+00:00",
                            "change": "TEMPO",
                            "weather": "SHRA",
                            "clouds": [{"cover": "BKN", "base": 2500}],
                        },
                        {
                            "time_from": "2026-07-21T12:00:00+00:00",
                            "time_to": "2026-07-21T18:00:00+00:00",
                            "change": "BECMG",
                            "weather": None,
                            "clouds": [{"cover": "SCT", "base": 4000}],
                        },
                    ]
                ),
            }
        ]
    )
    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=pd.DataFrame(),
        hourly=hourly,
        markets=pd.DataFrame(),
        tafs=tafs,
        timezone_name="Europe/Amsterdam",
        target=target,
        as_of=as_of,
    )
    assert result is not None
    assert result.future_outlook.post_rain_reheating_watch
    assert result.live_features["post_rain_reheating_watch"] == 1
    assert "post_rain_reheating" not in result.adjustment_contributions
    alternative = result.challenger_variants["Post-Rain Reheating Challenger"]
    assert alternative["forecast_mean_c"] > result.final_forecast_mean
    assert result.future_outlook.challenger_adjustment_c <= 0.35


def test_evening_model_path_is_anchored_to_metar_and_observed_maximum():
    as_of = datetime(2026, 7, 21, 21, tzinfo=ZoneInfo("Europe/Madrid"))
    as_of_utc = as_of.astimezone(timezone.utc)
    target = as_of.date()
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": "ECMWF",
                "run_at": as_of_utc - timedelta(minutes=20),
                "target_date": target,
                "max_temp_c": 38.0,
                "source": "open-meteo",
                "horizon": "Live",
            }
        ]
    )
    observations = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "observed_at": as_of_utc - timedelta(hours=2),
                "temp_c": 37.0,
                "dewpoint_c": 8.0,
            },
            {
                "airport": "LEMD",
                "observed_at": as_of_utc - timedelta(minutes=5),
                "temp_c": 35.0,
                "dewpoint_c": 8.0,
            },
        ]
    )
    hourly = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": "ECMWF",
                "run_at": as_of_utc - timedelta(minutes=20),
                "valid_at": valid_at,
                "temp_c": temp_c,
                "cloud_cover": 0.0,
                "temp_850hpa_c": 20.0,
                "radiation_wm2": 0.0,
            }
            for valid_at, temp_c in [
                (as_of_utc, 33.0),
                (as_of_utc + timedelta(hours=1), 34.0),
            ]
        ]
    )
    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=observations,
        hourly=hourly,
        markets=pd.DataFrame(),
        timezone_name="Europe/Madrid",
        target=target,
        as_of=as_of,
    )
    assert result is not None
    assert result.remaining_rise_c == 0
    assert result.day_status.label == "Peak locked"
    assert result.probabilities == {37: 1.0}


def test_live_conditioning_records_all_observed_weather_contributions():
    as_of = datetime(2026, 7, 22, 14, tzinfo=ZoneInfo("Europe/Madrid"))
    now_utc = as_of.astimezone(timezone.utc)
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": model,
                "run_at": now_utc - timedelta(minutes=20),
                "target_date": as_of.date(),
                "max_temp_c": maximum,
                "source": "open-meteo",
                "horizon": "Live",
            }
            for model, maximum in [("ECMWF", 36.0), ("GFS", 37.0)]
        ]
    )
    observations = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "observed_at": now_utc - timedelta(hours=1),
                "temp_c": 31.0,
                "dewpoint_c": 7.0,
                "cloud_cover": 20.0,
                "wind_kph": 15.0,
                "wind_direction": 180.0,
            },
            {
                "airport": "LEMD",
                "observed_at": now_utc,
                "temp_c": 33.0,
                "dewpoint_c": 5.0,
                "cloud_cover": 0.0,
                "wind_kph": 18.0,
                "wind_direction": 180.0,
            },
        ]
    )
    hourly = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": model,
                "run_at": now_utc - timedelta(minutes=20),
                "valid_at": valid_at,
                "temp_c": temp,
                "dewpoint_c": 15.0,
                "cloud_cover": 70.0,
                "temp_850hpa_c": 18.0,
                "radiation_wm2": 600.0,
                "wind_kph": 12.0,
                "wind_direction": 180.0,
            }
            for model in ["ECMWF", "GFS"]
            for valid_at, temp in [
                (now_utc - timedelta(hours=1), 31.0),
                (now_utc, 32.0),
                (now_utc + timedelta(hours=1), 34.0),
            ]
        ]
    )
    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=observations,
        hourly=hourly,
        markets=pd.DataFrame(),
        timezone_name="Europe/Madrid",
        target=as_of.date(),
        as_of=as_of,
        wind_profile={"warm_sectors": [[120, 230]], "cool_sectors": [[280, 60]]},
    )
    assert result is not None
    assert result.adjustment_contributions["temperature_anchor"] > 0
    assert result.adjustment_contributions["dryness"] > 0
    assert result.adjustment_contributions["cloud"] > 0
    assert result.adjustment_contributions["heating_rate"] > 0
    assert result.adjustment_contributions["radiation"] > 0
    assert result.adjustment_contributions["wind"] > 0
    assert result.adjustment_contributions["clear_sky_override"] > 0
    assert result.adjustment_contributions["total"] > 0
    assert set(result.stage_probabilities) == {
        "Raw model mean",
        "Weighted raw ensemble",
        "Bias corrected · equal weight",
        "Bias corrected · performance weighted",
        "METAR conditioned",
        "Final incl. TAF",
    }


def test_v1060_persistent_morning_metars_update_tmax_gradually():
    as_of = datetime(2026, 7, 26, 10, 30, tzinfo=ZoneInfo("Europe/Istanbul"))
    now_utc = as_of.astimezone(timezone.utc)
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "model": model,
                "run_at": now_utc - timedelta(minutes=20),
                "target_date": as_of.date(),
                "max_temp_c": maximum,
                "source": "open-meteo",
                "horizon": "Live",
            }
            for model, maximum in [("ECMWF", 23.0), ("GFS", 23.4), ("UKMO", 20.9)]
        ]
    )
    valid_points = [
        (now_utc - timedelta(hours=2), 17.0),
        (now_utc - timedelta(hours=1), 18.0),
        (now_utc, 19.0),
        (now_utc + timedelta(hours=4), 23.0),
    ]
    hourly = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "model": model,
                "run_at": now_utc - timedelta(minutes=20),
                "valid_at": valid_at,
                "temp_c": temp,
                "dewpoint_c": 10.0,
                "cloud_cover": 25.0,
                "wind_kph": 10.0,
                "wind_direction": 320.0,
                "radiation_wm2": 600.0,
                "temp_850hpa_c": 12.0,
            }
            for model in ["ECMWF", "GFS", "UKMO"]
            for valid_at, temp in valid_points
        ]
    )
    observations = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "observed_at": valid_at,
                "temp_c": expected + 0.8,
                "dewpoint_c": dewpoint,
                "cloud_cover": 10.0,
                "wind_kph": 8.0,
                "wind_direction": 320.0,
                "raw": "LTAC CAVOK",
            }
            for (valid_at, expected), dewpoint in zip(valid_points[:3], [10.0, 8.0, 6.0])
        ]
    )
    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=observations,
        hourly=hourly,
        markets=pd.DataFrame(),
        timezone_name="Europe/Istanbul",
        target=as_of.date(),
        as_of=as_of,
        wind_profile={"warm_sectors": [[140, 260]], "cool_sectors": [[300, 60]]},
    )
    assert result is not None
    assert result.live_features["temperature_anchor_streak"] == 3
    assert 0.20 <= result.adjustment_contributions["temperature_anchor"] <= 0.40
    assert result.current.loc[result.current.model == "UKMO", "outlier_multiplier"].iloc[0] < 1
    assert result.adjustment_contributions["total"] > 0


def test_v10_detects_failed_convection_after_clear_recent_metars():
    as_of = datetime(2026, 7, 26, 13, 30, tzinfo=ZoneInfo("Europe/Istanbul"))
    now_utc = as_of.astimezone(timezone.utc)
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "model": model,
                "run_at": now_utc - timedelta(minutes=20),
                "target_date": as_of.date(),
                "max_temp_c": 23.0,
                "source": "open-meteo",
                "horizon": "Live",
            }
            for model in ["ECMWF", "GFS"]
        ]
    )
    hourly = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "model": model,
                "run_at": now_utc - timedelta(minutes=20),
                "valid_at": valid_at,
                "temp_c": temp,
                "dewpoint_c": 8.0,
                "cloud_cover": 65.0,
                "wind_kph": 10.0,
                "wind_direction": 320.0,
                "radiation_wm2": 600.0,
                "temp_850hpa_c": 12.0,
            }
            for model in ["ECMWF", "GFS"]
            for valid_at, temp in [
                (now_utc - timedelta(hours=1), 20.0),
                (now_utc, 21.0),
                (now_utc + timedelta(hours=2), 23.0),
            ]
        ]
    )
    observations = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "observed_at": now_utc - timedelta(hours=1),
                "temp_c": 20.5,
                "dewpoint_c": 8.0,
                "cloud_cover": 20.0,
                "raw": "LTAC 9999 SCT040",
            },
            {
                "airport": "LTAC",
                "observed_at": now_utc,
                "temp_c": 21.5,
                "dewpoint_c": 7.0,
                "cloud_cover": 20.0,
                "raw": "LTAC 9999 SCT040 SCT100",
            },
        ]
    )
    tafs = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "issue_time": now_utc - timedelta(hours=2),
                "collected_at": now_utc - timedelta(hours=2),
                "valid_from": datetime(2026, 7, 25, 21, tzinfo=timezone.utc),
                "valid_to": datetime(2026, 7, 27, 3, tzinfo=timezone.utc),
                "raw_taf": "TAF LTAC TX23/2612Z TEMPO TSRA BKN030CB",
                "is_amended": False,
                "is_corrected": False,
                "max_temp_c": 23.0,
                "max_temp_at": datetime(2026, 7, 26, 12, tzinfo=timezone.utc),
                "periods_json": json.dumps(
                    [
                        {
                            "time_from": "2026-07-26T09:00:00+00:00",
                            "time_to": "2026-07-26T14:00:00+00:00",
                            "change": "TEMPO",
                            "weather": "TSRA",
                            "clouds": [{"cover": "BKN", "base": 3000, "type": "CB"}],
                        }
                    ]
                ),
            }
        ]
    )
    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=observations,
        hourly=hourly,
        markets=pd.DataFrame(),
        tafs=tafs,
        timezone_name="Europe/Istanbul",
        target=as_of.date(),
        as_of=as_of,
    )
    assert result is not None
    assert result.adjustment_contributions["failed_convection"] == 0.35
    assert result.live_features["failed_convection_active"] == 1
    assert any("not materialised" in signal for signal in result.heat.signals)


def test_post_convective_regime_broadens_but_does_not_shift_forecast_centre():
    as_of = datetime(2026, 7, 27, 13, tzinfo=ZoneInfo("Europe/Istanbul"))
    now_utc = as_of.astimezone(timezone.utc)
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "model": model,
                "run_at": now_utc - timedelta(minutes=20),
                "target_date": as_of.date(),
                "max_temp_c": maximum,
                "source": "open-meteo",
                "horizon": "Live",
            }
            for model, maximum in [("ECMWF", 26.0), ("GFS", 26.2)]
        ]
    )
    hourly = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "model": model,
                "run_at": now_utc - timedelta(minutes=20),
                "valid_at": valid_at,
                "temp_c": temp,
                "dewpoint_c": 8.0,
                "cloud_cover": 10.0,
                "wind_kph": 10.0,
                "wind_direction": 250.0,
                "radiation_wm2": 700.0,
                "temp_850hpa_c": 14.0,
            }
            for model in ["ECMWF", "GFS"]
            for valid_at, temp in [
                (now_utc, 24.0),
                (now_utc + timedelta(hours=3), 26.0),
            ]
        ]
    )
    observations = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "observed_at": now_utc - timedelta(hours=45),
                "temp_c": 20.0,
                "dewpoint_c": 12.0,
                "raw": "LTAC TSRA BKN030CB",
            },
            {
                "airport": "LTAC",
                "observed_at": now_utc - timedelta(hours=44, minutes=30),
                "temp_c": 19.0,
                "dewpoint_c": 13.0,
                "raw": "LTAC VCTS SCT030CB",
            },
        ]
    )
    common = {
        "forecasts": forecasts,
        "actuals": pd.DataFrame(),
        "observations": observations,
        "hourly": hourly,
        "markets": pd.DataFrame(),
        "timezone_name": "Europe/Istanbul",
        "target": as_of.date(),
        "as_of": as_of,
        "critical_window_local": ["11:30", "18:30"],
    }
    baseline = build_live_nowcast(**common)
    broadened = build_live_nowcast(
        **common,
        post_convective_profile={
            "enabled": True,
            "window_hours": 48,
            "minimum_reports": 2,
            "spread_multiplier": 1.5,
            "confidence_multiplier": 0.85,
        },
    )

    assert baseline is not None
    assert broadened is not None
    assert broadened.live_features["post_convective_uncertainty_active"] == 1
    assert broadened.live_features["post_convective_reports_48h"] == 2
    assert broadened.final_forecast_spread > baseline.final_forecast_spread
    assert abs(broadened.final_forecast_mean - baseline.final_forecast_mean) < 0.05
    assert broadened.forecast_confidence < baseline.forecast_confidence


def test_late_dry_mixing_flags_early_model_ceiling_and_adds_warm_tail_signal():
    as_of = datetime(2026, 7, 27, 15, 30, tzinfo=ZoneInfo("Europe/Istanbul"))
    now_utc = as_of.astimezone(timezone.utc)
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "model": model,
                "run_at": now_utc - timedelta(minutes=20),
                "target_date": as_of.date(),
                "max_temp_c": maximum,
                "source": "open-meteo",
                "horizon": "Live",
            }
            for model, maximum in [("ECMWF", 26.0), ("GFS", 26.2)]
        ]
    )
    hourly = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "model": model,
                "run_at": now_utc - timedelta(minutes=20),
                "valid_at": valid_at,
                "temp_c": temp,
                "dewpoint_c": 8.0,
                "cloud_cover": 5.0,
                "wind_kph": 10.0,
                "wind_direction": 320.0,
                "radiation_wm2": 650.0,
                "temp_850hpa_c": 14.0,
            }
            for model in ["ECMWF", "GFS"]
            for valid_at, temp in [
                (now_utc - timedelta(hours=1), 25.0),
                (now_utc, 25.5),
                (now_utc + timedelta(hours=1), 26.0),
            ]
        ]
    )
    observations = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "observed_at": now_utc - timedelta(hours=1),
                "temp_c": 25.5,
                "dewpoint_c": 8.0,
                "cloud_cover": 0.0,
                "wind_kph": 10.0,
                "wind_direction": 320.0,
                "raw": "LTAC CAVOK",
            },
            {
                "airport": "LTAC",
                "observed_at": now_utc,
                "temp_c": 26.0,
                "dewpoint_c": 6.0,
                "cloud_cover": 0.0,
                "wind_kph": 9.0,
                "wind_direction": 320.0,
                "raw": "LTAC CAVOK",
            },
        ]
    )
    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=observations,
        hourly=hourly,
        markets=pd.DataFrame(),
        timezone_name="Europe/Istanbul",
        target=as_of.date(),
        as_of=as_of,
        critical_window_local=["11:30", "18:30"],
    )

    assert result is not None
    assert result.live_features["model_ceiling_reached_early"] == 1
    assert result.live_features["late_dry_mixing_active"] == 1
    assert result.adjustment_contributions["late_dry_mixing"] == 0.30
    assert result.adjustment_contributions["wind"] == 0
    assert any("Late dry mixing" in signal for signal in result.heat.signals)


def test_rapid_heat_ramp_uses_yesterday_without_adding_a_fixed_degree():
    actuals = pd.DataFrame(
        [
            {"target_date": datetime(2026, 7, 27).date(), "max_temp_c": 25.0},
            {"target_date": datetime(2026, 7, 28).date(), "max_temp_c": 29.0},
        ]
    )
    regime = rapid_heat_ramp_regime(
        actuals,
        target=datetime(2026, 7, 29).date(),
        forecast_mean=32.0,
        profile={
            "positive_bias_multiplier": 0.4,
            "spread_multiplier": 1.3,
        },
    )
    assert regime["active"]
    assert regime["forecast_vs_latest_c"] == 3
    assert regime["latest_actual_change_c"] == 4
    assert regime["bias_multiplier"] == 0.4
    assert regime["spread_multiplier"] == 1.3


def test_rapid_heat_ramp_protects_a_coherent_warm_regional_cluster():
    as_of = datetime(2026, 7, 29, 9, tzinfo=ZoneInfo("Europe/Berlin"))
    now_utc = as_of.astimezone(timezone.utc)
    target = as_of.date()
    forecasts = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "model": model,
                "run_at": now_utc - timedelta(minutes=20),
                "target_date": target,
                "max_temp_c": maximum,
                "source": "open-meteo",
                "horizon": "Live",
            }
            for model, maximum in [
                ("ecmwf_ifs025", 30.0),
                ("gfs_global", 30.5),
                ("icon_eu", 33.0),
                ("meteofrance_arpege_europe", 33.2),
            ]
        ]
    )
    actuals = pd.DataFrame(
        [
            {"airport": "EDDM", "target_date": target - timedelta(days=2), "max_temp_c": 25.0},
            {"airport": "EDDM", "target_date": target - timedelta(days=1), "max_temp_c": 29.0},
        ]
    )
    common = {
        "forecasts": forecasts,
        "actuals": actuals,
        "observations": pd.DataFrame(),
        "hourly": pd.DataFrame(),
        "markets": pd.DataFrame(),
        "timezone_name": "Europe/Berlin",
        "target": target,
        "as_of": as_of,
    }
    unclustered = build_live_nowcast(**common)
    clustered = build_live_nowcast(
        **common,
        heat_regime_profile={
            "enabled": True,
            "positive_bias_multiplier": 0.4,
            "spread_multiplier": 1.3,
            "regional_models": ["icon_eu", "meteofrance_arpege_europe"],
            "regional_weight_multiplier": 1.4,
            "unconfirmed_multiplier": 1.2,
            "minimum_warm_gap_c": 0.6,
        },
    )
    assert unclustered is not None and clustered is not None
    assert clustered.live_features["rapid_heat_ramp_active"] == 1
    assert clustered.live_features["regional_cluster_active"] == 1
    assert clustered.corrected.mean > unclustered.corrected.mean
    regional_weights = clustered.current.set_index("model").model_weight
    assert regional_weights["icon_eu"] > regional_weights["ecmwf_ifs025"]
