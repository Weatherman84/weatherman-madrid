from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from weatherman.db import Base, DailyActual, MarketSnapshot
from weatherman.market_replay_export import (
    CHECKPOINT_LABELS,
    build_market_replay_export,
    checkpoint_schedule,
)


def _market(target, captured_at, market_id, bucket, price, *, price_kind="live"):
    return MarketSnapshot(
        airport="LEMD",
        target_date=target,
        event_slug=f"madrid-{target.isoformat()}",
        market_id=market_id,
        market_slug=f"madrid-{target.isoformat()}-{bucket}",
        bucket_label=f"{bucket}°C",
        bucket_low_c=float(bucket),
        bucket_high_c=float(bucket),
        yes_price=price,
        best_bid=price - 0.01 if price_kind == "live" else None,
        best_ask=price + 0.01 if price_kind == "live" else None,
        spread=0.02 if price_kind == "live" else None,
        volume=100.0,
        liquidity=50.0 if price_kind == "live" else None,
        closed=False,
        yes_won=None,
        resolution_source="LEMD METAR",
        price_kind=price_kind,
        captured_at=captured_at,
    )


def test_checkpoint_schedule_uses_madrid_local_times():
    schedule = dict(checkpoint_schedule(date(2026, 9, 13)))
    assert tuple(schedule) == CHECKPOINT_LABELS
    assert schedule["D-1 Evening @20:00"] == datetime(
        2026, 9, 12, 18, tzinfo=timezone.utc
    )
    assert schedule["D0 Morning @09:00"] == datetime(
        2026, 9, 13, 7, tzinfo=timezone.utc
    )
    assert schedule["First Live @12:00"] == datetime(
        2026, 9, 13, 10, tzinfo=timezone.utc
    )
    assert schedule["Late Live @16:00"] == datetime(
        2026, 9, 13, 14, tzinfo=timezone.utc
    )


def test_export_selects_latest_causal_snapshot_and_marks_unavailable():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 9, 13)
    unavailable_target = date(2026, 9, 12)
    schedule = dict(checkpoint_schedule(target))
    d1 = schedule["D-1 Evening @20:00"]
    d0 = schedule["D0 Morning @09:00"]
    with Session(engine) as session:
        session.add_all(
            [
                DailyActual(
                    airport="LEMD",
                    target_date=unavailable_target,
                    max_temp_c=33,
                    source="stored-metar-station",
                ),
                DailyActual(
                    airport="LEMD",
                    target_date=target,
                    max_temp_c=33,
                    source="stored-metar-station",
                ),
                # All snapshots for Sep 12 are after its last checkpoint.
                _market(
                    unavailable_target,
                    dict(checkpoint_schedule(unavailable_target))["Late Live @16:00"]
                    + timedelta(minutes=1),
                    "future-only",
                    33,
                    0.4,
                ),
                _market(target, d1, "d1-33", 33, 0.45, price_kind="historical trade-price sample"),
                _market(target, d1, "d1-34", 34, 0.35, price_kind="historical trade-price sample"),
                _market(target, d0 - timedelta(minutes=15), "d0-prior", 33, 0.50),
                # This is nearer in absolute time but is forbidden look-ahead.
                _market(target, d0 + timedelta(minutes=1), "d0-future", 33, 0.99),
            ]
        )
        session.commit()
        with engine.connect() as connection:
            payload = build_market_replay_export(
                connection,
                days=2,
                generated_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
            )

    assert payload["research_only"] is True
    assert payload["automatic_promotion"] is False
    assert payload["protected_forecast_baseline"] == "v10.7.10"
    assert payload["checkpoint_count"] == 8

    by_key = {
        (row["target_date"], row["checkpoint_label"]): row
        for row in payload["checkpoints"]
    }
    unavailable = by_key[("2026-09-12", "Late Live @16:00")]
    assert unavailable == {
        "target_date": "2026-09-12",
        "checkpoint_label": "Late Live @16:00",
        "checkpoint_at": "2026-09-12T14:00:00+00:00",
        "status": "unavailable",
        "provenance": "unavailable_no_causal_market_snapshot",
        "market_captured_at": None,
        "market_snapshot_age_minutes": None,
        "markets": [],
    }

    exact = by_key[("2026-09-13", "D-1 Evening @20:00")]
    assert exact["provenance"] == "exact_checkpoint_market_snapshot"
    assert exact["market_snapshot_age_minutes"] == 0.0
    assert [row["bucket_c"] for row in exact["markets"]] == [33, 34]
    assert exact["markets"][0]["best_bid"] is None

    causal = by_key[("2026-09-13", "D0 Morning @09:00")]
    assert causal["provenance"] == "nearest_available_before_checkpoint"
    assert causal["market_snapshot_age_minutes"] == 15.0
    assert causal["markets"][0]["yes_price"] == 0.5
    assert causal["markets"][0]["yes_price"] != 0.99
    assert set(causal["markets"][0]) == {
        "bucket_label",
        "bucket_c",
        "yes_price",
        "best_bid",
        "best_ask",
        "spread",
        "volume",
        "liquidity",
        "closed",
        "yes_won",
        "resolution_source",
        "price_kind",
    }


def test_market_export_workflow_is_manual_read_only_and_compact():
    source = open(".github/workflows/export-market-replay.yml", encoding="utf-8").read()
    script = open("scripts/export_market_replay.py", encoding="utf-8").read()
    assert "workflow_dispatch" in source
    assert "actions/upload-artifact@v4" in source
    assert "market-replay-export.json" in source
    assert "secrets.DATABASE_URL" in source
    assert "contents: read" in source
    assert "SET TRANSACTION READ ONLY" in script
    assert "transaction.rollback()" in script
    assert "init_db" not in script
    assert "REPLAY_DATABASE_URL" not in source
