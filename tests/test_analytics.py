from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from weatherman.analytics import (
    assess_day_status,
    condition_probability_range,
    condition_probabilities,
    consensus,
    detect_market_model_conflict,
    fixed_decision_snapshots,
    flat_bet_simulation,
    forecast_ladder_frame,
    forecast_ladder_metrics,
    forecast_scorecards,
    heat_spike_assessment,
    historical_d1_ladder,
    historical_price_strategy_simulation,
    market_edges,
    metar_schedule_status,
    model_metrics,
    model_weight_map,
    probability_for_range,
    resolved_market_range,
    score_frame,
    settled_signal_performance,
    settled_shadow_performance,
    settled_strategy_performance,
    trading_airport_scorecards,
)


def test_fixed_decision_snapshots_never_use_future_information():
    snapshots = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "target_date": date(2026, 7, 21),
                "captured_at": "2026-07-20T17:55:00Z",
                "raw_model_mean_c": 36.0,
            },
            {
                "airport": "LEMD",
                "target_date": date(2026, 7, 21),
                "captured_at": "2026-07-20T18:05:00Z",
                "raw_model_mean_c": 37.0,
            },
            {
                "airport": "LEMD",
                "target_date": date(2026, 7, 21),
                "captured_at": "2026-07-21T07:55:00Z",
                "raw_model_mean_c": 38.0,
            },
            {
                "airport": "LEMD",
                "target_date": date(2026, 7, 21),
                "captured_at": "2026-07-21T08:05:00Z",
                "raw_model_mean_c": 39.0,
            },
        ]
    )
    selected = fixed_decision_snapshots(
        snapshots,
        {"LEMD": "Europe/Madrid"},
    ).set_index("timing")
    assert selected.loc["D-1 Evening · 20:00", "raw_model_mean_c"] == 36.0
    assert selected.loc["D0 Morning · 10:00", "raw_model_mean_c"] == 38.0
    assert set(selected.checkpoint_gap_minutes) == {5.0}


def test_metar_guard_starts_seven_minutes_before_routine_issue():
    status = metar_schedule_status(
        as_of=datetime(2026, 7, 21, 11, 53, tzinfo=ZoneInfo("UTC")),
        latest_observation_at=datetime(2026, 7, 21, 11, 30, tzinfo=ZoneInfo("UTC")),
        routine_minutes=[0, 30],
    )
    assert status.is_pending
    assert status.due_at == datetime(2026, 7, 21, 12, 0, tzinfo=ZoneInfo("UTC"))

    outside_guard = metar_schedule_status(
        as_of=datetime(2026, 7, 21, 11, 52, tzinfo=ZoneInfo("UTC")),
        latest_observation_at=datetime(2026, 7, 21, 11, 30, tzinfo=ZoneInfo("UTC")),
        routine_minutes=[0, 30],
    )
    assert not outside_guard.is_pending


def test_near_certain_market_conflict_is_only_a_safety_flag():
    probabilities = {36: 0.45, 37: 0.40, 38: 0.15}
    markets = pd.DataFrame(
        [
            {
                "market_id": "37",
                "bucket_label": "37°C",
                "bucket_low_c": 37,
                "bucket_high_c": 37,
                "yes_price": 0.99,
                "closed": False,
            }
        ]
    )
    conflict = detect_market_model_conflict(probabilities, markets)
    assert conflict.is_conflict
    assert conflict.model_probability == 0.40
    assert probabilities == {36: 0.45, 37: 0.40, 38: 0.15}


def test_forecast_ladder_scores_each_stage_separately():
    snapshots = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "target_date": date(2026, 7, 21),
                "captured_at": datetime(2026, 7, 21, 12, tzinfo=ZoneInfo("UTC")),
                "timing": "D0 live",
                "hours_to_peak": 3.5,
                "raw_model_mean_c": 39.0,
                "weighted_raw_c": 38.7,
                "bias_corrected_equal_c": 38.2,
                "bias_corrected_c": 38.0,
                "metar_conditioned_c": 37.2,
                "final_forecast_c": 37.4,
            }
        ]
    )
    actuals = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "target_date": date(2026, 7, 21),
                "max_temp_c": 37.0,
                "actual_source": "airport METAR",
            }
        ]
    )
    scored = forecast_ladder_frame(snapshots, actuals)
    metrics = forecast_ladder_metrics(scored)
    assert metrics.stage.tolist() == [
        "Raw model mean",
        "Weighted raw ensemble",
        "Bias corrected · equal weight",
        "Bias corrected · performance weighted",
        "METAR conditioned",
        "Final incl. TAF",
    ]
    metar = metrics[metrics.stage == "METAR conditioned"].iloc[0]
    final = metrics[metrics.stage == "Final incl. TAF"].iloc[0]
    assert round(metar.mae, 2) == 0.20
    assert round(final.mae, 2) == 0.40


