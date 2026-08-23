from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import weatherman.collector as collector
import weatherman.research_diagnostics as diagnostics
import weatherman.service as service
from weatherman.db import (
    Base,
    CollectionCoverage,
    CollectionRun,
    DailyActual,
    Forecast,
    ShadowEvaluation,
)
from weatherman.regime_memory import evaluate_promotion_gate


def test_scheduler_lineage_separates_expected_event_queue_and_python_start(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WEATHERMAN_EVENT_CREATED_AT", "2026-08-15T10:24:00Z")
    monkeypatch.setenv("WEATHERMAN_RUN_STARTED_AT", "2026-08-15T10:25:30Z")
    started = datetime(2026, 8, 15, 10, 26, tzinfo=timezone.utc)

    expected, created, queue_started = collector._scheduler_times(
        started,
        trigger="schedule",
    )

    assert expected == datetime(2026, 8, 15, 10, 22, tzinfo=timezone.utc)
    assert created == datetime(2026, 8, 15, 10, 24, tzinfo=timezone.utc)
    assert queue_started == datetime(2026, 8, 15, 10, 25, 30, tzinfo=timezone.utc)


def test_collector_failure_metrics_remain_consistent_after_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'collector.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    scheduled = datetime(2026, 8, 15, 10, 17, tzinfo=timezone.utc)
    event_created = scheduled + timedelta(minutes=2)
    queue_started = event_created + timedelta(minutes=1)
    started = queue_started + timedelta(seconds=30)
    monkeypatch.setattr(collector, "Session", factory)

    collector._start_run(
        "failed-run",
        scheduled_at=scheduled,
        event_created_at=event_created,
        queue_started_at=queue_started,
        started_at=started,
        trigger="schedule",
        airport_codes=["EDDM"],
    )
    collector._finish_run(
        "failed-run",
        status="failed",
        results={
            "aviation_provider_metrics": {"metar/EDDM": {"duration_seconds": 1.2}},
            "live_decisions": {
                "provider_timings_seconds": {"EDDM": {"open-meteo": 2.3}},
                "provider_status": {"EDDM": {"open-meteo": "failed"}},
                "airport_timings_seconds": {"EDDM": 2.5},
            },
        },
        coverage=[
            {
                "airport": "EDDM",
                "data_type": "open-meteo",
                "status": "source_or_parser_failed",
                "rows_read": 0,
                "rows_written": 0,
                "source_age_minutes": None,
                "duration_seconds": 2.3,
                "attempts": 1,
                "metrics": {"tasks": 2},
                "reason": "timeout",
            }
        ],
        error_reason="provider timeout",
    )
    collector._start_run(
        "resumed-run",
        scheduled_at=scheduled + timedelta(minutes=10),
        event_created_at=event_created + timedelta(minutes=10),
        queue_started_at=queue_started + timedelta(minutes=10),
        started_at=started + timedelta(minutes=10),
        trigger="schedule",
        airport_codes=["EDDM"],
    )

    with factory() as session:
        failed = session.scalar(
            select(CollectionRun).where(CollectionRun.run_id == "failed-run")
        )
        coverage = session.scalar(
            select(CollectionCoverage).where(
                CollectionCoverage.run_id == "failed-run"
            )
        )
    assert failed is not None
    assert failed.overall_status == "failed"
    assert failed.trigger_delay_seconds == 120
    assert failed.queue_delay_seconds == 60
    assert failed.execution_seconds is not None
    assert failed.persistence_status == "persisted"
    assert json.loads(failed.airport_metrics_json) == {"EDDM": 2.5}
    assert coverage is not None
    assert coverage.status == "source_or_parser_failed"
    assert coverage.duration_seconds == 2.3
    assert coverage.attempts == 1
    assert json.loads(coverage.metrics_json) == {"tasks": 2}


def test_checkpoint_freshness_uses_conservative_oldest_model_age() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    target = date(2026, 8, 15)
    cutoff = datetime(2026, 8, 15, 8, tzinfo=timezone.utc)
    with factory() as session:
        session.add_all(
            [
                Forecast(
                    airport="EDDM",
                    model="fresh-model",
                    run_at=cutoff + timedelta(minutes=10),
                    target_date=target,
                    max_temp_c=30,
                    source="open-meteo",
                    available_at=cutoff - timedelta(minutes=15),
                    fetched_at=cutoff + timedelta(minutes=10),
                ),
                Forecast(
                    airport="EDDM",
                    model="old-model",
                    run_at=cutoff + timedelta(minutes=12),
                    target_date=target,
                    max_temp_c=31,
                    source="open-meteo",
                    available_at=cutoff - timedelta(minutes=120),
                    fetched_at=cutoff + timedelta(minutes=12),
                ),
            ]
        )
        session.flush()
        metadata = service._checkpoint_provenance(
            session,
            code="EDDM",
            target=target,
            checkpoint_at=cutoff,
            current_time=cutoff + timedelta(hours=2),
            label="D0 @10",
            expected_models=["fresh-model", "old-model", "missing-model"],
        )
    assert metadata["checkpoint_status"] == "reconstructed-causal"
    assert metadata["freshness_status"] == "stale"
    assert metadata["evidence_class"] == "partial"
    assert metadata["source_age_min_minutes"] == 15
    assert metadata["source_age_max_minutes"] == 120
    assert metadata["source_age_at_checkpoint_minutes"] == 120
    assert metadata["source_model_count"] == 2
    assert metadata["expected_model_count"] == 3
    assert round(float(metadata["source_coverage_ratio"]), 3) == 0.667


def test_checkpoint_without_causal_guidance_is_unavailable() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    cutoff = datetime(2026, 8, 15, 8, tzinfo=timezone.utc)
    with factory() as session:
        metadata = service._checkpoint_provenance(
            session,
            code="EDDM",
            target=date(2026, 8, 15),
            checkpoint_at=cutoff,
            current_time=cutoff,
            label="D0 @10",
            expected_models=["ecmwf_ifs025"],
        )
    assert metadata["checkpoint_status"] == "unavailable"
    assert metadata["freshness_status"] == "unavailable"
    assert metadata["evidence_class"] == "unavailable"


def test_due_provider_reads_are_parallel_but_database_write_is_serial(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    captured = datetime(2026, 8, 15, 8, tzinfo=timezone.utc)
    worker_names: set[str] = set()

    def daily(_airport, model, _days, **_kwargs):
        worker_names.add(threading.current_thread().name)
        time.sleep(0.03)
        return [
            {
                "model": model,
                "run_at": captured,
                "target_date": captured.date(),
                "max_temp_c": 30.0,
                "source": "open-meteo",
                "horizon": "D0-morning",
                "model_run_at": captured - timedelta(hours=6),
                "available_at": captured - timedelta(minutes=15),
                "fetched_at": captured,
                "provenance_status": "test",
            }
        ]

    def hourly(_airport, model, _days, **_kwargs):
        worker_names.add(threading.current_thread().name)
        time.sleep(0.03)
        return [
            {
                "model": model,
                "run_at": captured,
                "valid_at": captured,
                "temp_c": 25.0,
                "dewpoint_c": 10.0,
                "cloud_cover": 0.0,
                "wind_kph": 5.0,
                "wind_direction": 180.0,
                "radiation_wm2": 500.0,
                "temp_850hpa_c": 15.0,
            }
        ]

    monkeypatch.setattr(service, "open_meteo_forecast", daily)
    monkeypatch.setattr(service, "open_meteo_hourly", hourly)
    monkeypatch.setattr(service, "meteoblue_forecast", lambda *_args, **_kwargs: [])
    with factory() as session:
        result = service._store_current_provider_forecasts(
            session,
            airport_code="EDDM",
            airport={
                "timezone": "UTC",
                "models": ["model-a", "model-b"],
                "latitude": 0,
                "longitude": 0,
                "elevation_m": 0,
            },
            as_of=captured,
        )
        session.commit()
        stored = session.scalar(select(func.count(Forecast.id)))
    assert stored == 2
    assert int(result["forecasts"]) == 2
    assert int(result["hourly_forecasts"]) == 2
    assert worker_names
    assert all(name.startswith("collector-provider") for name in worker_names)
    assert threading.current_thread().name not in worker_names


def test_final_actual_cannot_be_replaced_by_archive_or_provisional() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    target = date(2026, 8, 14)
    with factory() as session:
        session.add(
            DailyActual(
                airport="LEMD",
                target_date=target,
                max_temp_c=39.0,
                source="stored-metar-station",
            )
        )
        session.flush()
        for source in ("open-meteo-archive", "metar-provisional"):
            assert (
                service._store_actual_rows(
                    session,
                    "LEMD",
                    [{"target_date": target, "max_temp_c": 38.0}],
                    source=source,
                    label="v10.7.7 regression",
                )
                == 0
            )
        actual = session.scalar(select(DailyActual))
    assert actual is not None
    assert actual.max_temp_c == 39.0
    assert actual.source == "stored-metar-station"


def test_shadow_row_is_not_stored_without_raw_probability_lineage() -> None:
    nowcast = SimpleNamespace(
        probabilities={30: 1.0},
        stage_probabilities={},
    )
    market_rows = [
        {
            "target_date": date(2026, 8, 15),
            "captured_at": datetime(2026, 8, 15, 10, tzinfo=timezone.utc),
            "market_id": "m30",
            "event_slug": "temperature",
            "bucket_label": "30 C",
            "bucket_low_c": 30,
            "bucket_high_c": 30,
            "yes_price": 0.5,
        }
    ]
    assert service._record_shadow_evaluations(
        None,
        "EDDM",
        {"timezone": "Europe/Berlin"},
        market_rows,
        {},
        nowcast,
    ) == (0, 0)


def test_provisional_actual_does_not_increment_oos_promotion_days() -> None:
    target = date(2026, 8, 14)
    captured = datetime(2026, 8, 14, 10, tzinfo=timezone.utc)
    common = {
        "airport": "EDDM",
        "target_date": target,
        "captured_at": captured,
        "timing": "D0 live",
    }
    variants = pd.DataFrame(
        [
            {
                **common,
                "variant": "Champion",
                "factor": None,
                "forecast_c": 30.0,
                "probabilities_json": '{"30":1.0}',
            },
            {
                **common,
                "variant": "Analog Memory Challenger",
                "factor": "regime_memory_analog",
                "forecast_c": 31.0,
                "probabilities_json": '{"31":1.0}',
            },
        ]
    )
    actuals = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "target_date": target,
                "max_temp_c": 31.0,
                "source": "metar-provisional",
            }
        ]
    )
    gate = evaluate_promotion_gate(
        variants,
        actuals,
        timing_group="D0 live",
    )
    assert gate.oos_days == 0
    assert not gate.eligible


