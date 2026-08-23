from datetime import date, datetime, timezone

import pandas as pd

from weatherman.analytics import DayStatus
from weatherman.shadow import (
    estimate_market_buy,
    evaluate_shadow_markets,
    taker_fee_per_share,
)


def active_day() -> DayStatus:
    return DayStatus(
        phase="heating",
        label="Heating window active",
        is_locked=False,
        minimum_bucket=34,
        maximum_bucket=None,
        remaining_heating_c=2.0,
        explanation="The day is still heating.",
    )


def test_market_buy_walks_depth_and_adds_dynamic_weather_fee():
    fill = estimate_market_buy(
        [
            {"price": "0.25", "size": "100"},
            {"price": "0.20", "size": "10"},
        ],
        budget_usdc=10,
        fee_rate=0.05,
    )
    assert round(taker_fee_per_share(0.20), 6) == 0.008
    assert fill.fully_fillable
    assert fill.best_ask == 0.20
    assert fill.average_fill_price is not None
    assert fill.average_fill_price > fill.best_ask
    assert fill.all_in_price is not None
    assert fill.all_in_price > fill.average_fill_price
    assert fill.slippage is not None and fill.slippage > 0
    assert round(fill.total_cost_usdc, 6) == 10


def test_shadow_bet_requires_net_edge_after_fee_slippage_and_safety_margin():
    captured_at = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    markets = pd.DataFrame(
        [
            {
                "event_slug": "munich-temperature",
                "market_id": "market-34",
                "token_id": "yes-34",
                "bucket_label": "34°C",
                "bucket_low_c": 34,
                "bucket_high_c": 34,
                "yes_price": 0.20,
                "best_bid": 0.19,
                "best_ask": 0.20,
                "closed": False,
            }
        ]
    )
    rows = evaluate_shadow_markets(
        airport="EDDM",
        target=date(2026, 7, 29),
        captured_at=captured_at,
        timing="D0 live",
        probabilities={34: 0.34, 35: 0.66},
        markets=markets,
        books={
            "yes-34": {
                "observed_at": captured_at,
                "hash": "book-1",
                "bids": [{"price": "0.19", "size": "100"}],
                "asks": [{"price": "0.20", "size": "100"}],
                "min_order_size": "5",
            }
        },
        forecast_confidence=80,
        day_status=active_day(),
        recommendations_enabled=True,
    )
    assert len(rows) == 1
    evaluation = rows[0]
    assert evaluation["status"] == "SHADOW BET"
    assert evaluation["fully_fillable"]
    assert evaluation["net_edge"] < evaluation["gross_edge"]
    assert evaluation["estimated_fee_usdc"] > 0
    assert evaluation["stake_usdc"] == 10


def test_shadow_watcher_defaults_to_research_only():
    captured_at = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    markets = pd.DataFrame(
        [
            {
                "event_slug": "munich-temperature",
                "market_id": "market-34",
                "token_id": "yes-34",
                "bucket_label": "34°C",
                "bucket_low_c": 34,
                "bucket_high_c": 34,
                "yes_price": 0.20,
                "best_bid": 0.19,
                "best_ask": 0.20,
                "closed": False,
            }
        ]
    )
    rows = evaluate_shadow_markets(
        airport="EDDM",
        target=date(2026, 7, 29),
        captured_at=captured_at,
        timing="D0 live",
        probabilities={34: 0.34, 35: 0.66},
        markets=markets,
        books={
            "yes-34": {
                "observed_at": captured_at,
                "bids": [{"price": "0.19", "size": "100"}],
                "asks": [{"price": "0.20", "size": "100"}],
                "min_order_size": "5",
            }
        },
        forecast_confidence=80,
        day_status=active_day(),
    )
    assert rows[0]["status"] == "RESEARCH ONLY"
    assert "calibration" in rows[0]["blockers_json"].lower()


def test_shadow_watcher_blocks_a_book_without_enough_depth():
    captured_at = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    markets = pd.DataFrame(
        [
            {
                "event_slug": "munich-temperature",
                "market_id": "market-34",
                "token_id": "yes-34",
                "bucket_label": "34°C",
                "bucket_low_c": 34,
                "bucket_high_c": 34,
                "yes_price": 0.20,
                "closed": False,
            }
        ]
    )
    rows = evaluate_shadow_markets(
        airport="EDDM",
        target=date(2026, 7, 29),
        captured_at=captured_at,
        timing="D0 live",
        probabilities={34: 0.70},
        markets=markets,
        books={
            "yes-34": {
                "observed_at": captured_at,
                "bids": [{"price": "0.19", "size": "1"}],
                "asks": [{"price": "0.20", "size": "1"}],
                "min_order_size": "5",
            }
        },
        forecast_confidence=90,
        day_status=active_day(),
    )
    assert rows[0]["status"] == "NO BET"
    assert not rows[0]["fully_fillable"]
    assert "cannot fill" in rows[0]["blockers_json"]
