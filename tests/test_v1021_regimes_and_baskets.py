import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from weatherman.analytics import (
    DayStatus,
    champion_challenger_metrics,
    champion_challenger_trading_metrics,
    settled_basket_performance,
)
from weatherman.decision import build_edge_basket, build_trade_decision
from weatherman.nowcast import (
    build_live_nowcast,
    maritime_advection_regime,
    maritime_low_range_regime,
    persistent_hot_regime,
    phase_vs_amplitude_regime,
)
from weatherman.shadow import build_shadow_basket


def active_day() -> DayStatus:
    return DayStatus(
        phase="heating",
        label="Heating window active",
        is_locked=False,
        minimum_bucket=None,
        maximum_bucket=None,
        remaining_heating_c=3.0,
        explanation="The day is still heating.",
    )


def ankara_markets(captured_at: datetime | None = None) -> pd.DataFrame:
    rows = []
    prices = {25: 0.05, 26: 0.10, 27: 0.40, 28: 0.10, 29: 0.15}
    for bucket, price in prices.items():
        rows.append(
            {
                "airport": "LTAC",
                "target_date": date(2026, 7, 30),
                "captured_at": captured_at,
                "event_slug": "ankara-temperature",
                "market_id": f"m{bucket}",
                "bucket_label": f"{bucket}°C",
                "bucket_low_c": bucket,
                "bucket_high_c": bucket,
                "yes_price": price,
                "best_bid": max(0.01, price - 0.01),
                "best_ask": price,
                "spread": 0.01,
                "closed": False,
                "yes_won": False,
            }
        )
    return pd.DataFrame(rows)


def test_madrid_persistent_hot_activates_despite_lower_day_on_day_forecast():
    target = date(2026, 7, 30)
    actuals = pd.DataFrame(
        [
            {"target_date": date(2026, 7, 28), "max_temp_c": 34.0},
            {"target_date": date(2026, 7, 29), "max_temp_c": 39.0},
        ]
    )
    scored = pd.DataFrame(
        [
            {
                "target_date": date(2026, 7, 29),
                "model": "AROME-HD",
                "error": -1.2,
            }
        ]
    )
    result = persistent_hot_regime(
        actuals,
        scored,
        target=target,
        forecast_mean=37.0,
        taf_guidance=None,
        profile={
            "persistent_hot": {
                "enabled": True,
                "minimum_latest_actual_c": 37.0,
                "maximum_forecast_drop_c": 2.5,
                "minimum_recent_warm_error_c": 0.6,
                "minimum_confirmations": 2,
                "positive_bias_multiplier": 0.15,
                "spread_multiplier": 1.35,
            }
        },
    )
    assert result["active"]
    assert result["forecast_vs_latest_c"] == -2.0
    assert result["recent_warm_error_c"] == -1.2
    assert result["evidence_score"] == 0.75
    assert result["intensity"] == 0.75
    assert round(float(result["bias_multiplier"]), 4) == 0.3625
    assert round(float(result["spread_multiplier"]), 4) == 1.2625


