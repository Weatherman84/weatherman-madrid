from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from weatherman import collector as collector_module
from weatherman.analytics import DayStatus
from weatherman.db import (
    Base,
    CollectionCoverage,
    CollectionRun,
    Forecast,
    ForecastSnapshot,
    MarketSnapshot,
    Observation,
)
from weatherman.history import (
    HistoryArchiveError,
    read_archive_live,
    validate_history_archive,
)
from weatherman.maintenance import maintain_sqlite_database
from weatherman.nowcast import build_live_nowcast
from weatherman.taf import (
    build_taf_guidance,
    taf_checkpoint_verification_frame,
    taf_verification_frame,
)


def _taf(
    issue: datetime,
    tx: float,
    *,
    first_seen: datetime,
    raw: str,
    backfilled: bool = False,
) -> dict[str, object]:
    target_at = datetime(2026, 8, 10, 16, tzinfo=timezone.utc)
    return {
        "airport": "LEMD",
        "issue_time": issue,
        "valid_from": datetime(2026, 8, 10, 0, tzinfo=timezone.utc),
        "valid_to": datetime(2026, 8, 11, 6, tzinfo=timezone.utc),
        "raw_taf": raw,
        "is_amended": False,
        "is_corrected": False,
        "max_temp_c": tx,
        "max_temp_at": target_at,
        "periods_json": "[]",
        "collected_at": first_seen,
        "first_seen_at": first_seen,
        "fetched_at": first_seen,
        "content_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "backfilled": backfilled,
    }


def test_taf_revisions_keep_tx37_tx38_and_checkpoint_causality() -> None:
    early = _taf(
        datetime(2026, 8, 10, 5, tzinfo=timezone.utc),
        37,
        first_seen=datetime(2026, 8, 10, 5, 5, tzinfo=timezone.utc),
        raw="TAF LEMD 100500Z TX37/1016Z",
    )
    revised = _taf(
        datetime(2026, 8, 10, 11, tzinfo=timezone.utc),
        38,
        first_seen=datetime(2026, 8, 10, 11, 5, tzinfo=timezone.utc),
        raw="TAF LEMD 101100Z TX38/1016Z",
    )
    tafs = pd.DataFrame([early, revised])
    actuals = pd.DataFrame(
        [{"airport": "LEMD", "target_date": date(2026, 8, 10), "max_temp_c": 37.3}]
    )

    revisions = taf_verification_frame(tafs, actuals, {"LEMD": "Europe/Madrid"})
    assert revisions.max_temp_c_taf.tolist() == [37, 38]
    assert revisions.error.round(1).tolist() == [-0.3, 0.7]

    checkpoints = taf_checkpoint_verification_frame(
        tafs, actuals, {"LEMD": "Europe/Madrid"}
    )
    morning = checkpoints[checkpoints.timing == "D0@10"].iloc[0]
    assert morning.max_temp_c_taf == 37
    assert morning.issue_time == pd.Timestamp("2026-08-10T05:00:00Z")

    before_revision = build_taf_guidance(
        tafs,
        timezone_name="Europe/Madrid",
        target=date(2026, 8, 10),
        as_of=datetime(2026, 8, 10, 10, 30, tzinfo=timezone.utc),
        model_mean=37,
    )
    assert before_revision is not None
    assert before_revision.max_temp_c == 37


def test_backfilled_taf_cannot_rewrite_an_earlier_weatherman_checkpoint() -> None:
    early = _taf(
        datetime(2026, 8, 10, 5, tzinfo=timezone.utc),
        37,
        first_seen=datetime(2026, 8, 10, 5, 5, tzinfo=timezone.utc),
        raw="TAF LEMD 100500Z TX37/1016Z",
    )
    late_backfill = _taf(
        datetime(2026, 8, 10, 11, tzinfo=timezone.utc),
        38,
        first_seen=datetime(2026, 8, 11, 8, tzinfo=timezone.utc),
        raw="TAF LEMD 101100Z TX38/1016Z",
        backfilled=True,
    )
    guidance = build_taf_guidance(
        pd.DataFrame([early, late_backfill]),
        timezone_name="Europe/Madrid",
        target=date(2026, 8, 10),
        as_of=datetime(2026, 8, 10, 13, tzinfo=timezone.utc),
        model_mean=37,
    )
    assert guidance is not None
    assert guidance.max_temp_c == 37
    assert guidance.backfilled is False


