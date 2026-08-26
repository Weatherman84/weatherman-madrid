from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from weatherman.daily_analysis_export import (
    _safe_error_text,
    assert_export_safe,
    build_daily_analysis_export,
)
from weatherman.db import (
    Base,
    DailyActual,
    ForecastSnapshot,
    ForecastVariantSnapshot,
    Observation,
    RegimeMemorySnapshot,
)


def test_daily_analysis_export_is_read_only_scoped_and_explainable() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    captured = datetime(2026, 8, 25, 10, tzinfo=timezone.utc)
    with factory() as session:
        session.add(
            ForecastSnapshot(
                airport="LEMD",
                target_date=date(2026, 8, 25),
                captured_at=captured,
                timing="D0",
                raw_model_mean_c=28.0,
                bias_corrected_c=28.2,
                final_forecast_c=28.4,
                raw_spread_c=0.8,
                bias_corrected_spread_c=0.7,
                final_spread_c=0.9,
                day_phase="active",
                model_count=3,
                checkpoint_label="First Live @12:00",
                checkpoint_at=captured,
                checkpoint_recorded_at=captured,
                checkpoint_status="scheduled-causal",
                evidence_class="complete",
                freshness_status="fresh",
                features_json=json.dumps({"observed_heating_rate_cph": 1.1}),
                source_provenance_json=json.dumps(
                    [{"model": "ecmwf_ifs025", "used_by_champion": True}]
                ),
                persistent_hot_active=True,
                persistent_hot_adjustment_c=0.2,
            )
        )
        session.add(
            ForecastVariantSnapshot(
                airport="LEMD",
                target_date=date(2026, 8, 25),
                captured_at=captured,
                timing="D0",
                variant="Champion",
                factor=None,
                forecast_c=28.4,
                spread_c=0.9,
                probabilities_json='{"28":0.6,"29":0.4}',
                forecast_confidence=74,
                day_phase="active",
            )
        )
        session.add(
            RegimeMemorySnapshot(
                airport="LEMD",
                target_date=date(2026, 8, 25),
                captured_at=captured,
                timing="D0",
                status="watch",
                label="Persistent Hot",
                confidence=65,
                suggested_forecast_c=28.5,
                suggested_spread_c=1.0,
                promotion_status="research-only",
                explanation="No promotion.",
                regimes_json='[{"name":"persistent_hot","status":"active"}]',
            )
        )
        session.add(
            DailyActual(
                airport="LEMD",
                target_date=date(2026, 8, 25),
                max_temp_c=28.0,
                source="stored-metar-station",
            )
        )
        session.add(
            Observation(
                airport="LEMD",
                observed_at=captured,
                temp_c=25.0,
                raw="LEMD 251000Z test",
            )
        )
        session.commit()

        before = session.query(ForecastSnapshot).count()
        payload = build_daily_analysis_export(
            session,
            generated_at=datetime(2026, 8, 26, 19, 15, tzinfo=timezone.utc),
            days=7,
        )
        after = session.query(ForecastSnapshot).count()

    assert before == after == 1
    assert payload["airport"] == "LEMD"
    assert payload["contains_credentials"] is False
    assert payload["writes_production_database"] is False
    assert payload["actuals"][0]["is_final_station_actual"] is True
    checkpoint = payload["checkpoints"][0]
    assert checkpoint["forecast_chain_c"]["champion"] == 28.4
    assert checkpoint["forecast_drivers"]["observed_heating_rate_cph"] == 1.1
    assert checkpoint["adjustment_impacts"]["persistent_hot_c"] == 0.2
    assert checkpoint["regime_flags"]["persistent_hot_active"] is True
    assert checkpoint["champion_and_challengers"][0]["bucket_probabilities"]["28"] == 0.6
    assert checkpoint["regime_memory"]["regimes"][0]["name"] == "persistent_hot"
    assert "DATABASE_URL" not in json.dumps(payload)


def test_daily_analysis_export_rejects_database_secret(monkeypatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://analyst:very-secret-password@example.invalid/weather",
    )
    payload = {"reason": "failed: very-secret-password"}

    try:
        assert_export_safe(payload)
    except ValueError as exc:
        assert "credentials" in str(exc)
    else:
        raise AssertionError("database password was not rejected")


def test_daily_analysis_export_redacts_provider_query_credentials() -> None:
    source = (
        "HTTP 429 for https://example.invalid/weather?lat=40&"
        "apikey=do-not-publish-this&format=json"
    )
    redacted = _safe_error_text(source)

    assert redacted is not None
    assert "do-not-publish-this" not in redacted
    assert "apikey=REDACTED" in redacted
    assert_export_safe({"reason": redacted})