def test_madrid_persistent_hot_changes_live_distribution_and_stores_challenger():
    as_of = datetime(2026, 7, 30, 10, tzinfo=timezone.utc)
    target = as_of.date()
    run_at = as_of - timedelta(minutes=20)
    models = [
        "ecmwf_ifs025",
        "gfs_global",
        "icon_global",
        "meteofrance_arome_france_hd",
    ]
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": model,
                "run_at": run_at,
                "fetched_at": run_at,
                "target_date": target,
                "max_temp_c": maximum,
                "source": "open-meteo",
                "horizon": "Live",
            }
            for model, maximum in zip(models, [37.0, 37.0, 37.0, 40.0])
        ]
        + [
            {
                "airport": "LEMD",
                "model": model,
                "run_at": as_of - timedelta(days=1, hours=16),
                "fetched_at": as_of - timedelta(days=1, hours=16),
                "target_date": date(2026, 7, 29),
                "max_temp_c": 39.0 + error,
                "source": "open-meteo",
                "horizon": "D-1",
            }
            for model, error in zip(models, [-1.0, -1.0, -1.0, 1.0])
        ]
    )
    actuals = pd.DataFrame(
        [
            {"airport": "LEMD", "target_date": date(2026, 7, 28), "max_temp_c": 34.0},
            {"airport": "LEMD", "target_date": date(2026, 7, 29), "max_temp_c": 39.0},
        ]
    )
    result = build_live_nowcast(
        forecasts=forecasts,
        actuals=actuals,
        observations=pd.DataFrame(),
        hourly=pd.DataFrame(),
        markets=pd.DataFrame(),
        timezone_name="Europe/Madrid",
        target=target,
        as_of=as_of,
        heat_regime_profile={
            "enabled": True,
            "minimum_warm_gap_c": 0.6,
            "regional_models": ["meteofrance_arome_france_hd"],
            "unconfirmed_multiplier": 1.2,
            "persistent_hot": {
                "enabled": True,
                "minimum_latest_actual_c": 37.0,
                "minimum_latest_anomaly_c": 3.0,
                "maximum_forecast_drop_c": 2.5,
                "minimum_recent_warm_error_c": 0.6,
                "minimum_confirmations": 2,
                "positive_bias_multiplier": 0.15,
                "spread_multiplier": 1.35,
                "confidence_multiplier": 0.88,
                "regional_models": ["meteofrance_arome_france_hd"],
                "regional_weight_multiplier": 1.65,
            },
        },
    )
    assert result is not None
    assert result.live_features["persistent_hot_active"] == 1
    assert result.live_features["regional_cluster_active"] == 1
    arome = result.current[
        result.current.model == "meteofrance_arome_france_hd"
    ].iloc[0]
    assert arome.d1_bias < arome.historical_d1_bias
    challenger = result.challenger_variants["Without Persistent Hot"]
    assert result.final_forecast_mean > float(challenger["forecast_mean_c"])
    assert result.final_forecast_spread > float(challenger["spread_c"])


def test_munich_phase_fit_separates_early_curve_from_vertical_level_error():
    as_of = datetime(2026, 7, 30, 9, tzinfo=timezone.utc)
    run_at = as_of - timedelta(minutes=20)
    hourly = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "model": model,
                "run_at": run_at,
                "valid_at": as_of - timedelta(hours=2) + timedelta(hours=index),
                "temp_c": temperature,
            }
            for model in ["ECMWF", "ICON-EU"]
            for index, temperature in enumerate([20.0, 22.0, 25.0, 28.0, 31.0, 34.0])
        ]
    )
    observations = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "observed_at": as_of - timedelta(hours=2) + timedelta(hours=index),
                "temp_c": temperature,
            }
            for index, temperature in enumerate([22.0, 25.0, 28.0])
        ]
    )
    result = phase_vs_amplitude_regime(
        hourly,
        observations,
        timezone_name="Europe/Berlin",
        target=date(2026, 7, 30),
        as_of=as_of,
        hours_to_peak=4.0,
        profile={
            "enabled": True,
            "minimum_reports": 3,
            "maximum_phase_shift_hours": 2.0,
            "minimum_phase_shift_hours": 0.75,
            "minimum_rmse_gain_c": 0.3,
            "maximum_phase_rmse_c": 1.0,
            "phase_anchor_blend": 0.75,
        },
    )
    assert result["active"]
    assert result["classification"] == "phase-dominant"
    assert result["phase_shift_hours"] == 1.0
    assert abs(float(result["level_residual_after_shift_c"])) < 0.01
    assert float(result["phase_rmse_c"]) < float(result["baseline_rmse_c"])


def test_amsterdam_maritime_advection_closes_heating_tail_after_wind_shift():
    start = datetime(2026, 7, 30, 10, tzinfo=timezone.utc)
    observations = pd.DataFrame(
        [
            {
                "observed_at": start + timedelta(hours=index),
                "temp_c": temperature,
                "wind_kph": wind,
                "wind_direction": direction,
            }
            for index, temperature, wind, direction in [
                (0, 26.0, 11.0, 200.0),
                (1, 26.0, 15.0, 280.0),
                (2, 25.5, 18.0, 295.0),
                (3, 25.0, 20.0, 305.0),
            ]
        ]
    )
    result = maritime_advection_regime(
        observations,
        profile={
            "enabled": True,
            "minimum_reports": 4,
            "maritime_sectors": [[220, 340]],
            "minimum_wind_kph": 14.0,
            "strong_wind_kph": 18.0,
            "minimum_maritime_fraction": 0.67,
            "maximum_temperature_rate_cph": 0.2,
            "remaining_rise_cap_c": 0.4,
        },
    )
    assert result["active"]
    assert float(result["center_adjustment_c"]) < 0
    assert result["remaining_rise_cap_c"] == 0.4
    assert float(result["temperature_rate_cph"]) < 0