def test_historical_d1_ladder_uses_only_earlier_errors():
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": model,
                "run_at": datetime(2026, 7, day - 1, 12, tzinfo=ZoneInfo("UTC")),
                "target_date": date(2026, 7, day),
                "max_temp_c": forecast,
                "horizon": "D-1",
            }
            for day, model, forecast in [
                (10, "a", 31.0),
                (10, "b", 30.0),
                (11, "a", 32.0),
                (11, "b", 31.0),
            ]
        ]
    )
    actuals = pd.DataFrame(
        [
            {"airport": "LEMD", "target_date": date(2026, 7, 10), "max_temp_c": 30.0},
            {"airport": "LEMD", "target_date": date(2026, 7, 11), "max_temp_c": 31.0},
        ]
    )
    result = historical_d1_ladder(forecasts, actuals)
    day_two = result[result.target_date == date(2026, 7, 11)]
    raw = day_two[day_two.stage == "Raw model mean"].iloc[0]
    corrected = day_two[
        day_two.stage == "Bias corrected · equal weight"
    ].iloc[0]
    assert raw.forecast_c == 31.5
    assert corrected.forecast_c == 31.0


def test_consensus_bias_correction():
    result = consensus([35, 36, 37], [1, 0, -1])
    assert result.mean == 36
    assert abs(sum(result.probability_by_bucket.values()) - 1) < 1e-9


def test_consensus_supports_dynamic_model_weights():
    result = consensus([10, 20], weights=[0.9, 0.1])
    assert result.mean == 11


def test_scoring_and_metrics():
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": "a",
                "run_at": "2026-07-01",
                "target_date": "2026-07-02",
                "max_temp_c": 35,
            },
            {
                "airport": "LEMD",
                "model": "a",
                "run_at": "2026-07-01 12:00",
                "target_date": "2026-07-02",
                "max_temp_c": 36,
            },
        ]
    )
    actuals = pd.DataFrame([{"airport": "LEMD", "target_date": "2026-07-02", "max_temp_c": 35}])
    scored = score_frame(forecasts, actuals)
    assert len(scored) == 1
    assert model_metrics(scored).iloc[0].bias == 1


def test_flat_bet_simulation():
    scored = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": "test-model",
                "target_date": "2026-07-02",
                "max_temp_c_forecast": 35.1,
                "max_temp_c_actual": 35.0,
            },
            {
                "airport": "LEMD",
                "model": "test-model",
                "target_date": "2026-07-03",
                "max_temp_c_forecast": 36.2,
                "max_temp_c_actual": 35.0,
            },
        ]
    )
    result = flat_bet_simulation(scored)
    assert result.pnl.tolist() == [1.0, -1.0]


def test_metar_floor_removes_impossible_buckets():
    conditioned = condition_probabilities({34: 0.4, 35: 0.35, 36: 0.2, 37: 0.05}, 35)
    assert 34 not in conditioned
    assert abs(sum(conditioned.values()) - 1) < 1e-9
    assert round(conditioned[35], 3) == 0.583


def test_completed_day_locks_probability_to_observed_maximum():
    status = assess_day_status(
        target_date=date(2026, 7, 20),
        local_now=datetime(2026, 7, 20, 21, tzinfo=ZoneInfo("Europe/Madrid")),
        observed_max=35.0,
        observation_age_hours=0.4,
        heating_rate=-0.8,
        remaining_model_rise=0.1,
        future_radiation_max=0,
    )
    assert status.label == "Peak locked"
    assert status.is_locked
    assert condition_probability_range({35: 0.3, 36: 0.5, 37: 0.2}, 35, 35) == {
        35: 1.0
    }


