from datetime import date

import pandas as pd

from weatherman.analytics import DayStatus
from weatherman.decision import (
    balanced_hedge_plan,
    build_trade_decision,
    hedge_outcome_table,
    latest_prior_probabilities,
)


def active_day() -> DayStatus:
    return DayStatus(
        phase="heating",
        label="Heating window active",
        is_locked=False,
        minimum_bucket=23,
        maximum_bucket=None,
        remaining_heating_c=2.0,
        explanation="The day is still heating.",
    )


def markets() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "market_id": "23",
                "bucket_label": "23°C",
                "bucket_low_c": 23,
                "bucket_high_c": 23,
                "yes_price": 0.20,
                "best_bid": 0.19,
                "best_ask": 0.22,
                "spread": 0.03,
                "closed": False,
            },
            {
                "market_id": "24",
                "bucket_label": "24°C",
                "bucket_low_c": 24,
                "bucket_high_c": 24,
                "yes_price": 0.25,
                "best_bid": 0.24,
                "best_ask": 0.26,
                "spread": 0.02,
                "closed": False,
            },
        ]
    )


def test_decision_engine_requires_edge_confidence_and_execution_quality():
    decision = build_trade_decision(
        probabilities={23: 0.25, 24: 0.40, 25: 0.35},
        markets=markets(),
        forecast_confidence=78,
        day_status=active_day(),
        previous_probabilities={"24°C": 0.35},
        recommendations_enabled=True,
    )
    assert decision.status == "BET"
    assert decision.bucket_label == "24°C"
    assert round(decision.edge or 0, 2) == 0.14
    assert round(decision.probability_change or 0, 2) == 0.05

    low_confidence = build_trade_decision(
        probabilities={23: 0.25, 24: 0.40, 25: 0.35},
        markets=markets(),
        forecast_confidence=55,
        day_status=active_day(),
        recommendations_enabled=True,
    )
    assert low_confidence.status == "WATCH"
    assert any("confidence" in blocker.lower() for blocker in low_confidence.blockers)


def test_decision_engine_blocks_when_metar_is_pending():
    decision = build_trade_decision(
        probabilities={23: 0.25, 24: 0.42, 25: 0.33},
        markets=markets(),
        forecast_confidence=90,
        day_status=active_day(),
        metar_pending=True,
    )
    assert decision.status == "NO BET"
    assert any("METAR" in blocker for blocker in decision.blockers)


def test_decision_engine_blocks_a_large_edge_when_models_are_stale():
    decision = build_trade_decision(
        probabilities={23: 0.10, 24: 0.70, 25: 0.20},
        markets=markets(),
        forecast_confidence=90,
        day_status=active_day(),
        forecast_stale=True,
    )
    assert decision.status == "NO BET"
    assert any("current weather models" in blocker for blocker in decision.blockers)


def test_recommendations_default_to_research_only_until_calibrated():
    decision = build_trade_decision(
        probabilities={23: 0.25, 24: 0.40, 25: 0.35},
        markets=markets(),
        forecast_confidence=85,
        day_status=active_day(),
    )
    assert decision.status == "RESEARCH ONLY"
    assert any("calibration" in blocker.lower() for blocker in decision.blockers)


def test_non_top_cheap_and_extreme_disagreements_are_hard_blocked():
    non_top_markets = markets().copy()
    non_top_markets.loc[non_top_markets.market_id == "23", "best_ask"] = 0.10
    non_top_markets.loc[non_top_markets.market_id == "24", "best_ask"] = 0.45
    non_top = build_trade_decision(
        probabilities={23: 0.24, 24: 0.50, 25: 0.26},
        markets=non_top_markets,
        forecast_confidence=90,
        day_status=active_day(),
        recommendations_enabled=True,
    )
    assert non_top.status == "NO BET"
    assert any("most likely" in blocker.lower() for blocker in non_top.blockers)

    cheap_markets = markets().copy()
    cheap_markets.loc[cheap_markets.market_id == "24", "best_ask"] = 0.05
    cheap = build_trade_decision(
        probabilities={23: 0.10, 24: 0.19, 25: 0.71},
        markets=cheap_markets[cheap_markets.market_id == "24"],
        forecast_confidence=90,
        day_status=active_day(),
        recommendations_enabled=True,
    )
    assert cheap.status == "NO BET"
    assert any("cheap-tail" in blocker for blocker in cheap.blockers)

    conflict = build_trade_decision(
        probabilities={23: 0.10, 24: 0.60, 25: 0.30},
        markets=markets()[markets().market_id == "24"],
        forecast_confidence=90,
        day_status=active_day(),
        recommendations_enabled=True,
    )
    assert conflict.status == "NO BET"
    assert any("conflict" in blocker.lower() for blocker in conflict.blockers)


def test_latest_prior_probability_view_uses_latest_capture():
    frame = pd.DataFrame(
        [
            {
                "target_date": date(2026, 7, 26),
                "captured_at": "2026-07-26T08:00:00Z",
                "bucket_label": "24°C",
                "model_probability": 0.20,
            },
            {
                "target_date": date(2026, 7, 26),
                "captured_at": "2026-07-26T09:00:00Z",
                "bucket_label": "24°C",
                "model_probability": 0.31,
            },
        ]
    )
    assert latest_prior_probabilities(frame, date(2026, 7, 26)) == {"24°C": 0.31}


def test_hedge_calculator_balances_selected_bucket_payouts():
    plan = balanced_hedge_plan(
        primary_bucket="23°C",
        primary_stake=10,
        primary_price=0.25,
        hedge_bucket="24°C",
        hedge_price=0.40,
    )
    assert plan.balanced_hedge_stake == 16
    assert plan.covered_result == 14
    assert plan.uncovered_result == -26

    outcomes = hedge_outcome_table(
        outcome_buckets=["23°C", "24°C", "25°C"],
        primary_bucket="23°C",
        primary_stake=10,
        primary_price=0.25,
        hedge_bucket="24°C",
        hedge_stake=16,
        hedge_price=0.40,
    )
    results = {row["Outcome"]: row["Net P/L"] for row in outcomes}
    assert results == {"23°C": 14, "24°C": 14, "25°C": -26}
