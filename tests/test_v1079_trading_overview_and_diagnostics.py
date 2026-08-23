from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from weatherman import service
from weatherman.analytics import (
    first_stored_live_champion,
    forecast_ladder_history,
    forecast_ladder_oos_reliability,
)
from weatherman.db import Base, DailyActual, ForecastSnapshot
from weatherman.live_ui import _bucket_table
from weatherman.post_peak_diagnostics import post_peak_diagnostic
from weatherman.terminology import CHECKPOINT_LABELS, FORECAST_STAGE_LABELS


ROOT = Path(__file__).resolve().parents[1]


def test_relevant_buckets_default_to_temperature_order() -> None:
    frame = _bucket_table({29: 0.15, 30: 0.60, 31: 0.25}, pd.DataFrame(), {})
    assert frame.Bucket.tolist() == ["29 °C", "30 °C", "31 °C"]
    relevant = frame.nsmallest(2, "_relevance_rank").sort_values("_bucket_order")
    assert relevant.Bucket.tolist() == ["30 °C", "31 °C"]


def test_oos_reliability_uses_stage_specific_scheduled_evidence() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "target_date": date(2026, 8, 18),
                "captured_at": datetime(2026, 8, 18, 8, tzinfo=timezone.utc),
                "timing": "D0 morning",
                "checkpoint_label": "D0 @10",
                "checkpoint_status": "scheduled-precutoff",
                "raw_model_mean_c": 20.05,
                "bias_corrected_c": 21.0,
                "metar_conditioned_c": 21.4,
                "final_forecast_c": 21.42,
                "expected_peak_at": datetime(2026, 8, 18, 13, tzinfo=timezone.utc),
                "hours_to_peak": 5.0,
            },
            {
                "airport": "EDDM",
                "target_date": date(2026, 8, 18),
                "captured_at": datetime(2026, 8, 18, 14, tzinfo=timezone.utc),
                "timing": "D0 live",
                "checkpoint_label": None,
                "raw_model_mean_c": 20.0,
                "bias_corrected_c": 20.0,
                "metar_conditioned_c": 20.0,
                "final_forecast_c": 20.0,
                "expected_peak_at": datetime(2026, 8, 18, 13, tzinfo=timezone.utc),
                "hours_to_peak": -1.0,
            },
        ]
    )
    actuals = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "target_date": date(2026, 8, 18),
                "max_temp_c": 20.0,
                "source": "stored-metar-station",
            }
        ]
    )
    history = forecast_ladder_history(snapshots, actuals, timezone_name="Europe/Berlin")
    reliability = forecast_ladder_oos_reliability(history)
    d010 = reliability[reliability.stage == "D0 @10:00 LT · Champion"].iloc[0]
    live = reliability[
        reliability.stage == "First stored live snapshot after D0@10 · Champion"
    ].iloc[0]
    assert d010.n == 1
    assert d010.exact_bucket == 0.0
    assert d010.within_1c == 0.0
    assert live.n == 0  # late/post-peak is diagnostic, not timing reliability


def test_post_peak_radiation_diagnostic_never_changes_production() -> None:
    nowcast = SimpleNamespace(
        observed_max=25.0,
        probabilities={25: 0.25, 26: 0.40, 27: 0.35},
        expected_peak_at=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
        remaining_rise_c=0.0,
        heating_rate=0.0,
        future_radiation_max=190.0,
        day_status=SimpleNamespace(is_locked=False, phase="active", label="Active"),
        live_features={"post_convective_uncertainty_active": 1},
    )
    result = post_peak_diagnostic(
        nowcast, datetime(2026, 8, 18, 17, tzinfo=timezone.utc)
    )
    assert result["radiation_only_candidate"] is True
    assert result["upper_tail_probability"] == 0.75
    assert result["research_only"] is True
    assert result["production_changed"] is False
    assert nowcast.probabilities == {25: 0.25, 26: 0.40, 27: 0.35}


def test_final_actual_quality_remains_monotone_after_48_hour_window() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    target = date(2026, 8, 10)
    with factory() as session:
        service._store_actual_rows(
            session,
            "EDDM",
            [{"target_date": target, "max_temp_c": 34.0}],
            source="stored-metar-station",
            label="final",
        )
        session.commit()
        service._store_actual_rows(
            session,
            "EDDM",
            [{"target_date": target, "max_temp_c": 31.0}],
            source="metar-provisional",
            label="48-hour rolling window",
        )
        session.commit()
        actual = session.scalar(
            select(DailyActual).where(
                DailyActual.airport == "EDDM", DailyActual.target_date == target
            )
        )
    assert actual is not None
    assert actual.max_temp_c == 34.0
    assert actual.source == "stored-metar-station"