def test_day_stays_open_while_sunlight_and_warming_remain():
    status = assess_day_status(
        target_date=date(2026, 7, 20),
        local_now=datetime(2026, 7, 20, 17, tzinfo=ZoneInfo("Europe/Madrid")),
        observed_max=35.0,
        observation_age_hours=0.3,
        heating_rate=0.0,
        remaining_model_rise=0.8,
        future_radiation_max=250,
    )
    assert status.label == "Heating window open"
    assert not status.is_locked
    assert status.maximum_bucket is None


def test_late_evening_cooling_can_lock_when_radiation_is_missing():
    status = assess_day_status(
        target_date=date(2026, 7, 20),
        local_now=datetime(2026, 7, 20, 21, tzinfo=ZoneInfo("Europe/Madrid")),
        observed_max=37.0,
        latest_observed_temp=35.0,
        observation_age_hours=0.1,
        heating_rate=-1.0,
        remaining_model_rise=0.0,
        future_radiation_max=None,
    )
    assert status.label == "Peak locked"
    assert status.minimum_bucket == status.maximum_bucket == 37


def test_heat_spike_confirmation():
    result = heat_spike_assessment(
        forecast_mean=36,
        recent_baseline=31,
        run_trend=1.1,
        model_spread=0.8,
        observed_temp=34,
        observed_dewpoint=15,
        expected_temp_now=32.5,
        heating_rate=1.6,
        cloud_cover=10,
    )
    assert result.score >= 70
    assert result.status == "Confirmed"
    assert result.adjustment_c > 0


def test_heat_spike_wind_uses_airport_specific_warm_and_cooling_sectors():
    common = {
        "forecast_mean": 34,
        "recent_baseline": 32,
        "run_trend": 0.0,
        "model_spread": 1.2,
        "observed_temp": 31,
        "observed_dewpoint": 20,
        "expected_temp_now": 31,
        "heating_rate": 0.5,
        "cloud_cover": 30,
        "wind_speed_kph": 22,
        "warm_wind_sectors": [[120, 230]],
        "cool_wind_sectors": [[280, 60]],
        "wind_source": "METAR",
    }
    warm = heat_spike_assessment(**common, wind_direction_deg=180)
    cooling = heat_spike_assessment(**common, wind_direction_deg=330)
    assert warm.score > cooling.score
    assert warm.adjustment_c > cooling.adjustment_c
    assert any("warm sector" in signal for signal in warm.signals)
    assert any("cooling sector" in signal for signal in cooling.signals)


def test_market_range_probabilities_and_actionable_edge():
    probabilities = {34: 0.1, 35: 0.3, 36: 0.5, 37: 0.1}
    assert probability_for_range(probabilities, None, 34) == 0.1
    assert probability_for_range(probabilities, 37, None) == 0.1
    markets = pd.DataFrame(
        [
            {
                "bucket_label": "36°C",
                "bucket_low_c": 36,
                "bucket_high_c": 36,
                "yes_price": 0.39,
                "best_ask": 0.41,
            }
        ]
    )
    result = market_edges(probabilities, markets)
    assert round(result.iloc[0].edge, 2) == 0.09
    assert result.iloc[0].signal == "Uncalibrated disagreement"


def test_closed_market_is_not_marked_as_an_edge_and_supplies_winner():
    markets = pd.DataFrame(
        [
            {
                "market_id": "1",
                "captured_at": "2026-07-20T21:00:00Z",
                "bucket_label": "35°C",
                "bucket_low_c": 35,
                "bucket_high_c": 35,
                "yes_price": 1.0,
                "best_ask": 1.0,
                "closed": True,
                "yes_won": True,
            },
            {
                "market_id": "2",
                "captured_at": "2026-07-20T21:00:00Z",
                "bucket_label": "36°C",
                "bucket_low_c": 36,
                "bucket_high_c": 36,
                "yes_price": 0.0,
                "best_ask": 0.01,
                "closed": True,
                "yes_won": False,
            },
        ]
    )
    result = market_edges({35: 1.0}, markets)
    assert set(result.signal) == {"No material disagreement"}
    assert resolved_market_range(markets) == (35.0, 35.0, "35°C")

    status = assess_day_status(
        target_date=date(2026, 7, 20),
        local_now=datetime(2026, 7, 21, 9, tzinfo=ZoneInfo("Europe/Madrid")),
        observed_max=None,
        observation_age_hours=None,
        heating_rate=None,
        remaining_model_rise=None,
        future_radiation_max=None,
        resolved_lower_c=35,
        resolved_upper_c=35,
    )
    assert status.label == "Officially resolved"
    assert status.minimum_bucket == status.maximum_bucket == 35