def test_istanbul_low_range_regime_damps_heating_under_stable_sea_wind():
    local_now = datetime(2026, 7, 30, 14, tzinfo=timezone.utc)
    observations = pd.DataFrame(
        [
            {
                "observed_at": local_now - timedelta(hours=4 - index),
                "temp_c": temperature,
                "wind_kph": wind,
                "wind_direction": direction,
            }
            for index, temperature, wind, direction in [
                (0, 25.0, 34.0, 30.0),
                (1, 25.5, 35.0, 35.0),
                (2, 26.0, 36.0, 40.0),
                (3, 25.8, 35.0, 35.0),
                (4, 26.0, 34.0, 30.0),
            ]
        ]
    )
    result = maritime_low_range_regime(
        observations,
        local_now=local_now,
        profile={
            "enabled": True,
            "minimum_local_hour": 10,
            "minimum_reports": 4,
            "sea_wind_sectors": [[320, 70]],
            "minimum_wind_kph": 28.0,
            "minimum_sea_wind_fraction": 0.8,
            "maximum_recent_range_c": 1.5,
            "maximum_daily_range_c": 4.5,
            "maximum_abs_temperature_rate_cph": 0.3,
            "positive_factor_multiplier": 0.15,
            "spread_multiplier": 0.85,
        },
    )
    assert result["active"]
    assert result["positive_factor_multiplier"] == 0.15
    assert result["spread_multiplier"] == 0.85
    assert float(result["daily_range_c"]) == 1.0


def test_ankara_edge_basket_blocks_gap_around_the_most_likely_bucket():
    probabilities = {25: 0.15, 26: 0.20, 27: 0.35, 28: 0.20, 29: 0.10}
    basket = build_edge_basket(probabilities, ankara_markets())
    assert basket is not None
    assert basket.bucket_labels == ("25°C", "26°C", "28°C")
    assert basket.fair_probability == 0.55
    assert basket.total_cost == 0.25
    assert round(basket.edge, 2) == 0.30
    assert not basket.top_model_included
    assert basket.middle_bucket_excluded
    assert basket.warnings == (
        "Most likely bucket excluded",
        "Middle bucket excluded",
    )

    decision = build_trade_decision(
        probabilities=probabilities,
        markets=ankara_markets(),
        forecast_confidence=85,
        day_status=active_day(),
    )
    assert decision.status == "NO BET"
    assert decision.basket == basket
    assert any("Most likely bucket excluded" in blocker for blocker in decision.blockers)


def test_shadow_basket_uses_joint_cost_and_rejects_the_ankara_gap():
    markets = ankara_markets()
    fair = {25: 0.15, 26: 0.20, 27: 0.35, 28: 0.20, 29: 0.10}
    rows = []
    for market in markets.itertuples():
        bucket = int(market.bucket_low_c)
        selected = bucket in {25, 26, 28}
        rows.append(
            {
                "market_id": market.market_id,
                "bucket_label": market.bucket_label,
                "fair_probability": fair[bucket],
                "all_in_price": market.best_ask,
                "net_edge": fair[bucket] - market.best_ask - 0.02,
                "safety_margin": 0.02,
                "fully_fillable": True,
                "status": "SHADOW BET" if selected else "WATCH",
            }
        )
    basket = build_shadow_basket(rows, markets)
    assert basket is not None
    assert basket.status == "BASKET WATCH"
    assert basket.total_cost == 0.25
    assert round(basket.net_edge, 2) == 0.24
    assert basket.warnings == (
        "Most likely bucket excluded",
        "Middle bucket excluded",
    )