def test_stage1_archives_all_history_and_archive_live_is_identical(tmp_path) -> None:
    database = tmp_path / "weatherman.db"
    archive = tmp_path / "history"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    old = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    recent = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    with factory() as session:
        session.add_all(
            [
                Forecast(
                    airport="LEMD",
                    model="ecmwf",
                    run_at=old,
                    target_date=date(2026, 8, 2),
                    max_temp_c=36,
                    source="open-meteo",
                ),
                Forecast(
                    airport="LEMD",
                    model="ecmwf",
                    run_at=recent,
                    target_date=date(2026, 8, 11),
                    max_temp_c=38,
                    source="open-meteo",
                ),
                Observation(airport="LEMD", observed_at=old, temp_c=35, raw="old"),
                Observation(airport="LEMD", observed_at=recent, temp_c=37, raw="recent"),
                MarketSnapshot(
                    airport="LEMD",
                    target_date=date(2026, 8, 2),
                    event_slug="event",
                    market_id="market-old",
                    market_slug="market-old",
                    bucket_label="37",
                    yes_price=0.5,
                    captured_at=old,
                ),
            ]
        )
        session.commit()
        before_forecasts = read_archive_live(
            Forecast, session.bind, filters={"airport": "LEMD"}, directory=archive
        )
        before_observations = read_archive_live(
            Observation, session.bind, filters={"airport": "LEMD"}, directory=archive
        )
    engine.dispose()

    first = maintain_sqlite_database(
        database,
        retention_days=3,
        archive_directory=archive,
        reference_time=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    second = maintain_sqlite_database(
        database,
        retention_days=3,
        archive_directory=archive,
        reference_time=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    migrated_engine = create_engine(f"sqlite:///{database}")
    after_forecasts = read_archive_live(
        Forecast, migrated_engine, filters={"airport": "LEMD"}, directory=archive
    )
    after_observations = read_archive_live(
        Observation, migrated_engine, filters={"airport": "LEMD"}, directory=archive
    )
    migrated_engine.dispose()

    assert before_forecasts[["airport", "model", "run_at"]].equals(
        after_forecasts[["airport", "model", "run_at"]]
    )
    assert before_observations[["airport", "observed_at", "temp_c"]].equals(
        after_observations[["airport", "observed_at", "temp_c"]]
    )
    assert first["database_bytes"] < 48 * 1024 * 1024
    assert second["archive_rows_added"] == 0
    assert sum(int(item.get("pruned", 0)) for item in second["tables"]) == 0
    manifest = validate_history_archive(archive)
    assert manifest["total_rows"] >= 3
    assert {item["table"] for item in manifest["partitions"]} >= {
        "forecasts",
        "observations",
        "market_snapshots",
    }


def test_archive_hash_mismatch_does_not_rewrite_the_trust_manifest(tmp_path) -> None:
    database = tmp_path / "weatherman.db"
    archive = tmp_path / "history"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        session.add(
            Observation(
                airport="LEMD",
                observed_at=datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
                temp_c=35,
                raw="original",
            )
        )
        session.commit()
    engine.dispose()
    maintain_sqlite_database(
        database,
        retention_days=3,
        archive_directory=archive,
        reference_time=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    manifest_path = archive / "manifest.json"
    trusted_manifest = manifest_path.read_bytes()
    partition = next((archive / "observations").glob("*.jsonl.gz"))
    with gzip.open(partition, "rt", encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle]
    records[0]["raw"] = "tampered"
    with gzip.open(partition, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    with pytest.raises(HistoryArchiveError, match="manifest mismatch"):
        validate_history_archive(archive)
    assert manifest_path.read_bytes() == trusted_manifest


def test_next_collector_run_confirms_prior_pending_git_persistence(
    tmp_path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'collector.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    scheduled = datetime(2026, 8, 11, 8, tzinfo=timezone.utc)
    with factory() as session:
        session.add(
            CollectionRun(
                run_id="prior",
                scheduled_at=scheduled,
                started_at=scheduled,
                ended_at=scheduled + timedelta(minutes=1),
                collector_version="10.7.3",
                trigger="schedule",
                overall_status="success",
                airports_json='["LEMD"]',
                source_status_json="{}",
                rows_read_json="{}",
                rows_written_json="{}",
                source_age_json="{}",
                persistence_status="pending_commit",
            )
        )
        session.add(
            CollectionCoverage(
                run_id="prior",
                airport="LEMD",
                data_type="taf",
                status="stored_pending_persistence",
                scheduled_at=scheduled,
            )
        )
        session.commit()
    monkeypatch.setattr(collector_module, "Session", factory)

    collector_module._start_run(
        "current",
        scheduled_at=scheduled + timedelta(minutes=10),
        started_at=scheduled + timedelta(minutes=11),
        trigger="schedule",
        airport_codes=["LEMD"],
    )

    with factory() as session:
        prior = session.query(CollectionRun).filter_by(run_id="prior").one()
        current = session.query(CollectionRun).filter_by(run_id="current").one()
        coverage = session.query(CollectionCoverage).filter_by(run_id="prior").one()
        assert prior.persistence_status == "persisted"
        assert coverage.status == "stored_persisted"
        assert current.overall_status == "running"
        assert current.scheduler_drift_seconds == 60
    engine.dispose()


def test_coverage_audit_names_collector_gap_and_failure(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'coverage.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    with factory() as session:
        for run_id, minutes, status, reason in (
            ("early", 40, "success", None),
            ("failed", 5, "failed", "provider timeout"),
        ):
            at = now - timedelta(minutes=minutes)
            session.add(
                CollectionRun(
                    run_id=run_id,
                    scheduled_at=at,
                    started_at=at,
                    ended_at=at + timedelta(minutes=1),
                    collector_version="10.7.3",
                    trigger="schedule",
                    overall_status=status,
                    airports_json="[]",
                    source_status_json="{}",
                    rows_read_json="{}",
                    rows_written_json="{}",
                    source_age_json="{}",
                    persistence_status="persisted",
                    error_reason=reason,
                )
            )
        session.commit()
    monkeypatch.setattr(collector_module, "Session", factory)
    monkeypatch.setattr(collector_module, "trading_airports", lambda: {})
    monkeypatch.setattr(collector_module, "configured_sqlite_path", lambda: None)
    monkeypatch.setattr(
        collector_module,
        "validate_history_archive",
        lambda _path: {"partition_count": 0, "total_rows": 0},
    )

    report = collector_module.coverage_audit(
        now=now, report_path=tmp_path / "coverage.json"
    )

    warning_types = {item["type"] for item in report["warnings"]}
    assert "collector_gap" in warning_types
    assert "collector_failed" in warning_types
    assert any("provider timeout" in item["message"] for item in report["warnings"])
    engine.dispose()


def test_terminal_peak_lock_survives_epwa_night_reheating() -> None:
    target = date(2026, 8, 10)
    as_of = datetime(2026, 8, 10, 20, 41, tzinfo=timezone.utc)
    forecasts = pd.DataFrame(
        [
            {
                "airport": "EPWA",
                "model": model,
                "run_at": as_of - timedelta(minutes=20),
                "fetched_at": as_of - timedelta(minutes=20),
                "target_date": target,
                "max_temp_c": value,
                "source": "open-meteo",
                "horizon": "Live",
            }
            for model, value in (("ecmwf", 32.0), ("gfs", 33.0))
        ]
    )
    observations = pd.DataFrame(
        [
            {
                "airport": "EPWA",
                "observed_at": datetime(2026, 8, 10, 16, tzinfo=timezone.utc),
                "temp_c": 32.0,
                "dewpoint_c": 12.0,
            },
            {
                "airport": "EPWA",
                "observed_at": datetime(2026, 8, 10, 20, 30, tzinfo=timezone.utc),
                "temp_c": 24.0,
                "dewpoint_c": 13.0,
            },
        ]
    )
    prior = DayStatus(
        phase="locked",
        label="Peak locked",
        is_locked=True,
        minimum_bucket=32,
        maximum_bucket=32,
        remaining_heating_c=0.0,
        explanation="Locked at 20:00 UTC",
    )
    nowcast = build_live_nowcast(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=observations,
        hourly=pd.DataFrame(),
        markets=pd.DataFrame(),
        timezone_name="Europe/Warsaw",
        target=target,
        as_of=as_of,
        prior_terminal_status=prior,
        _build_challengers=False,
    )
    assert nowcast is not None
    assert nowcast.day_status.is_locked is True
    assert nowcast.day_status.phase == "locked"
    assert nowcast.probabilities == {32: 1.0}
    assert nowcast.final_forecast_mean == 32
    assert nowcast.final_forecast_spread == 0


def test_forecast_snapshot_schema_proves_the_used_taf_revision() -> None:
    columns = ForecastSnapshot.__table__.columns
    assert {column.name for column in columns} >= {
        "taf_report_id",
        "taf_issue_time",
        "taf_first_seen_at",
        "taf_max_temp_c",
        "taf_content_hash",
    }