def test_real_price_signal_performance_uses_first_entry_and_official_result():
    signals = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "target_date": "2026-07-20",
                "market_id": "winner",
                "bucket_label": "35°C",
                "captured_at": "2026-07-19T18:00:00Z",
                "timing": "D-1 or earlier",
                "model_probability": 0.50,
                "buy_price": 0.40,
                "edge": 0.10,
                "signal": "Possible edge",
            },
            {
                "airport": "LEMD",
                "target_date": "2026-07-20",
                "market_id": "winner",
                "bucket_label": "35°C",
                "captured_at": "2026-07-20T08:00:00Z",
                "timing": "D0 morning",
                "model_probability": 0.55,
                "buy_price": 0.30,
                "edge": 0.25,
                "signal": "Possible edge",
            },
            {
                "airport": "LEMD",
                "target_date": "2026-07-20",
                "market_id": "loser",
                "bucket_label": "36°C",
                "captured_at": "2026-07-19T19:00:00Z",
                "timing": "D-1 or earlier",
                "model_probability": 0.30,
                "buy_price": 0.20,
                "edge": 0.10,
                "signal": "Possible edge",
            },
            {
                "airport": "LEMD",
                "target_date": "2026-07-20",
                "market_id": "ignored",
                "bucket_label": "37°C",
                "captured_at": "2026-07-19T20:00:00Z",
                "timing": "D-1 or earlier",
                "model_probability": 0.12,
                "buy_price": 0.07,
                "edge": 0.05,
                "signal": "Watch",
            },
        ]
    )
    markets = pd.DataFrame(
        [
            {
                "market_id": "winner",
                "captured_at": "2026-07-21T08:00:00Z",
                "closed": True,
                "yes_won": True,
            },
            {
                "market_id": "loser",
                "captured_at": "2026-07-21T08:00:00Z",
                "closed": True,
                "yes_won": False,
            },
            {
                "market_id": "ignored",
                "captured_at": "2026-07-21T08:00:00Z",
                "closed": True,
                "yes_won": False,
            },
        ]
    )
    result = settled_signal_performance(signals, markets)
    assert result.market_id.tolist() == ["winner", "loser"]
    assert result.buy_price.tolist() == [0.4, 0.2]
    assert result.pnl.tolist() == [1.5, -1.0]
    assert result.cumulative_pnl.tolist() == [1.5, 0.5]


def test_signal_performance_waits_for_a_confirmed_winner():
    signals = pd.DataFrame(
        [
            {
                "airport": "EPWA",
                "target_date": "2026-07-20",
                "market_id": "unresolved-20",
                "bucket_label": "20°C",
                "captured_at": "2026-07-20T08:00:00Z",
                "timing": "D0 morning",
                "model_probability": 0.4,
                "buy_price": 0.2,
                "edge": 0.2,
                "signal": "Possible edge",
            }
        ]
    )
    markets = pd.DataFrame(
        [
            {
                "event_slug": "unresolved-event",
                "market_id": market_id,
                "captured_at": "2026-07-20T22:00:00Z",
                "closed": True,
                "yes_won": False,
            }
            for market_id in ["unresolved-20", "unresolved-21"]
        ]
    )
    assert settled_signal_performance(signals, markets).empty