def test_checkpoint_schema_contains_market_and_post_peak_lineage() -> None:
    columns = {column.name for column in ForecastSnapshot.__table__.columns}
    assert {
        "market_snapshot_status",
        "market_snapshot_at",
        "market_bucket_count",
        "post_peak_diagnostic_json",
    }.issubset(columns)


def test_all_airport_refresh_is_serial_and_failure_isolated(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        service,
        "trading_airports",
        lambda: {
            "LEMD": {"timezone": "UTC"},
            "EDDM": {"timezone": "UTC"},
        },
    )

    def refresh(code: str, _target: date) -> dict[str, object]:
        calls.append(code)
        if code == "EDDM":
            raise RuntimeError("provider down")
        return {"errors": {}, "elapsed_seconds": 1.0}

    monkeypatch.setattr(service, "collect_live_trading_refresh", refresh)
    result = service.collect_all_live_trading_refresh()
    assert calls == ["LEMD", "EDDM"]
    assert result["successful_airports"] == 1
    assert result["failed_airports"] == ["EDDM"]


def test_ui_scope_and_central_terminology_are_visible() -> None:
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'airport_code = "LEMD"' in app
    assert 'button("Refresh Madrid now"' in app
    assert "Champion reliability by fixed checkpoint" in app
    assert "First Live @12:00" in app
    assert CHECKPOINT_LABELS["d0_10"] == "D0 @10:00 LT"
    assert FORECAST_STAGE_LABELS["taf"] == "TAF guidance"


def test_live_lineage_records_model_exclusion_reason() -> None:
    captured = pd.Timestamp("2026-08-18T12:00:00Z")
    freshness = pd.DataFrame(
        [
            {"model": "fresh", "age_minutes": 5, "data_timestamp": captured},
            {"model": "stale", "age_minutes": 180, "data_timestamp": captured},
            {"model": "extra", "age_minutes": 5, "data_timestamp": captured},
        ]
    )
    nowcast = SimpleNamespace(
        model_freshness=freshness,
        current=freshness[freshness.model == "fresh"],
    )
    metadata = service._live_snapshot_provenance(
        nowcast, {"models": ["fresh", "stale"]}
    )
    provenance = {
        row["model"]: row for row in json.loads(str(metadata["source_provenance_json"]))
    }
    assert provenance["fresh"]["selection_status"] == "used"
    assert provenance["fresh"]["exclusion_reason"] is None
    assert provenance["stale"]["exclusion_reason"] == "stale at this checkpoint"
    assert provenance["extra"]["selection_status"] == "available-not-expected"


def test_first_live_champion_does_not_require_a_final_actual() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "target_date": date(2026, 8, 19),
                "captured_at": datetime(2026, 8, 19, 8, 30, tzinfo=timezone.utc),
                "timing": "D0 morning",
                "checkpoint_label": "D0 @10",
                "final_forecast_c": 37.0,
            },
            {
                "airport": "LEMD",
                "target_date": date(2026, 8, 19),
                "captured_at": datetime(2026, 8, 19, 10, 5, tzinfo=timezone.utc),
                "timing": "D0 live",
                "checkpoint_label": None,
                "final_forecast_c": 38.2,
                "latest_metar_at": datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc),
                "expected_peak_at": datetime(2026, 8, 19, 15, tzinfo=timezone.utc),
                "hours_to_peak": 4.9,
                "freshness_status": "fresh",
                "source_age_at_checkpoint_minutes": 12.0,
            },
            {
                "airport": "LEMD",
                "target_date": date(2026, 8, 19),
                "captured_at": datetime(2026, 8, 19, 11, 5, tzinfo=timezone.utc),
                "timing": "D0 live",
                "checkpoint_label": None,
                "final_forecast_c": 39.0,
                "latest_metar_at": datetime(2026, 8, 19, 11, 0, tzinfo=timezone.utc),
                "expected_peak_at": datetime(2026, 8, 19, 15, tzinfo=timezone.utc),
                "hours_to_peak": 3.9,
            },
        ]
    )
    first = first_stored_live_champion(
        snapshots,
        target=date(2026, 8, 19),
        timezone_name="Europe/Madrid",
    )
    assert first is not None
    assert first["champion_c"] == 38.2
    assert first["forecast_at"] == datetime(2026, 8, 19, 10, 5, tzinfo=timezone.utc)
    assert first["latest_metar_at"] == datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc)
    assert first["evidence"] == "scheduled"


def test_overview_and_detail_use_the_same_canonical_nowcast_builder() -> None:
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    source = (ROOT / "src" / "weatherman" / "service.py").read_text(encoding="utf-8")
    assert "nowcast = build_current_live_nowcast(" in app
    assert "return build_current_live_nowcast(" in source
    assert 'filters={"airport": airport}' in app
