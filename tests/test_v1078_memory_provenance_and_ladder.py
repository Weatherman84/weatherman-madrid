from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from weatherman import service
from weatherman.analytics import (
    forecast_ladder_history,
    forecast_ladder_history_metrics,
)
from weatherman.db import Base, Forecast, ForecastSnapshot
from weatherman.history import read_archived_table


def _write_archive(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_archive_filters_airport_and_bounds_while_streaming(tmp_path: Path) -> None:
    archive = tmp_path / "history"
    _write_archive(
        archive / "hourly_forecasts" / "2026-08-15.jsonl.gz",
        [
            {
                "airport": airport,
                "model": "ecmwf",
                "run_at": "2026-08-15T12:00:00+00:00",
                "valid_at": valid_at,
                "temp_c": 30.0,
            }
            for airport, valid_at in (
                ("LEMD", "2026-08-16T10:00:00+00:00"),
                ("LEMD", "2026-08-17T10:00:00+00:00"),
                ("EHAM", "2026-08-16T10:00:00+00:00"),
            )
        ],
    )
    frame = read_archived_table(
        "hourly_forecasts",
        directory=archive,
        filters={"airport": "LEMD"},
        minimums={"valid_at": datetime(2026, 8, 16, tzinfo=timezone.utc)},
        maximums={"valid_at": datetime(2026, 8, 16, 23, 59, tzinfo=timezone.utc)},
    )
    assert len(frame) == 1
    assert frame.iloc[0].airport == "LEMD"
    assert frame.iloc[0].valid_at.startswith("2026-08-16")


def test_foreign_models_cannot_inflate_coverage_or_stale_freshness() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    cutoff = datetime(2026, 8, 15, 8, tzinfo=timezone.utc)
    with factory() as session:
        session.add_all(
            [
                Forecast(
                    airport="EDDM",
                    model="expected-model",
                    run_at=cutoff - timedelta(minutes=10),
                    target_date=date(2026, 8, 15),
                    max_temp_c=30,
                    source="open-meteo",
                    available_at=cutoff - timedelta(minutes=15),
                    fetched_at=cutoff - timedelta(minutes=10),
                ),
                Forecast(
                    airport="EDDM",
                    model="foreign-stale-model",
                    run_at=cutoff - timedelta(minutes=10),
                    target_date=date(2026, 8, 15),
                    max_temp_c=31,
                    source="open-meteo",
                    available_at=cutoff - timedelta(minutes=240),
                    fetched_at=cutoff - timedelta(minutes=10),
                ),
            ]
        )
        session.flush()
        metadata = service._checkpoint_provenance(
            session,
            code="EDDM",
            target=date(2026, 8, 15),
            checkpoint_at=cutoff,
            current_time=cutoff,
            label="D0 @10",
            expected_models=["expected-model"],
        )
    assert metadata["expected_model_count"] == 1
    assert metadata["source_model_count"] == 1
    assert metadata["available_model_count"] == 2
    assert metadata["fresh_model_count"] == 1
    assert metadata["source_coverage_ratio"] == 1.0
    assert metadata["source_age_at_checkpoint_minutes"] == 15
    assert metadata["freshness_status"] == "fresh"
    assert json.loads(str(metadata["extra_models_json"])) == ["foreign-stale-model"]


def test_live_provenance_separates_expected_available_and_used() -> None:
    captured = pd.Timestamp("2026-08-16T10:00:00Z")
    freshness = pd.DataFrame(
        [
            {
                "model": "ecmwf",
                "source": "open-meteo",
                "model_run_at": captured - timedelta(hours=6),
                "available_at": captured - timedelta(minutes=20),
                "fetched_at": captured - timedelta(minutes=5),
                "data_timestamp": captured - timedelta(minutes=5),
                "age_minutes": 5,
            },
            {
                "model": "foreign",
                "source": "open-meteo",
                "model_run_at": captured - timedelta(hours=6),
                "available_at": captured - timedelta(hours=2),
                "fetched_at": captured - timedelta(hours=2),
                "data_timestamp": captured - timedelta(hours=2),
                "age_minutes": 120,
            },
        ]
    )
    nowcast = SimpleNamespace(
        model_freshness=freshness,
        current=freshness[freshness.model == "ecmwf"].copy(),
    )
    metadata = service._live_snapshot_provenance(
        nowcast,
        {"models": ["ecmwf"]},
    )
    assert metadata["expected_model_count"] == 2  # configured model + meteoblue
    assert metadata["available_model_count"] == 2
    assert metadata["fresh_model_count"] == 1
    assert metadata["used_model_count"] == 1
    assert json.loads(str(metadata["used_models_json"])) == ["ecmwf"]
    assert json.loads(str(metadata["extra_models_json"])) == ["foreign"]
    assert metadata["source_age_at_checkpoint_minutes"] == 5


def test_ladder_history_uses_final_actual_and_first_live_snapshot() -> None:
    target = date(2026, 8, 15)
    common = {
        "airport": "EDDM",
        "target_date": target,
        "raw_model_mean_c": 33.0,
        "bias_corrected_c": 33.5,
        "metar_conditioned_c": 34.0,
        "final_forecast_c": 34.2,
        "freshness_status": "fresh",
        "source_age_at_checkpoint_minutes": 20.0,
        "expected_peak_at": datetime(2026, 8, 15, 14, tzinfo=timezone.utc),
        "hours_to_peak": 2.0,
    }
    snapshots = pd.DataFrame(
        [
            {
                **common,
                "captured_at": datetime(2026, 8, 14, 18, tzinfo=timezone.utc),
                "timing": "D-1",
                "checkpoint_label": "D-1 @20",
                "checkpoint_status": "scheduled-precutoff",
            },
            {
                **common,
                "captured_at": datetime(2026, 8, 15, 4, tzinfo=timezone.utc),
                "timing": "D0 morning",
                "checkpoint_label": "D0 @06",
                "checkpoint_status": "reconstructed-causal",
                "checkpoint_reconstructed": True,
            },
            {
                **common,
                "captured_at": datetime(2026, 8, 15, 8, tzinfo=timezone.utc),
                "timing": "D0 morning",
                "checkpoint_label": "D0 @10",
                "checkpoint_status": "scheduled-precutoff",
            },
            {
                **common,
                "captured_at": datetime(2026, 8, 15, 10, tzinfo=timezone.utc),
                "timing": "D0 live",
                "checkpoint_label": None,
                "final_forecast_c": 34.8,
            },
            {
                **common,
                "captured_at": datetime(2026, 8, 15, 11, tzinfo=timezone.utc),
                "timing": "D0 live",
                "checkpoint_label": None,
                "final_forecast_c": 35.2,
            },
        ]
    )
    actuals = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "target_date": target,
                "max_temp_c": 35.0,
                "source": "stored-metar-station",
            },
            {
                "airport": "EDDM",
                "target_date": date(2026, 8, 16),
                "max_temp_c": 36.0,
                "source": "metar-provisional",
            },
        ]
    )
    history = forecast_ladder_history(
        snapshots,
        actuals,
        timezone_name="Europe/Berlin",
    )
    assert len(history) == 1
    row = history.iloc[0]
    assert row.actual_status == "final"
    assert row.d0_06_evidence == "reconstructed"
    assert row.live_champion_c == 34.8
    assert round(float(row.live_champion_error_c), 2) == -0.2
    assert row.live_local_time == "12:00"
    assert not bool(row.regular_oos)
    metrics = forecast_ladder_history_metrics(history)
    live = metrics[
        metrics.stage == "First stored live snapshot after D0@10 · Champion"
    ].iloc[0]
    assert live.n == 1
    assert round(float(live.bias), 2) == -0.2
    assert round(float(live.mae), 2) == 0.2


def test_v1078_schema_and_production_research_guard() -> None:
    names = {column.name for column in ForecastSnapshot.__table__.columns}
    assert {
        "available_model_count",
        "fresh_model_count",
        "used_model_count",
        "expected_models_json",
        "available_models_json",
        "used_models_json",
        "extra_models_json",
    } <= names
    root = Path(__file__).resolve().parents[1]
    app_source = (root / "app.py").read_text(encoding="utf-8")
    config = (root / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert 'minimums={"valid_at": target_start_utc}' in app_source
    assert not (root / "pages" / "airport_research.py").exists()
    assert "showSidebarNavigation = false" in config
