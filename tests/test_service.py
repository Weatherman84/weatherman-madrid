from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from weatherman import service
from weatherman.db import (
    Base,
    BasketSnapshot,
    CollectionCoverage,
    DailyActual,
    Forecast,
    ForecastVariantSnapshot,
    Observation,
    ProviderCall,
    RegimeMemorySnapshot,
    ShadowEvaluation,
    SignalSnapshot,
    StrategySnapshot,
)
from weatherman.service import (
    _record_forecast_variants,
    _record_regime_memory_snapshot,
    _record_shadow_evaluations,
    _record_signal_snapshots,
    _record_strategy_snapshots,
    _meteoblue_poll_policy,
    _source_refresh_due,
    _restore_stored_station_actuals,
    _store_reanalysis_actuals,
    _upsert_batch,
    in_critical_window,
    in_forecast_refresh_window,
    in_final_metar_collection_window,
    provisional_metar_actuals,
)


def test_reanalysis_actual_never_overwrites_station_truth():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    target = date(2026, 7, 29)
    with session_factory() as session:
        session.add(
            DailyActual(
                airport="LEMD",
                target_date=target,
                max_temp_c=35.0,
                source="stored-metar-station",
            )
        )
        session.flush()
        stored = _store_reanalysis_actuals(
            session,
            "LEMD",
            [{"target_date": target, "max_temp_c": 33.8}],
        )
        session.commit()
        actual = session.scalar(select(DailyActual))
        assert stored == 0
        assert actual is not None
        assert actual.max_temp_c == 35.0
        assert actual.source == "stored-metar-station"


def test_complete_stored_metar_day_restores_station_actual():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    target = date(2026, 7, 29)
    with session_factory() as session:
        session.add(
            DailyActual(
                airport="LEMD",
                target_date=target,
                max_temp_c=33.8,
                source="open-meteo-archive",
            )
        )
        session.add_all(
            [
                Observation(
                    airport="LEMD",
                    observed_at=datetime(2026, 7, 29, hour, tzinfo=timezone.utc),
                    temp_c=temperature,
                )
                for hour, temperature in [
                    (7, 22.0),
                    (8, 24.0),
                    (9, 26.0),
                    (10, 28.0),
                    (12, 32.0),
                    (15, 35.0),
                    (17, 33.0),
                    (19, 29.0),
                ]
            ]
        )
        restored = _restore_stored_station_actuals(
            session,
            "LEMD",
            {
                "timezone": "Europe/Madrid",
                "critical_window_local": ["12:00", "17:30"],
            },
            as_of=datetime(2026, 7, 30, 8, tzinfo=timezone.utc),
        )
        session.commit()
        actual = session.scalar(select(DailyActual))
        assert restored == 1
        assert actual is not None
        assert actual.max_temp_c == 35.0
        assert actual.source == "stored-metar-station"


def test_failed_batch_does_not_poison_following_database_work():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    with session_factory() as session:
        bad_rows = [
            {"model": "valid", "temperature": 20.0},
            {"model": "invalid", "temperature": None},
        ]
        stored = _upsert_batch(
            session,
            Forecast,
            bad_rows,
            lambda item: {
                "airport": "LEMD",
                "model": item["model"],
                "run_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
                "target_date": date(2026, 7, 20),
            },
            lambda item: {
                "max_temp_c": item["temperature"],
                "source": "test",
                "horizon": "Live",
            },
            "deliberately invalid batch",
        )
        assert stored == 0

        stored = _upsert_batch(
            session,
            Forecast,
            [{"model": "next", "temperature": 21.0}],
            lambda item: {
                "airport": "LEMD",
                "model": item["model"],
                "run_at": datetime(2026, 7, 20, 1, tzinfo=timezone.utc),
                "target_date": date(2026, 7, 20),
            },
            lambda item: {
                "max_temp_c": item["temperature"],
                "source": "test",
                "horizon": "Live",
            },
            "valid batch",
        )
        session.commit()
        assert stored == 1
        assert session.scalar(select(func.count()).select_from(Forecast)) == 1


