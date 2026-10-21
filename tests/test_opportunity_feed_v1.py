from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from weatherman.opportunity_feed import (
    build_forecast_snapshot,
    build_live_feed,
    completed_peak_rows,
    load_calibration_seed,
    load_peak_seed,
    normal_bucket_probabilities,
    normalize_markets,
    validate_live_feed,
)
from weatherman.opportunity_scanner import load_airport_config


def test_frozen_seeds_cover_six_airports_without_production_dependency() -> None:
    airports = set(load_airport_config()["airports"])
    calibration = load_calibration_seed()
    peaks = load_peak_seed()
    assert set(calibration["airports"]) == airports
    assert set(peaks["airports"]) == airports
    assert all(len(rows) == 60 for rows in peaks["airports"].values())
    assert calibration["method"].endswith("no Production DB")


def test_normal_buckets_are_rankable_and_normalized() -> None:
    probabilities = normal_bucket_probabilities(24.2, 0.7)
    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert max(probabilities, key=probabilities.get) == 24


def test_completed_peaks_are_prior_day_first_touch_only() -> None:
    rows = []
    start = datetime(2026, 10, 12, 7, tzinfo=timezone.utc)
    for index in range(25):
        rows.append(
            {
                "observed_at": start + timedelta(minutes=30 * index),
                "temp_c": 20 + min(index, 14) // 3,
            }
        )
    result = completed_peak_rows(rows, zone_name="UTC", before=date(2026, 10, 13))
    assert len(result) == 1
    assert result[0]["actual_first_peak_minute"] == 13 * 60
    assert result[0]["eligible"] is True


def test_forecast_snapshot_uses_fixed_common_models_and_never_outcome_fields() -> None:
    config = load_airport_config()
    airport_config = config["airports"]["LEMD"]
    target = date(2026, 10, 13)
    as_of = datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc)
    model_rows = {}
    for offset, model in enumerate(config["common_models"]):
        model_rows[model] = [
            {
                "model": model,
                "valid_at": datetime(2026, 10, 13, hour, tzinfo=timezone.utc),
                "temp_c": 18 + offset * 0.2 + min(hour, 14) * 0.5,
                "cloud_cover": 10,
                "wind_kph": 8,
            }
            for hour in range(24)
        ]
    observations = [
        {
            "observed_at": as_of - timedelta(minutes=30 * (4 - index)),
            "temp_c": 22 + index * 0.4,
            "dewpoint_c": 10 - index * 0.4,
            "cloud_cover": 0,
            "wind_kph": 7,
            "raw": "LEMD CAVOK",
        }
        for index in range(5)
    ]
    snapshot = build_forecast_snapshot(
        airport="LEMD",
        airport_config=airport_config,
        target=target,
        as_of=as_of,
        model_rows=model_rows,
        observations=observations,
        calibration=load_calibration_seed()["airports"]["LEMD"],
        peak_history=load_peak_seed()["airports"]["LEMD"],
    )
    assert snapshot["models"] == sorted(config["common_models"])
    assert snapshot["forecast_method"].startswith("common_models_fixed_24h_lead")
    assert "actual_bucket" not in snapshot
    assert snapshot["research_only"] is True


def test_market_normalization_prefers_clob_bid_ask_and_preserves_boundary_buckets() -> None:
    rows = [
        {
            "target_date": date(2026, 10, 13),
            "bucket_low_c": 24.0,
            "bucket_high_c": 24.0,
            "token_id": "yes24",
            "event_slug": "event",
            "market_id": "m24",
            "captured_at": datetime(2026, 10, 13, 12, tzinfo=timezone.utc),
            "best_bid": 0.2,
            "best_ask": 0.8,
            "yes_price": 0.5,
            "closed": False,
        },
        {
            "target_date": date(2026, 10, 13),
            "bucket_low_c": None,
            "bucket_high_c": 20.0,
            "token_id": "range",
        },
    ]
    books = {
        "yes24": {
            "observed_at": datetime(2026, 10, 13, 12, tzinfo=timezone.utc),
            "bids": [{"price": "0.42"}],
            "asks": [{"price": "0.47"}],
        }
    }
    result = normalize_markets("LEMD", rows, books)
    assert len(result) == 2
    assert result[0]["best_bid"] == 0.42
    assert result[0]["best_ask"] == 0.47
    assert result[0]["quote_source"] == "polymarket_clob"
    assert result[1]["bucket"] is None
    assert result[1]["bucket_low_c"] is None
    assert result[1]["bucket_high_c"] == 20


def test_full_feed_builder_emits_all_airports_and_safety_flags() -> None:
    as_of = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)

    def forecast_fetcher(airport, model, target):
        return [
            {
                "model": model,
                "valid_at": datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
                + timedelta(hours=hour),
                "temp_c": 15 + min(hour, 15) * 0.5,
                "cloud_cover": 20,
                "wind_kph": 10,
            }
            for hour in range(24)
        ]

    def metar_fetcher(airport, hours=48):
        return [
            {
                "observed_at": as_of - timedelta(minutes=30 * index),
                "temp_c": 21 - index * 0.2,
                "dewpoint_c": 10,
                "cloud_cover": 10,
                "wind_kph": 8,
                "raw": f"{airport} CAVOK",
            }
            for index in range(8, -1, -1)
        ]

    feed = build_live_feed(
        as_of=as_of,
        forecast_fetcher=forecast_fetcher,
        metar_fetcher=metar_fetcher,
        market_fetcher=lambda airport, target: [],
        book_fetcher=lambda tokens: {},
    )
    validate_live_feed(feed)
    assert len(feed["forecasts"]) == 6
    assert feed["contains_outcomes"] is False
    assert feed["writes_production_database"] is False


def test_feed_validation_rejects_outcome_leakage() -> None:
    payload = {
        "schema_version": "opportunity_scanner_feed_v1",
        "research_only": True,
        "automatic_promotion": False,
        "automatic_execution": False,
        "writes_production_database": False,
        "contains_outcomes": False,
        "automatic_backfill": False,
        "common_models": ["ecmwf_ifs025", "gfs_global", "icon_global"],
        "forecasts": [
            {"airport": airport, **({"actual_bucket": 20} if airport == "LEMD" else {})}
            for airport in load_airport_config()["airports"]
        ],
    }
    with pytest.raises(ValueError, match="Outcome leakage"):
        validate_live_feed(payload)
