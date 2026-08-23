from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from weatherman.analytics import checkpoint_completeness
from weatherman.db import (
    Base,
    DailyActual,
    Forecast,
    ForecastSnapshot,
    HourlyForecast,
    Observation,
    TafReport,
)
from weatherman.service import (
    _build_nowcast_from_session,
    _checkpoint_provenance,
    _research_checkpoint_schedule,
)
import weatherman.service as service


def test_checkpoint_schedule_uses_airport_local_time() -> None:
    airport = {
        "timezone": "Europe/Madrid",
        "decision_checkpoints_local": [
            {"label": "D-1 Evening @20:00", "target_day_offset": 1, "time": "20:00"},
            {"label": "D0 Morning @09:00", "target_day_offset": 0, "time": "09:00"},
            {"label": "First Live @12:00", "target_day_offset": 0, "time": "12:00"},
            {"label": "Late Live @16:00", "target_day_offset": 0, "time": "16:00"},
        ],
    }
    schedule = dict(_research_checkpoint_schedule(date(2026, 8, 9), airport))
    assert schedule["D-1 Evening @20:00"].astimezone(timezone.utc) == datetime(
        2026, 8, 8, 18, tzinfo=timezone.utc
    )
    assert schedule["D0 Morning @09:00"].astimezone(timezone.utc) == datetime(
        2026, 8, 9, 7, tzinfo=timezone.utc
    )
    assert schedule["First Live @12:00"].astimezone(timezone.utc) == datetime(
        2026, 8, 9, 10, tzinfo=timezone.utc
    )


def test_checkpoint_provenance_excludes_guidance_not_available_at_cutoff() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    target = date(2026, 8, 9)
    cutoff = datetime(2026, 8, 9, 8, tzinfo=timezone.utc)
    with session_factory() as session:
        session.add_all(
            [
                Forecast(
                    airport="EDDM",
                    model="available-before",
                    run_at=cutoff + timedelta(minutes=20),
                    fetched_at=cutoff + timedelta(minutes=20),
                    available_at=cutoff - timedelta(minutes=15),
                    target_date=target,
                    max_temp_c=30,
                    source="open-meteo",
                    horizon="D0-morning",
                ),
                Forecast(
                    airport="EDDM",
                    model="fetched-after-only",
                    run_at=cutoff + timedelta(minutes=20),
                    fetched_at=cutoff + timedelta(minutes=20),
                    target_date=target,
                    max_temp_c=31,
                    source="open-meteo",
                    horizon="D0-morning",
                ),
            ]
        )
        session.flush()
        metadata = _checkpoint_provenance(
            session,
            code="EDDM",
            target=target,
            checkpoint_at=cutoff,
            current_time=cutoff + timedelta(hours=2),
            label="D0 @10",
        )
    assert metadata["checkpoint_reconstructed"] is True
    assert metadata["checkpoint_status"] == "reconstructed-causal"
    assert metadata["checkpoint_gap_minutes"] == 15
    assert "available-before" in str(metadata["source_provenance_json"])
    assert "fetched-after-only" not in str(metadata["source_provenance_json"])


