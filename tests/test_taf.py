import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from weatherman.taf import (
    build_taf_guidance,
    taf_verification_frame,
    taf_verification_metrics,
)


def report(
    *,
    issue: datetime,
    maximum: float | None,
    maximum_at: datetime | None,
    clouds: list[dict] | None = None,
    weather: str | None = None,
    wind_direction: int | str = 240,
    gust: int | None = 25,
) -> dict:
    return {
        "airport": "LEMD",
        "issue_time": issue,
        "collected_at": issue + timedelta(minutes=5),
        "valid_from": datetime(2026, 7, 21, 0, tzinfo=timezone.utc),
        "valid_to": datetime(2026, 7, 22, 6, tzinfo=timezone.utc),
        "raw_taf": "TAF LEMD TEST",
        "is_amended": False,
        "is_corrected": False,
        "max_temp_c": maximum,
        "max_temp_at": maximum_at,
        "periods_json": json.dumps(
            [
                {
                    "time_from": "2026-07-21T10:00:00+00:00",
                    "time_to": "2026-07-21T18:00:00+00:00",
                    "change": "TEMPO",
                    "probability": 40,
                    "wind_direction": wind_direction,
                    "wind_speed_kt": 12,
                    "wind_gust_kt": gust,
                    "weather": weather,
                    "clouds": clouds or [],
                }
            ]
        ),
    }


def test_taf_conflict_is_limited_and_flags_peak_weather():
    as_of = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    tafs = pd.DataFrame(
        [
            report(
                issue=as_of - timedelta(hours=1),
                maximum=36,
                maximum_at=datetime(2026, 7, 21, 16, tzinfo=timezone.utc),
                clouds=[{"cover": "BKN", "base": 3000, "type": "CB"}],
                weather="TSRA",
            )
        ]
    )
    guidance = build_taf_guidance(
        tafs,
        timezone_name="Europe/Madrid",
        target=date(2026, 7, 21),
        as_of=as_of,
        model_mean=39,
        wind_profile={"warm_sectors": [[120, 230]], "cool_sectors": [[240, 60]]},
    )
    assert guidance is not None
    assert guidance.agreement == "Contradicts model"
    assert guidance.center_adjustment_c == -0.25
    assert guidance.spread_addition_c == 0.45
    assert guidance.thunderstorm_risk
    assert guidance.heat_score_points == -12


def test_taf_without_tx_guides_conditions_without_moving_temperature_center():
    as_of = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    tafs = pd.DataFrame(
        [
            report(
                issue=as_of - timedelta(hours=1),
                maximum=None,
                maximum_at=None,
                clouds=[{"cover": "NSC", "base": None, "type": None}],
                gust=None,
            )
        ]
    )
    guidance = build_taf_guidance(
        tafs,
        timezone_name="Europe/Madrid",
        target=date(2026, 7, 21),
        as_of=as_of,
        model_mean=39,
    )
    assert guidance is not None
    assert guidance.agreement == "Neutral · no TX issued"
    assert guidance.center_adjustment_c == 0
    assert guidance.cloud_risk == "No significant cloud near peak"


def test_taf_exposes_future_rain_to_clear_timing_without_moving_center():
    as_of = datetime(2026, 7, 21, 10, tzinfo=timezone.utc)
    row = report(
        issue=as_of - timedelta(hours=1),
        maximum=27,
        maximum_at=datetime(2026, 7, 21, 15, tzinfo=timezone.utc),
    )
    row["periods_json"] = json.dumps(
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
    )
    guidance = build_taf_guidance(
        pd.DataFrame([row]),
        timezone_name="Europe/Amsterdam",
        target=date(2026, 7, 21),
        as_of=as_of,
        model_mean=27,
    )
    assert guidance is not None
    assert guidance.post_rain_reheating_predicted
    assert guidance.precipitation_end_at == datetime(
        2026, 7, 21, 12, tzinfo=timezone.utc
    )
    assert guidance.clearing_at == datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    assert any("rain ends" in signal for signal in guidance.signals)
    assert guidance.center_adjustment_c == 0


