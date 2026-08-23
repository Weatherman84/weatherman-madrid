from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from weatherman import service
from weatherman.db import (
    Base,
    DailyActual,
    Forecast,
    HourlyForecast,
    MarketSnapshot,
    Observation,
)


ROOT = Path(__file__).resolve().parents[1]


def test_streamlit_refresh_uses_bounded_live_trading_path() -> None:
    app = (ROOT / "app.py").read_text(encoding="utf-8")

    assert 'button("Refresh Madrid now"' in app
    assert "collect_live_trading_refresh(airport_code, target)" in app
    assert "collect([airport])" not in app
    assert "Refresh forecasts + METAR + TAF" not in app
    assert "repository_dispatch" not in app
    assert "workflow_dispatch" not in app


def test_aviation_journal_repairs_actual_outside_trading_window(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    airport = {
        "timezone": "Europe/Berlin",
        "critical_window_local": ["12:00", "17:30"],
    }
    observations = [
        {
            "observed_at": datetime(2026, 8, 10, hour, tzinfo=timezone.utc),
            "temp_c": temperature,
        }
        for hour, temperature in (
            (6, 20.0),
            (8, 24.0),
            (10, 29.0),
            (12, 32.0),
            (14, 34.0),
            (16, 33.0),
            (18, 29.0),
            (20, 25.0),
        )
    ]

    with session_factory() as session:
        session.add(
            DailyActual(
                airport="EDDM",
                target_date=date(2026, 8, 10),
                max_temp_c=28.0,
                source="metar-provisional",
            )
        )
        session.commit()

    monkeypatch.setattr(service, "Session", session_factory)
    monkeypatch.setattr(service, "init_db", lambda: None)
    monkeypatch.setattr(service, "trading_airports", lambda: {"EDDM": airport})
    monkeypatch.setattr(service, "recent_metars", lambda *args, **kwargs: observations)
    monkeypatch.setattr(service, "recent_tafs", lambda *args, **kwargs: [])

    result = service.collect_aviation_journal(
        ["EDDM"],
        now=datetime(2026, 8, 14, 6, tzinfo=timezone.utc),
    )

    with session_factory() as session:
        actual = session.scalar(
            select(DailyActual).where(
                DailyActual.airport == "EDDM",
                DailyActual.target_date == date(2026, 8, 10),
            )
        )

    assert result["counts"]["restored_actuals"] == 1
    assert actual is not None
    assert actual.max_temp_c == 34.0
    assert actual.source == "stored-metar-station"


def test_live_trading_refresh_updates_every_decision_source_with_bounded_calls(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now(timezone.utc)
    target = now.date()
    airport = {
        "timezone": "UTC",
        "latitude": 40.0,
        "longitude": -3.0,
        "elevation_m": 600,
        "models": ["model-a", "model-b"],
    }
    calls: list[tuple[str, dict]] = []

    def forecast(_airport, model, _days, **kwargs):
        calls.append((f"forecast/{model}", kwargs))
        return [
            {
                "model": model,
                "run_at": now,
                "target_date": target,
                "max_temp_c": 30.0,
                "source": "open-meteo",
                "horizon": "Live",
                "model_run_at": now,
                "available_at": now,
                "fetched_at": now,
                "provenance_status": "test",
            }
        ]

    def hourly(_airport, model, _days, **kwargs):
        calls.append((f"hourly/{model}", kwargs))
        return [
            {
                "model": model,
                "run_at": now,
                "valid_at": now,
                "temp_c": 29.0,
                "dewpoint_c": 10.0,
                "cloud_cover": 0.0,
                "wind_kph": 5.0,
                "wind_direction": 180.0,
                "radiation_wm2": 700.0,
                "temp_850hpa_c": 20.0,
            }
        ]

    def metar(*args, **kwargs):
        calls.append(("metar", kwargs))
        return [{"observed_at": now, "temp_c": 28.0}]

    def taf(*args, **kwargs):
        calls.append(("taf", kwargs))
        return []

    def market(*args, **kwargs):
        calls.append(("polymarket", kwargs))
        return [
            {
                "target_date": target,
                "event_slug": "madrid-temperature",
                "market_id": "market-30",
                "market_slug": "madrid-30",
                "token_id": "token-30",
                "bucket_label": "30°C",
                "bucket_low_c": 30.0,
                "bucket_high_c": 30.0,
                "yes_price": 0.4,
                "best_bid": 0.39,
                "best_ask": 0.41,
                "spread": 0.02,
                "volume": 1000.0,
                "liquidity": 500.0,
                "closed": False,
                "yes_won": None,
                "resolution_source": "station",
                "price_kind": "live",
                "captured_at": now,
            }
        ]

    monkeypatch.setattr(service, "Session", session_factory)
    monkeypatch.setattr(service, "init_db", lambda: None)
    monkeypatch.setattr(service, "airports", lambda: {"LEMD": airport})
    monkeypatch.setattr(service, "open_meteo_forecast", forecast)
    monkeypatch.setattr(service, "open_meteo_hourly", hourly)
    monkeypatch.setattr(service, "meteoblue_forecast", lambda *args, **kwargs: [])
    monkeypatch.setattr(service, "recent_metars", metar)
    monkeypatch.setattr(service, "recent_tafs", taf)
    monkeypatch.setattr(service, "polymarket_prices", market)

    result = service.collect_live_trading_refresh("LEMD", target)

    assert result["models_requested"] == 2
    assert result["models_refreshed"] == 2
    assert result["forecasts"] == 2
    assert result["hourly_forecasts"] == 2
    assert result["observations"] == 1
    assert result["market_prices"] == 1
    assert result["errors"] == {}
    assert {label for label, _kwargs in calls} == {
        "forecast/model-a",
        "forecast/model-b",
        "hourly/model-a",
        "hourly/model-b",
        "metar",
        "taf",
        "polymarket",
    }
    for label, kwargs in calls:
        assert kwargs["attempts"] == 1, label
        assert kwargs["timeout"] <= 7, label

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Forecast)) == 2
        assert session.scalar(select(func.count()).select_from(HourlyForecast)) == 2
        assert session.scalar(select(func.count()).select_from(Observation)) == 1
        assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 1
