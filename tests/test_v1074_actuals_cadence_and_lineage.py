from datetime import date, datetime, timezone

import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from weatherman.analytics import DayStatus
from weatherman.db import Base, DailyActual, ForecastSnapshot
from weatherman.service import (
    _signal_timing,
    _store_actual_rows,
    provisional_metar_actuals,
)
from weatherman.shadow import evaluate_shadow_markets


def _active_day() -> DayStatus:
    return DayStatus(
        phase="heating",
        label="Heating window active",
        is_locked=False,
        minimum_bucket=30,
        maximum_bucket=None,
        remaining_heating_c=2.0,
        explanation="The day is still heating.",
    )


def test_final_station_actual_cannot_be_downgraded_by_rolling_provisional() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    target = date(2026, 8, 10)
    with factory() as session:
        session.add(
            DailyActual(
                airport="EDDM",
                target_date=target,
                max_temp_c=34.0,
                source="stored-metar-station",
            )
        )
        session.flush()
        stored = _store_actual_rows(
            session,
            "EDDM",
            [{"target_date": target, "max_temp_c": 28.0}],
            source="metar-provisional",
            label="regression",
        )
        session.commit()
        actual = session.scalar(select(DailyActual))
    assert stored == 0
    assert actual is not None
    assert actual.max_temp_c == 34.0
    assert actual.source == "stored-metar-station"


def test_provisional_actual_is_monotone_when_peak_leaves_provider_window() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    target = date(2026, 8, 10)
    with factory() as session:
        session.add(
            DailyActual(
                airport="EHAM",
                target_date=target,
                max_temp_c=23.0,
                source="metar-provisional",
            )
        )
        session.flush()
        stored = _store_actual_rows(
            session,
            "EHAM",
            [{"target_date": target, "max_temp_c": 21.0}],
            source="metar-provisional",
            label="rolling-window regression",
        )
        session.commit()
        actual = session.scalar(select(DailyActual))
    assert stored == 0
    assert actual is not None
    assert actual.max_temp_c == 23.0


def test_provisional_derivation_ignores_an_older_truncated_day() -> None:
    as_of = datetime(2026, 8, 12, 20, tzinfo=timezone.utc)
    rows = []
    for day, peak in ((10, 28.0), (11, 32.0)):
        for hour, offset in zip((8, 10, 12, 14, 16, 18, 20, 22), range(8)):
            rows.append(
                {
                    "observed_at": datetime(
                        2026,
                        8,
                        day,
                        hour,
                        tzinfo=timezone.utc,
                    ),
                    "temp_c": peak - abs(3 - offset),
                }
            )
    actuals = provisional_metar_actuals(
        rows,
        {
            "timezone": "Europe/Berlin",
            "critical_window_local": ["12:00", "17:30"],
        },
        as_of=as_of,
    )
    assert actuals == [
        {"target_date": date(2026, 8, 11), "max_temp_c": 32.0}
    ]


def test_shadow_journal_separates_raw_and_champion_probability() -> None:
    captured_at = datetime(2026, 8, 12, 11, tzinfo=timezone.utc)
    markets = pd.DataFrame(
        [
            {
                "event_slug": "madrid-temperature",
                "market_id": "m38",
                "token_id": "yes38",
                "bucket_label": "38°C",
                "bucket_low_c": 38,
                "bucket_high_c": 38,
                "yes_price": 0.30,
                "closed": False,
            }
        ]
    )
    rows = evaluate_shadow_markets(
        airport="LEMD",
        target=date(2026, 8, 12),
        captured_at=captured_at,
        timing="D0 live",
        probabilities={38: 0.40, 39: 0.60},
        raw_probabilities={38: 0.55, 39: 0.45},
        markets=markets,
        books={
            "yes38": {
                "observed_at": captured_at,
                "asks": [{"price": "0.30", "size": "100"}],
                "bids": [{"price": "0.29", "size": "100"}],
                "min_order_size": "5",
            }
        },
        forecast_confidence=80,
        day_status=_active_day(),
    )
    assert len(rows) == 1
    assert rows[0]["raw_probability"] == 0.55
    assert rows[0]["fair_probability"] == 0.40
    assert rows[0]["forecast_snapshot_at"] == captured_at
    assert "Champion probability 40.0%" in rows[0]["reasons_json"]
    assert "Raw model probability" not in rows[0]["reasons_json"]


def test_market_information_sets_distinguish_d2_d1_morning_and_live() -> None:
    target = date(2026, 8, 12)
    assert _signal_timing(
        datetime(2026, 8, 10, 8, tzinfo=timezone.utc),
        target,
        "Europe/Madrid",
    ) == "D-2 or earlier"
    assert _signal_timing(
        datetime(2026, 8, 11, 8, tzinfo=timezone.utc),
        target,
        "Europe/Madrid",
    ) == "D-1"
    assert _signal_timing(
        datetime(2026, 8, 12, 7, tzinfo=timezone.utc),
        target,
        "Europe/Madrid",
    ) == "D0 morning"
    assert _signal_timing(
        datetime(2026, 8, 12, 13, tzinfo=timezone.utc),
        target,
        "Europe/Madrid",
    ) == "D0 live"


def test_forecast_snapshot_schema_journals_taf_bucket_flips() -> None:
    names = {column.name for column in ForecastSnapshot.__table__.columns}
    assert {
        "pre_taf_modal_bucket_c",
        "champion_modal_bucket_c",
        "taf_modal_bucket_flip",
    } <= names