def test_peak_lock_ablation_is_diagnostic_only() -> None:
    target = date(2026, 8, 14)
    captured = datetime(2026, 8, 14, 17, tzinfo=timezone.utc)
    snapshots = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "target_date": target,
                "captured_at": captured,
                "latest_metar_at": captured - timedelta(minutes=5),
                "observed_max_c": 33.0,
                "checkpoint_label": None,
                "peak_lock_json": (
                    '{"phase":"active","remaining_model_rise_c":0.0,'
                    '"future_radiation_max_wm2":250}'
                ),
                "features_json": '{"observed_heating_rate_cph":-1.0}',
            },
            {
                "airport": "EDDM",
                "target_date": target,
                "captured_at": captured + timedelta(hours=2),
                "latest_metar_at": captured + timedelta(hours=2) - timedelta(minutes=5),
                "observed_max_c": 33.0,
                "checkpoint_label": None,
                "peak_lock_json": '{"phase":"locked"}',
                "features_json": '{}',
            },
        ]
    )
    observations = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "observed_at": captured - timedelta(minutes=5),
                "temp_c": 32.0,
            },
            {
                "airport": "EDDM",
                "observed_at": captured + timedelta(hours=1),
                "temp_c": 31.0,
            },
        ]
    )
    result = diagnostics.analyze_peak_lock_candidates(
        snapshots,
        observations,
        {"EDDM": "Europe/Berlin"},
    )
    assert result["candidate_airport_days"] == 1
    assert result["false_higher_bucket_locks"] == 0
    assert result["production_logic_changed"] is False


def test_replay_readiness_report_does_not_modify_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "research.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        session.add(
            Forecast(
                airport="EDDM",
                model="ecmwf_ifs025",
                run_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
                target_date=date(2026, 8, 15),
                max_temp_c=30,
                source="previous-runs",
                horizon="D-1",
            )
        )
        session.commit()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "manifest.json").write_text(
        '{"partition_count":0,"partitions":[]}', encoding="utf-8"
    )
    monkeypatch.setattr(diagnostics, "Session", factory)
    monkeypatch.setattr(diagnostics, "DEFAULT_ARCHIVE_DIRECTORY", archive)
    monkeypatch.setattr(
        diagnostics,
        "trading_airports",
        lambda: {"EDDM": {"timezone": "Europe/Berlin"}},
    )
    report = diagnostics.replay_readiness_report(tmp_path / "readiness.json")
    after = hashlib.sha256(database.read_bytes()).hexdigest()
    assert before == after
    assert report["writes_production_database"] is False
    assert report["provider_matrix"][0]["evidence_class"] == "reconstructed-research"
    assert not list(factory().scalars(select(ShadowEvaluation)))
