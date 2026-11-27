from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import weatherman.checkpoint_delivery_guard as guard
from weatherman.db import Base, CollectionRun, ForecastSnapshot


AIRPORT = {
    "timezone": "Europe/Madrid",
    "decision_checkpoints_local": [
        {"label": "D0 Morning @09:00", "target_day_offset": 0, "time": "09:00"}
    ],
}


def test_guard_adds_seasonal_morning_precutoff_refresh() -> None:
    assert guard.guarded_collection_mode(
        "auto", datetime(2026, 11, 20, 7, 7, tzinfo=timezone.utc)
    ) == "fixed"
    assert guard.guarded_collection_mode(
        "auto", datetime(2026, 8, 20, 6, 7, tzinfo=timezone.utc)
    ) == "fixed"
    assert guard.guarded_collection_mode(
        "auto", datetime(2026, 11, 20, 7, 37, tzinfo=timezone.utc)
    ) == "auto"
    assert guard.guarded_collection_mode(
        "aviation", datetime(2026, 11, 20, 7, 7, tzinfo=timezone.utc)
    ) == "aviation"


def test_guard_records_missing_due_checkpoint_with_provider_reason(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(guard, "Session", factory)
    monkeypatch.setattr(guard, "research_airports", lambda: {"LEMD": AIRPORT})
    current = datetime(2026, 11, 18, 8, 10, tzinfo=timezone.utc)
    result = {
        "checkpoints": {"checkpoints_missing_inputs": 1},
        "live_decisions": {
            "provider_coverage": [
                {
                    "airport": "LEMD",
                    "data_type": "open-meteo",
                    "status": "source_or_parser_failed",
                    "rows_read": 0,
                    "rows_written": 0,
                    "reason": "TimeoutException: provider unavailable",
                }
            ]
        },
    }

    attempts = guard.checkpoint_delivery_attempts(
        ["LEMD"], now=current, collector_result=result
    )

    assert len(attempts) == 1
    assert attempts[0]["checkpoint_label"] == "D0 Morning @09:00"
    assert attempts[0]["checkpoint_at"] == "2026-11-18T08:00:00+00:00"
    assert attempts[0]["status"] == "missing"
    assert attempts[0]["reason"] == "no_forecast_inputs_available_at_or_before_cutoff"
    assert attempts[0]["provider_diagnostics"][0]["reason"].startswith(
        "TimeoutException"
    )


def test_guard_links_written_snapshot_and_persists_partial_run(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(guard, "Session", factory)
    monkeypatch.setattr(guard, "research_airports", lambda: {"LEMD": AIRPORT})
    cutoff = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)
    with factory() as session:
        session.add(
            ForecastSnapshot(
                airport="LEMD",
                target_date=date(2026, 11, 18),
                captured_at=cutoff,
                timing="D0 morning",
                raw_model_mean_c=15.0,
                bias_corrected_c=15.1,
                final_forecast_c=15.2,
                raw_spread_c=0.8,
                bias_corrected_spread_c=0.8,
                final_spread_c=0.9,
                day_phase="morning",
                model_count=8,
                checkpoint_label="D0 Morning @09:00",
                checkpoint_at=cutoff,
                checkpoint_recorded_at=cutoff.replace(minute=8),
                checkpoint_status="scheduled-causal",
                checkpoint_reconstructed=False,
                evidence_class="complete",
                expected_model_count=9,
                available_model_count=8,
                source_model_count=8,
                fresh_model_count=8,
                used_model_count=8,
            )
        )
        session.add(
            CollectionRun(
                run_id="run-1",
                scheduled_at=cutoff.replace(minute=7),
                started_at=cutoff.replace(minute=7),
                collector_version="1.0.22",
                trigger="schedule",
                overall_status="success",
                airports_json='["LEMD"]',
                rows_written_json='{"collector":{"checkpoints":{}}}',
            )
        )
        session.commit()

    attempts = guard.checkpoint_delivery_attempts(
        ["LEMD"],
        now=cutoff.replace(minute=10),
        collector_result={"checkpoints": {}, "live_decisions": {}},
    )
    assert attempts[0]["status"] == "written"
    assert attempts[0]["checkpoint_recorded_at"] == "2026-11-18T08:08:00+00:00"
    assert guard.persist_delivery_audit("run-1", attempts) == "complete"

    missing = [{**attempts[0], "status": "missing", "reason": "test-gap"}]
    assert guard.persist_delivery_audit("run-1", missing) == "partial"
    with factory() as session:
        row = session.scalar(select(CollectionRun).where(CollectionRun.run_id == "run-1"))
        payload = json.loads(row.rows_written_json)
    assert row.overall_status == "partial"
    assert payload["collector"]["checkpoints"]["checkpoint_attempts"][0]["reason"] == "test-gap"