def test_taf_exposes_cloud_clearance_without_requiring_rain():
    as_of = datetime(2026, 7, 21, 10, tzinfo=timezone.utc)
    row = report(
        issue=as_of - timedelta(hours=1),
        maximum=31,
        maximum_at=datetime(2026, 7, 21, 16, tzinfo=timezone.utc),
    )
    row["periods_json"] = json.dumps(
        [
            {
                "time_from": "2026-07-21T09:00:00+00:00",
                "time_to": "2026-07-21T12:00:00+00:00",
                "change": "TEMPO",
                "weather": None,
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
    )
    guidance = build_taf_guidance(
        pd.DataFrame([row]),
        timezone_name="Europe/Berlin",
        target=date(2026, 7, 21),
        as_of=as_of,
        model_mean=30,
    )
    assert guidance is not None
    assert guidance.cloud_clearance_reheating_predicted
    assert not guidance.post_rain_reheating_predicted
    assert guidance.clearing_at == datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    assert any("broken/overcast" in signal for signal in guidance.signals)


def test_taf_temperature_influence_expires_after_tx_when_metar_is_cooling():
    target = date(2026, 7, 21)
    tafs = pd.DataFrame(
        [
            report(
                issue=datetime(2026, 7, 21, 10, tzinfo=timezone.utc),
                maximum=39,
                maximum_at=datetime(2026, 7, 21, 16, tzinfo=timezone.utc),
            )
        ]
    )
    guidance = build_taf_guidance(
        tafs,
        timezone_name="Europe/Madrid",
        target=target,
        as_of=datetime(2026, 7, 21, 19, tzinfo=timezone.utc),
        model_mean=37,
        observed_cooling=True,
    )
    assert guidance is not None
    assert guidance.max_temp_c == 39
    assert not guidance.temperature_influence_active
    assert guidance.center_adjustment_c == 0
    assert guidance.spread_addition_c == 0


def test_next_day_taf_tx_does_not_move_selected_day():
    tafs = pd.DataFrame(
        [
            report(
                issue=datetime(2026, 7, 21, 17, tzinfo=timezone.utc),
                maximum=41,
                maximum_at=datetime(2026, 7, 22, 16, tzinfo=timezone.utc),
            )
        ]
    )
    guidance = build_taf_guidance(
        tafs,
        timezone_name="Europe/Madrid",
        target=date(2026, 7, 21),
        as_of=datetime(2026, 7, 21, 18, tzinfo=timezone.utc),
        model_mean=37,
    )
    assert guidance is not None
    assert guidance.max_temp_c is None
    assert guidance.center_adjustment_c == 0


def test_taf_change_and_timing_verification_use_latest_available_report():
    target = date(2026, 7, 21)
    maximum_at = datetime(2026, 7, 21, 16, tzinfo=timezone.utc)
    tafs = pd.DataFrame(
        [
            report(
                issue=datetime(2026, 7, 20, 18, tzinfo=timezone.utc),
                maximum=37,
                maximum_at=maximum_at,
            ),
            report(
                issue=datetime(2026, 7, 20, 21, tzinfo=timezone.utc),
                maximum=38,
                maximum_at=maximum_at,
            ),
        ]
    )
    guidance = build_taf_guidance(
        tafs,
        timezone_name="Europe/Madrid",
        target=target,
        as_of=datetime(2026, 7, 20, 22, tzinfo=timezone.utc),
        model_mean=38,
    )
    assert guidance is not None
    assert guidance.change_summary == "TX changed +1 °C"

    actuals = pd.DataFrame(
        [{"airport": "LEMD", "target_date": target, "max_temp_c": 39.0}]
    )
    scored = taf_verification_frame(
        tafs,
        actuals,
        {"LEMD": "Europe/Madrid"},
    )
    assert len(scored) == 2
    assert set(scored.timing) == {"D-1"}
    assert scored.max_temp_c_taf.tolist() == [37, 38]
    metrics = taf_verification_metrics(scored)
    assert metrics.iloc[0].mae == 1.5