def test_postgresql_upsert_batch_uses_bounded_bulk_statements():
    class FakePostgresSession:
        def __init__(self):
            self.statements = []
            self.flushes = 0

        def begin_nested(self):
            return nullcontext()

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, statement):
            self.statements.append(statement)

        def flush(self):
            self.flushes += 1

    session = FakePostgresSession()
    run_at = datetime(2026, 8, 27, tzinfo=timezone.utc)
    rows = [
        {
            "model": "test-model",
            "valid_at": run_at + timedelta(hours=index),
            "temperature": 20.0 + index / 100,
        }
        for index in range(501)
    ]

    stored = _upsert_batch(
        session,
        service.HourlyForecast,
        rows,
        lambda item: {
            "airport": "LEMD",
            "model": item["model"],
            "run_at": run_at,
            "valid_at": item["valid_at"],
        },
        lambda item: {
            "temp_c": item["temperature"],
            "dewpoint_c": None,
            "cloud_cover": None,
            "wind_kph": None,
            "wind_direction": None,
            "radiation_wm2": None,
            "temp_850hpa_c": None,
        },
        "postgres bulk upsert",
    )

    assert stored == 501
    assert len(session.statements) == 2
    assert session.flushes == 1
    compiled = str(
        session.statements[0].compile(dialect=postgresql.dialect())
    )
    assert "ON CONFLICT (airport, model, run_at, valid_at) DO UPDATE" in compiled


def test_live_provider_refresh_age_is_checked_against_real_fetch_time():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime(2026, 7, 30, 8, tzinfo=timezone.utc)
    target = date(2026, 7, 30)
    with session_factory() as session:
        session.add(
            Forecast(
                airport="EHAM",
                model="meteoblue",
                run_at=now - timedelta(minutes=59),
                fetched_at=now - timedelta(minutes=59),
                target_date=target,
                max_temp_c=27,
                source="meteoblue",
                horizon="D0-morning",
            )
        )
        session.commit()
        assert not _source_refresh_due(
            session,
            airport_code="EHAM",
            source="meteoblue",
            target=target,
            as_of=now,
            maximum_age_minutes=60,
        )
        assert _source_refresh_due(
            session,
            airport_code="EHAM",
            source="meteoblue",
            target=target,
            as_of=now + timedelta(minutes=2),
            maximum_age_minutes=60,
        )


def madrid_checkpoint_airport() -> dict[str, object]:
    return {
        "timezone": "Europe/Madrid",
        "decision_checkpoints_local": [
            {
                "label": "D-1 Evening @20:00",
                "time": "20:00",
                "meteoblue_lead_minutes": 15,
                "meteoblue_grace_minutes": 30,
            },
            {
                "label": "D0 Morning @09:00",
                "time": "09:00",
                "meteoblue_lead_minutes": 60,
                "meteoblue_grace_minutes": 30,
            },
            {
                "label": "First Live @12:00",
                "time": "12:00",
                "meteoblue_lead_minutes": 15,
                "meteoblue_grace_minutes": 30,
            },
            {
                "label": "Late Live @16:00",
                "time": "16:00",
                "meteoblue_lead_minutes": 15,
                "meteoblue_grace_minutes": 30,
            },
        ],
    }


def meteoblue_settings() -> SimpleNamespace:
    return SimpleNamespace(
        meteoblue_api_key="configured",
        meteoblue_url_template="configured",
        meteoblue_checkpoint_lead_minutes=15,
        meteoblue_checkpoint_grace_minutes=30,
        meteoblue_daily_call_limit=4,
        meteoblue_rate_limit_cooldown_hours=24,
    )