def test_champion_challenger_scores_distribution_and_independent_days():
    captured = datetime(2026, 7, 30, 10, tzinfo=timezone.utc)
    variants = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "target_date": date(2026, 7, 30),
                "captured_at": captured,
                "timing": "D0 live",
                "variant": "Champion",
                "factor": None,
                "forecast_c": 27.0,
                "probabilities_json": json.dumps({27: 0.8, 26: 0.2}),
                "forecast_confidence": 80,
            },
            {
                "airport": "EDDM",
                "target_date": date(2026, 7, 30),
                "captured_at": captured,
                "timing": "D0 live",
                "variant": "Without Phase-vs-Amplitude",
                "factor": "phase_vs_amplitude",
                "forecast_c": 29.0,
                "probabilities_json": json.dumps({27: 0.2, 29: 0.8}),
                "forecast_confidence": 80,
            },
        ]
    )
    actuals = pd.DataFrame(
        [{"airport": "EDDM", "target_date": date(2026, 7, 30), "max_temp_c": 27.0}]
    )
    metrics = champion_challenger_metrics(variants, actuals)
    assert len(metrics) == 1
    row = metrics.iloc[0]
    assert row.n_days == 1
    assert row.evidence == "Case studies only"
    assert row.champion_mae == 0
    assert row.challenger_mae == 2
    assert row.mae_gain == 2
    assert row.brier_gain > 0
    assert row.log_loss_gain > 0
    assert row.exact_hit_gain == 1


def test_event_level_basket_settlement_counts_one_day_once():
    first = datetime(2026, 7, 30, 10, tzinfo=timezone.utc)
    baskets = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "target_date": date(2026, 7, 30),
                "event_slug": "ankara-temperature",
                "captured_at": first + timedelta(minutes=offset),
                "timing": "D0 live",
                "strategy": "Net-edge basket",
                "market_ids_json": json.dumps(["m25", "m26"]),
                "market_count": 2,
                "fair_probability": 0.45,
                "total_cost": 0.30,
                "net_edge": 0.15,
                "status": "SHADOW BASKET",
            }
            for offset in [0, 10]
        ]
    )
    markets = ankara_markets(first)
    final = ankara_markets(first + timedelta(hours=10))
    final["closed"] = True
    final["yes_won"] = final.market_id == "m26"
    results = settled_basket_performance(
        baskets,
        pd.concat([markets, final], ignore_index=True),
    )
    assert len(results) == 1
    assert bool(results.iloc[0].won)
    assert results.iloc[0].pnl == 0.70
    assert round(results.iloc[0].roi, 3) == 2.333


def test_champion_challenger_trading_uses_same_snapshot_and_resolved_market():
    captured = datetime(2026, 7, 30, 10, tzinfo=timezone.utc)
    variants = pd.DataFrame(
        [
            {
                "airport": "LTAC",
                "target_date": date(2026, 7, 30),
                "captured_at": captured,
                "timing": "D0 live",
                "variant": "Champion",
                "factor": None,
                "probabilities_json": json.dumps({27: 0.75, 26: 0.25}),
                "forecast_confidence": 80,
            },
            {
                "airport": "LTAC",
                "target_date": date(2026, 7, 30),
                "captured_at": captured,
                "timing": "D0 live",
                "variant": "Without Persistent Hot",
                "factor": "persistent_hot",
                "probabilities_json": json.dumps({26: 0.75, 27: 0.25}),
                "forecast_confidence": 80,
            },
        ]
    )
    open_markets = ankara_markets(captured)
    open_markets.loc[open_markets.market_id == "m26", ["yes_price", "best_ask"]] = 0.20
    open_markets.loc[open_markets.market_id == "m27", ["yes_price", "best_ask"]] = 0.20
    final = open_markets.copy()
    final["captured_at"] = captured + timedelta(hours=10)
    final["closed"] = True
    final["yes_won"] = final.market_id == "m27"
    metrics = champion_challenger_trading_metrics(
        variants,
        pd.concat([open_markets, final], ignore_index=True),
    )
    assert len(metrics) == 1
    row = metrics.iloc[0]
    assert row.champion_entries == 1
    assert row.challenger_entries == 1
    assert row.champion_hit_rate == 1
    assert row.challenger_hit_rate == 0
    assert row.champion_net_pnl > 0
    assert row.challenger_net_pnl == -1