def test_replayed_nowcast_receives_only_information_available_before_cutoff(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    target = date(2026, 8, 9)
    cutoff = datetime(2026, 8, 9, 8, tzinfo=timezone.utc)
    captured: dict[str, pd.DataFrame] = {}

    def fake_nowcast(**kwargs):
        captured.update(
            {
                key: kwargs[key].copy()
                for key in ("forecasts", "actuals", "observations", "hourly", "tafs")
            }
        )
        return SimpleNamespace()

    monkeypatch.setattr("weatherman.service.build_live_nowcast", fake_nowcast)
    monkeypatch.setattr(
        "weatherman.service.enrich_nowcast_with_regime_memory",
        lambda nowcast, *args, **kwargs: nowcast,
    )
    monkeypatch.setattr(
        "weatherman.service.continuous_regime_profiles",
        lambda airport: {
            "post_convective": None,
            "heat": None,
            "phase": None,
            "maritime_advection": None,
            "maritime_low_range": None,
        },
    )
    with session_factory() as session:
        session.add_all(
            [
                Forecast(
                    airport="EDDM",
                    model="causal",
                    run_at=cutoff + timedelta(minutes=20),
                    fetched_at=cutoff + timedelta(minutes=20),
                    available_at=cutoff - timedelta(minutes=15),
                    target_date=target,
                    max_temp_c=30,
                    source="open-meteo",
                    horizon="D0-morning",
                ),
                Forecast(
                    airport="EDDM",
                    model="future",
                    run_at=cutoff + timedelta(minutes=20),
                    fetched_at=cutoff + timedelta(minutes=20),
                    target_date=target,
                    max_temp_c=31,
                    source="open-meteo",
                    horizon="D0-morning",
                ),
                DailyActual(
                    airport="EDDM",
                    target_date=target,
                    max_temp_c=35,
                    source="metar-provisional",
                ),
                Observation(
                    airport="EDDM",
                    observed_at=cutoff + timedelta(minutes=10),
                    temp_c=25,
                ),
                HourlyForecast(
                    airport="EDDM",
                    model="future",
                    run_at=cutoff + timedelta(minutes=10),
                    valid_at=cutoff + timedelta(hours=3),
                    temp_c=31,
                ),
                TafReport(
                    airport="EDDM",
                    issue_time=cutoff - timedelta(hours=1),
                    valid_from=cutoff,
                    valid_to=cutoff + timedelta(hours=12),
                    raw_taf="TAF EDDM",
                    collected_at=cutoff + timedelta(minutes=5),
                ),
            ]
        )
        session.flush()
        result = _build_nowcast_from_session(
            session,
            "EDDM",
            {"timezone": "Europe/Berlin"},
            target,
            cutoff,
            [],
        )
    assert result is not None
    assert captured["forecasts"].model.tolist() == ["causal"]
    assert captured["forecasts"].run_at.max() <= pd.Timestamp(cutoff)
    assert captured["actuals"].empty
    assert captured["observations"].empty
    assert captured["hourly"].empty
    assert captured["tafs"].empty


def test_checkpoint_completeness_shows_captured_and_missing_rows() -> None:
    target = date(2026, 8, 9)
    cutoff = datetime(2026, 8, 9, 8, tzinfo=timezone.utc)
    snapshots = pd.DataFrame(
        [
            {
                "airport": "EDDM",
                "target_date": target,
                "captured_at": cutoff,
                "checkpoint_at": cutoff,
                "checkpoint_label": "D0 @10",
                "checkpoint_status": "reconstructed-causal",
                "checkpoint_reconstructed": True,
                "checkpoint_gap_minutes": 18.0,
                "model_count": 5,
            }
        ]
    )
    result = checkpoint_completeness(
        snapshots,
        {"EDDM": "Europe/Berlin"},
        as_of=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
        lookback_days=1,
    ).set_index("checkpoint")
    assert result.loc["D0 @10", "status"] == "reconstructed-causal"
    assert bool(result.loc["D0 @10", "reconstructed"])
    assert result.loc["D0 @10", "models"] == 5
    assert result.loc["D0 @06", "status"] == "unavailable"
    assert result.loc["D-1 @20", "status"] == "unavailable"


def test_research_collector_reconstructs_exact_cutoff_with_provenance(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    current_time = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
    target = date(2026, 8, 9)
    captured: list[tuple[datetime, dict[str, object]]] = []
    airport = {
        "timezone": "Europe/Madrid",
        "research_models": ["test-model"],
        "models": ["test-model"],
        "decision_checkpoints_local": [
            {"label": "D0 Morning @09:00", "target_day_offset": 0, "time": "09:00"}
        ],
    }
    monkeypatch.setattr(service, "Session", session_factory)
    monkeypatch.setattr(service, "init_db", lambda: None)
    monkeypatch.setattr(service, "research_airports", lambda: {"EDDM": airport})
    monkeypatch.setattr(
        service,
        "sync_airport_universe",
        lambda: {"cities": 1, "mapped": 1, "unmapped": 0},
    )
    monkeypatch.setattr(
        service,
        "open_meteo_forecast",
        lambda *args, **kwargs: [
            {
                "model": "test-model",
                "run_at": current_time,
                "target_date": target,
                "max_temp_c": 30.0,
                "source": "open-meteo",
                "horizon": "D0-morning",
                "model_run_at": current_time - timedelta(hours=6),
                "available_at": current_time - timedelta(minutes=40),
                "fetched_at": current_time,
                "provenance_status": "verified",
            }
        ],
    )
    monkeypatch.setattr(service, "historical_actuals", lambda *args, **kwargs: [])
    with session_factory() as session:
        session.add(
            Forecast(
                airport="EDDM",
                model="test-model",
                run_at=current_time - timedelta(hours=2),
                target_date=target,
                max_temp_c=30.0,
                source="open-meteo",
                horizon="D0-morning",
                model_run_at=current_time - timedelta(hours=6),
                available_at=current_time - timedelta(hours=1, minutes=30),
                fetched_at=current_time - timedelta(hours=1),
                provenance_status="verified",
            )
        )
        session.commit()
    monkeypatch.setattr(
        service,
        "_build_nowcast_from_session",
        lambda *args, **kwargs: SimpleNamespace(),
    )

    def record(*args, checkpoint_metadata=None, **kwargs):
        captured.append((args[4], checkpoint_metadata))
        return 1

    monkeypatch.setattr(service, "_record_forecast_snapshot", record)
    monkeypatch.setattr(service, "_record_forecast_variants", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        service, "_record_regime_memory_snapshot", lambda *args, **kwargs: 0
    )
    result = service.collect_research_checkpoints(
        ["EDDM"],
        now=current_time,
        window_minutes=35,
        catchup_hours=48,
    )
    expected_cutoff = datetime(2026, 8, 9, 7, tzinfo=timezone.utc)
    d0_morning = [
        item
        for item in captured
        if item[1]["checkpoint_label"] == "D0 Morning @09:00"
        and item[0] == expected_cutoff
    ]
    assert len(d0_morning) == 1
    assert d0_morning[0][0] == expected_cutoff
    assert d0_morning[0][1]["checkpoint_reconstructed"] is True
    assert d0_morning[0][1]["checkpoint_recorded_at"] == current_time
    assert "test-model" in str(d0_morning[0][1]["source_provenance_json"])
    assert result["checkpoints_reconstructed"] >= 1


@pytest.mark.parametrize("code", ["LEMD", "EHAM", "EPWA", "LTAC", "LTFM", "EDDM"])
def test_evening_metar_path_still_writes_post_peak_snapshots(
    monkeypatch,
    code: str,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime(2026, 8, 9, 19, tzinfo=timezone.utc)
    airport = {
        "timezone": "UTC",
        "critical_window_local": ["11:00", "17:00"],
        "final_metar_collection_end_local": "21:35",
    }
    monkeypatch.setattr(service, "Session", session_factory)
    monkeypatch.setattr(service, "init_db", lambda: None)
    monkeypatch.setattr(service, "trading_airports", lambda: {code: airport})
    monkeypatch.setattr(service, "in_critical_window", lambda *args: False)
    monkeypatch.setattr(service, "in_forecast_refresh_window", lambda *args: False)
    monkeypatch.setattr(service, "in_final_metar_collection_window", lambda *args: True)
    monkeypatch.setattr(
        service,
        "recent_metars",
        lambda *args, **kwargs: [
            {
                "observed_at": now,
                "temp_c": 25.0,
                "dewpoint_c": 12.0,
                "wind_kph": 10.0,
                "wind_direction": 250.0,
                "cloud_cover": 0.0,
                "cloud_base_ft": None,
                "raw": f"{code} METAR",
            }
        ],
    )
    monkeypatch.setattr(service, "provisional_metar_actuals", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        service,
        "_build_nowcast_from_session",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(service, "_record_forecast_snapshot", lambda *args, **kwargs: 1)
    monkeypatch.setattr(service, "_record_forecast_variants", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        service, "_record_regime_memory_snapshot", lambda *args, **kwargs: 1
    )
    result = service.collect_live_decision_checkpoints([code], now=now)
    assert result["airports_due"] == 0
    assert result["final_metar_airports_due"] == 1
    assert result["forecast_snapshots"] == 1
    assert result["forecast_variants"] == 1
    assert result["regime_memory_snapshots"] == 1
    assert result["post_peak_snapshots"] == 1


def test_evening_path_does_not_duplicate_same_metar(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime(2026, 8, 9, 19, tzinfo=timezone.utc)
    airport = {
        "timezone": "UTC",
        "critical_window_local": ["11:00", "17:00"],
        "final_metar_collection_end_local": "21:35",
    }
    with session_factory() as session:
        session.add(
            ForecastSnapshot(
                airport="EHAM",
                target_date=now.date(),
                captured_at=now - timedelta(minutes=10),
                timing="D0 live",
                raw_model_mean_c=25.0,
                bias_corrected_c=25.0,
                final_forecast_c=25.0,
                raw_spread_c=1.0,
                bias_corrected_spread_c=1.0,
                final_spread_c=1.0,
                latest_metar_at=now,
                day_phase="post-peak",
                model_count=5,
            )
        )
        session.commit()
    monkeypatch.setattr(service, "Session", session_factory)
    monkeypatch.setattr(service, "init_db", lambda: None)
    monkeypatch.setattr(service, "trading_airports", lambda: {"EHAM": airport})
    monkeypatch.setattr(service, "in_critical_window", lambda *args: False)
    monkeypatch.setattr(service, "in_forecast_refresh_window", lambda *args: False)
    monkeypatch.setattr(service, "in_final_metar_collection_window", lambda *args: True)
    monkeypatch.setattr(
        service,
        "recent_metars",
        lambda *args, **kwargs: [
            {
                "observed_at": now,
                "temp_c": 25.0,
                "dewpoint_c": 12.0,
                "wind_kph": 10.0,
                "wind_direction": 250.0,
                "cloud_cover": 0.0,
                "cloud_base_ft": None,
                "raw": "EHAM METAR",
            }
        ],
    )
    monkeypatch.setattr(service, "provisional_metar_actuals", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        service,
        "_build_nowcast_from_session",
        lambda *args, **kwargs: pytest.fail("same METAR must not be journaled twice"),
    )
    result = service.collect_live_decision_checkpoints(["EHAM"], now=now)
    assert result["post_peak_no_new_metar"] == 1
    assert result["post_peak_snapshots"] == 0