def test_meteoblue_checkpoint_attempt_is_reserved_once(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime(2026, 8, 20, 10, tzinfo=timezone.utc)  # 12:00 Madrid
    monkeypatch.setattr(service, "settings", meteoblue_settings())
    with session_factory() as session:
        due, status = _meteoblue_poll_policy(
            session,
            airport_code="LEMD",
            airport=madrid_checkpoint_airport(),
            as_of=now,
        )
        assert due
        assert status == "requested:First Live @12:00"
        assert session.scalar(select(func.count(ProviderCall.id))) == 1
        due, status = _meteoblue_poll_policy(
            session,
            airport_code="LEMD",
            airport=madrid_checkpoint_airport(),
            as_of=now + timedelta(minutes=5),
        )
    assert not due
    assert status == "checkpoint-reused:First Live @12:00"


def test_meteoblue_rate_limit_enters_persistent_cooldown(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime(2026, 8, 20, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(service, "settings", meteoblue_settings())
    with session_factory() as session:
        session.add(
            CollectionCoverage(
                run_id="rate-limited",
                airport="EHAM",
                data_type="meteoblue",
                status="source_or_parser_failed",
                scheduled_at=now - timedelta(hours=2),
                rows_read=0,
                rows_written=0,
                attempts=1,
                reason="HTTPStatusError: 429 quota exceeded",
            )
        )
        session.commit()
        due, status = _meteoblue_poll_policy(
            session,
            airport_code="LEMD",
            airport=madrid_checkpoint_airport(),
            as_of=now,
        )
    assert not due
    assert status == "rate-limited-cooldown"


def test_meteoblue_stays_outside_fixed_checkpoint_windows(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    monkeypatch.setattr(service, "settings", meteoblue_settings())
    with session_factory() as session:
        due, status = _meteoblue_poll_policy(
            session,
            airport_code="LEMD",
            airport=madrid_checkpoint_airport(),
            as_of=datetime(2026, 8, 20, 5, tzinfo=timezone.utc),
        )
    assert not due
    assert status == "outside-checkpoint-window"


def test_forecast_refresh_begins_at_six_airport_local_time():
    airport = {
        "timezone": "Europe/Amsterdam",
        "critical_window_local": ["11:30", "17:30"],
    }
    assert not in_forecast_refresh_window(
        airport,
        datetime(2026, 7, 30, 3, 59, tzinfo=timezone.utc),
    )
    assert in_forecast_refresh_window(
        airport,
        datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc),
    )
    assert not in_forecast_refresh_window(
        airport,
        datetime(2026, 7, 30, 15, 31, tzinfo=timezone.utc),
    )


def test_completed_metar_day_becomes_next_day_provisional_actual():
    as_of = datetime(2026, 7, 30, 8, tzinfo=timezone.utc)
    rows = [
        {
            "observed_at": datetime(2026, 7, 29, hour, tzinfo=timezone.utc),
            "temp_c": temperature,
        }
        for hour, temperature in [
            (7, 19),
            (9, 23),
            (11, 28),
            (13, 32),
            (14, 34),
            (15, 33),
            (17, 30),
            (20, 25),
        ]
    ]
    provisional = provisional_metar_actuals(
        rows,
        {
            "timezone": "Europe/Berlin",
            "critical_window_local": ["12:00", "17:30"],
        },
        as_of=as_of,
    )
    assert provisional == [
        {"target_date": date(2026, 7, 29), "max_temp_c": 34.0}
    ]


def test_final_metar_window_continues_until_after_21_local():
    airport = {
        "timezone": "Europe/Berlin",
        "critical_window_local": ["12:00", "17:30"],
        "final_metar_collection_end_local": "21:35",
    }
    assert in_final_metar_collection_window(
        airport,
        datetime(2026, 7, 30, 15, 31, tzinfo=timezone.utc),
    )
    assert in_final_metar_collection_window(
        airport,
        datetime(2026, 7, 30, 19, 35, tzinfo=timezone.utc),
    )
    assert not in_final_metar_collection_window(
        airport,
        datetime(2026, 7, 30, 19, 36, tzinfo=timezone.utc),
    )


def test_evening_metar_collection_updates_current_day_actual():
    as_of = datetime(2026, 7, 30, 19, 30, tzinfo=timezone.utc)
    rows = [
        {
            "observed_at": datetime(2026, 7, 30, hour, tzinfo=timezone.utc),
            "temp_c": temperature,
        }
        for hour, temperature in [
            (6, 18),
            (8, 22),
            (10, 27),
            (12, 30),
            (14, 31),
            (16, 29),
            (18, 25),
            (19, 23),
        ]
    ]
    actuals = provisional_metar_actuals(
        rows,
        {
            "timezone": "Europe/Berlin",
            "critical_window_local": ["12:00", "17:30"],
        },
        as_of=as_of,
        include_current_day=True,
    )
    assert actuals == [
        {"target_date": date(2026, 7, 30), "max_temp_c": 31.0}
    ]


def test_collection_journals_model_probability_and_real_ask():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    captured_at = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
    target = date(2026, 7, 21)
    with session_factory() as session:
        session.add(
            Forecast(
                airport="LEMD",
                model="ECMWF",
                run_at=captured_at - timedelta(minutes=10),
                target_date=target,
                max_temp_c=35,
                source="open-meteo",
                horizon="Live",
            )
        )
        session.flush()
        stored = _record_signal_snapshots(
            session,
            "LEMD",
            {"timezone": "Europe/Madrid"},
            [
                {
                    "target_date": target,
                    "event_slug": "test-event",
                    "market_id": "market-35",
                    "bucket_label": "35°C",
                    "bucket_low_c": 35,
                    "bucket_high_c": 35,
                    "yes_price": 0.18,
                    "best_ask": 0.20,
                    "closed": False,
                    "yes_won": None,
                    "captured_at": captured_at,
                }
            ],
        )
        session.commit()
        signal = session.scalar(select(SignalSnapshot))
        assert stored == 1
        assert signal is not None
        assert signal.buy_price == 0.20
        assert signal.model_probability > signal.buy_price
        assert signal.signal == "Market-model conflict"
        assert signal.timing == "D-1"


def test_consensus_strategy_journal_chooses_model_mode_without_edge_filter():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    captured_at = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
    market_rows = [
        {
            "target_date": date(2026, 7, 21),
            "market_id": "market-35",
            "bucket_label": "35°C",
            "bucket_low_c": 35,
            "bucket_high_c": 35,
            "yes_price": 0.70,
            "best_ask": 0.72,
            "closed": False,
            "captured_at": captured_at,
        }
    ]
    nowcast = SimpleNamespace(
        stage_probabilities={"Raw model mean": {34: 0.2, 35: 0.8}},
        observed_max=None,
        day_status=SimpleNamespace(phase="forecast"),
    )
    with session_factory() as session:
        stored = _record_strategy_snapshots(
            session,
            "LEMD",
            {"timezone": "Europe/Madrid"},
            market_rows,
            nowcast,
        )
        session.commit()
        strategy = session.scalar(select(StrategySnapshot))
        assert stored == 1
        assert strategy.strategy == "Raw model mean"
        assert strategy.model_bucket_c == 35
        assert strategy.buy_price == 0.72


def test_collection_journals_champion_and_same_snapshot_challenger():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    captured_at = datetime(2026, 7, 30, 10, tzinfo=timezone.utc)
    nowcast = SimpleNamespace(
        final_forecast_mean=38.4,
        final_forecast_spread=1.1,
        probabilities={37: 0.2, 38: 0.5, 39: 0.3},
        forecast_confidence=72,
        day_status=SimpleNamespace(phase="heating"),
        challenger_variants={
            "Without Persistent Hot": {
                "factor": "persistent_hot",
                "forecast_mean_c": 37.8,
                "spread_c": 0.8,
                "probabilities": {37: 0.4, 38: 0.5, 39: 0.1},
                "forecast_confidence": 80,
            }
        },
    )
    with session_factory() as session:
        stored = _record_forecast_variants(
            session,
            "LEMD",
            {"timezone": "Europe/Madrid"},
            date(2026, 7, 30),
            captured_at,
            nowcast,
        )
        session.commit()
        rows = list(
            session.scalars(
                select(ForecastVariantSnapshot).order_by(
                    ForecastVariantSnapshot.variant
                )
            )
        )
        assert stored == 2
        assert {row.variant for row in rows} == {
            "Champion",
            "Without Persistent Hot",
        }
        assert len({row.captured_at for row in rows}) == 1


def test_collection_journals_explainable_regime_memory_snapshot():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    captured_at = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
    memory = SimpleNamespace(
        status="WATCH",
        label="Learned Analog Pattern",
        confidence=67,
        analog_count=3,
        best_similarity=0.82,
        center_adjustment_c=0.27,
        suggested_forecast_c=39.27,
        suggested_spread_c=1.0,
        shadow_only=True,
        applied_to_champion=False,
        promotion=SimpleNamespace(
            status="SHADOW",
            eligible=False,
            oos_days=0,
        ),
        regimes=(
            SimpleNamespace(
                name="Learned Analog Pattern",
                status="WATCH",
                confidence=67,
                source="learned",
                champion_effect="Challenger only",
                supports=("three similar days",),
                contradictions=("OOS gate not met",),
                explanation="Stored without changing the Champion.",
            ),
        ),
        analogs=(
            SimpleNamespace(
                target_date="2026-07-31",
                captured_at="2026-07-31T10:00:00+00:00",
                similarity=0.82,
                forecast_c=38.0,
                actual_c=39.0,
                residual_c=1.0,
                matched_on=("heating rate", "wind direction"),
            ),
        ),
        pro_signals=("three similar days",),
        contra_signals=("OOS gate not met",),
        explanation="The learned pattern remains Challenger-only.",
        feature_signature={"heating_rate_surprise_cph": 0.4},
    )
    with session_factory() as session:
        stored = _record_regime_memory_snapshot(
            session,
            "LEMD",
            {"timezone": "Europe/Madrid"},
            date(2026, 8, 1),
            captured_at,
            SimpleNamespace(regime_memory=memory),
        )
        session.commit()
        row = session.scalar(select(RegimeMemorySnapshot))
        assert stored == 1
        assert row is not None
        assert row.shadow_only
        assert not row.applied_to_champion
        assert row.analog_count == 3
        assert "heating rate" in row.analogs_json


def test_shadow_collection_stays_research_only_without_an_actionable_basket():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    captured_at = datetime(2026, 7, 30, 10, tzinfo=timezone.utc)
    fair = {25: 0.15, 26: 0.20, 27: 0.35, 28: 0.20, 29: 0.10}
    prices = {25: 0.05, 26: 0.10, 27: 0.40, 28: 0.10, 29: 0.15}
    market_rows = [
        {
            "target_date": date(2026, 7, 30),
            "event_slug": "ankara-temperature",
            "market_id": f"m{bucket}",
            "token_id": f"t{bucket}",
            "bucket_label": f"{bucket}°C",
            "bucket_low_c": bucket,
            "bucket_high_c": bucket,
            "yes_price": price,
            "best_bid": max(0.01, price - 0.01),
            "best_ask": price,
            "closed": False,
            "yes_won": False,
            "captured_at": captured_at,
        }
        for bucket, price in prices.items()
    ]
    books = {
        f"t{bucket}": {
            "observed_at": captured_at,
            "hash": f"book-{bucket}",
            "bids": [{"price": str(max(0.01, price - 0.01)), "size": "1000"}],
            "asks": [{"price": str(price), "size": "1000"}],
            "min_order_size": "5",
        }
        for bucket, price in prices.items()
    }
    nowcast = SimpleNamespace(
        probabilities=fair,
        stage_probabilities={"Raw model mean": fair},
        forecast_confidence=85,
        day_status=SimpleNamespace(is_locked=False, phase="heating"),
        metar_pending=False,
        forecast_data_stale=False,
    )
    with session_factory() as session:
        shadow_count, basket_count = _record_shadow_evaluations(
            session,
            "LTAC",
            {"timezone": "Europe/Istanbul"},
            market_rows,
            books,
            nowcast,
        )
        session.commit()
        basket = session.scalar(select(BasketSnapshot))
        blocked_rows = session.scalars(
            select(ShadowEvaluation).where(
                ShadowEvaluation.market_id.in_(["m25", "m26", "m28"])
            )
        ).all()
        assert shadow_count == 5
        assert basket_count == 0
        assert basket is None
        assert {row.status for row in blocked_rows} == {"NO BET"}
        assert all(row.blockers_json != "[]" for row in blocked_rows)


def test_airport_specific_critical_window_uses_local_time():
    airport = {
        "timezone": "Europe/Istanbul",
        "critical_window_local": ["11:30", "16:30"],
    }
    assert in_critical_window(
        airport,
        datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc),
    )
    assert not in_critical_window(
        airport,
        datetime(2026, 7, 26, 6, 0, tzinfo=timezone.utc),
    )