def test_shadow_performance_uses_stored_fee_adjusted_shares():
    evaluations = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "target_date": date(2026, 7, 29),
                "market_id": "winner",
                "bucket_label": "34°C",
                "captured_at": "2026-07-29T12:00:00Z",
                "timing": "D0 live",
                "fair_probability": 0.40,
                "average_fill_price": 0.20,
                "fee_per_share": 0.008,
                "all_in_price": 0.208,
                "slippage": 0.0,
                "net_edge": 0.172,
                "stake_usdc": 10.0,
                "shares": 48.076923,
                "total_cost_usdc": 10.0,
                "fully_fillable": True,
                "status": "SHADOW BET",
            }
        ]
    )
    markets = pd.DataFrame(
        [
            {
                "event_slug": "munich-2026-07-29",
                "market_id": "winner",
                "captured_at": "2026-07-30T00:00:00Z",
                "closed": True,
                "yes_won": True,
            },
            {
                "event_slug": "munich-2026-07-29",
                "market_id": "loser",
                "captured_at": "2026-07-30T00:00:00Z",
                "closed": True,
                "yes_won": False,
            },
        ]
    )
    settled = settled_shadow_performance(evaluations, markets)
    assert len(settled) == 1
    assert settled.iloc[0].won
    assert round(settled.iloc[0].pnl, 2) == 38.08
    assert round(settled.iloc[0].roi, 3) == 3.808


def test_empty_strategy_performance_keeps_filterable_schema():
    result = settled_strategy_performance(pd.DataFrame(), pd.DataFrame())

    assert result.empty
    assert {
        "airport",
        "target_date",
        "market_id",
        "strategy",
        "timing",
        "won",
        "pnl",
    }.issubset(result.columns)
    assert result[result.airport == "EDDM"].empty


def test_empty_historical_price_simulation_keeps_filterable_schema():
    result = historical_price_strategy_simulation(
        pd.DataFrame(),
        pd.DataFrame(),
    )

    assert result.empty
    assert {
        "airport",
        "target_date",
        "strategy",
        "timing",
        "won",
        "pnl",
    }.issubset(result.columns)
    assert result[result.airport == "LTFM"].empty


def test_airport_model_weights_and_walk_forward_scorecard():
    start = date(2026, 1, 1)
    forecasts = []
    actuals = []
    for offset in range(100):
        target = start + timedelta(days=offset)
        actual = 20 + offset % 5
        actuals.append(
            {"airport": "LEMD", "target_date": target, "max_temp_c": actual}
        )
        for model, error in [
            ("accurate", 0.2),
            ("weak", 2.0 if offset % 2 == 0 else -2.0),
        ]:
            forecasts.append(
                {
                    "airport": "LEMD",
                    "model": model,
                    "run_at": datetime.combine(target, datetime.min.time())
                    - timedelta(days=1),
                    "target_date": target,
                    "max_temp_c": actual + error,
                    "source": "previous-runs",
                    "horizon": "D-1",
                }
            )
    forecast_frame = pd.DataFrame(forecasts)
    actual_frame = pd.DataFrame(actuals)
    scored = score_frame(forecast_frame, actual_frame)
    weights = model_weight_map(scored)
    assert weights["accurate"] > weights["weak"]

    cards = forecast_scorecards(forecast_frame, actual_frame)
    recent = cards[cards.window_days == 90]
    assert "Weighted ensemble" in set(recent.model)
    accurate_score = recent[recent.model == "accurate"].iloc[0].forecast_score
    weak_score = recent[recent.model == "weak"].iloc[0].forecast_score
    assert accurate_score > weak_score


def test_trade_score_is_gated_by_independent_days():
    performance = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "target_date": date(2026, 1, 1) + timedelta(days=index),
                "market_id": f"market-{index}",
                "won": index % 3 != 0,
                "pnl": 0.5 if index % 3 != 0 else -1.0,
                "edge": 0.12,
            }
            for index in range(30)
        ]
    )
    probability_records = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model_probability": 0.6,
                "outcome": float(index % 3 != 0),
                "model_brier": 0.16,
                "market_brier": 0.20,
                "model_market_gap": 0.1,
            }
            for index in range(100)
        ]
    )
    gated = trading_airport_scorecards(performance.iloc[:9], probability_records).iloc[0]
    assert gated.confidence == "Not enough data"
    assert pd.isna(gated.trade_score)

    developing = trading_airport_scorecards(performance, probability_records).iloc[0]
    assert developing.confidence == "Developing"
    assert pd.notna(developing.trade_score)
    assert pd.notna(developing.sharpe)
    assert pd.notna(developing.calibration_error)
