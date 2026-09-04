from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from weatherman.daily_analysis_export import (
    _aemet_physical_payload,
    _pipeline_health_payload,
    _safe_error_text,
    _sanitize_public_value,
    assert_export_safe,
    build_daily_analysis_export,
)
import weatherman.daily_analysis_export as export_module
from weatherman.db import (
    Base,
    CollectionRun,
    DailyActual,
    ForecastSnapshot,
    ForecastVariantSnapshot,
    HourlyForecast,
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
                expected_model_count=2,
                available_model_count=2,
                fresh_model_count=2,
                used_model_count=2,
                features_json=json.dumps({"observed_heating_rate_cph": 1.1}),
                source_provenance_json=json.dumps(
                    [
                        {
                            "model": "ecmwf_ifs025",
                            "used_by_champion": True,
                            "expected": True,
                            "freshness_state": "current_latest_run",
                        },
                        {
                            "model": "gfs_global",
                            "used_by_champion": True,
                            "expected": True,
                            "freshness_state": "awaiting_next_run",
                        },
                    ]
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
        session.add(
            HourlyForecast(
                airport="LEMD",
                model="ecmwf_ifs025",
                run_at=datetime(2026, 8, 26, 8, tzinfo=timezone.utc),
                valid_at=datetime(2026, 8, 26, 10, tzinfo=timezone.utc),
                temp_c=27.0,
                dewpoint_c=9.0,
                cloud_cover=20.0,
                wind_kph=18.0,
                wind_direction=220.0,
                radiation_wm2=700.0,
                temp_850hpa_c=16.5,
            )
        )
        session.add(
            CollectionRun(
                run_id="cloudflare-slot",
                scheduled_at=datetime(2026, 8, 26, 5, 7, tzinfo=timezone.utc),
                started_at=datetime(2026, 8, 26, 5, 7, 5, tzinfo=timezone.utc),
                ended_at=datetime(2026, 8, 26, 5, 8, tzinfo=timezone.utc),
                collector_version="1.0.3",
                trigger="cloudflare",
                overall_status="success",
                airports_json='["LEMD"]',
                source_status_json="{}",
                rows_read_json="{}",
                rows_written_json="{}",
                source_age_json="{}",
                persistence_status="persisted",
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
    assert payload["schema_version"] == "1.3"
    assert payload["evaluation_targets"]["mixing_targets_permitted"] is False
    assert payload["contains_credentials"] is False
    assert payload["writes_production_database"] is False
    assert payload["actuals"][0]["is_final_station_actual"] is True
    assert payload["actuals"][0]["stored_metar_max_c"] == 28.0
    assert payload["actuals"][0]["market_resolution_actual"] is None
    assert payload["latest_hourly_model_forecasts"][0]["temp_850hpa_c"] == 16.5
    assert payload["latest_hourly_model_forecasts"][0]["radiation_wm2"] == 700.0
    assert payload["pipeline_health"]["expected_slots_full_day"] == 33
    assert payload["pipeline_health"]["trigger_counts"] == {"cloudflare": 1}
    checkpoint = payload["checkpoints"][0]
    assert checkpoint["forecast_chain_c"]["champion"] == 28.4
    assert checkpoint["model_counts"] == {
        "expected": 2,
        "available": 2,
        "usable": 2,
        "used": 2,
        "current_latest_run": 1,
        "awaiting_next_run": 1,
        "missing_expected_run": 0,
        "hard_stale": 0,
        "unclassified": 0,
    }
    assert "fresh" not in checkpoint["model_counts"]
    assert checkpoint["forecast_drivers"]["observed_heating_rate_cph"] == 1.1
    assert checkpoint["adjustment_impacts"]["persistent_hot_c"] == 0.2
    assert checkpoint["regime_flags"]["persistent_hot_active"] is True
    assert checkpoint["champion_and_challengers"][0]["bucket_probabilities"]["28"] == 0.6
    assert checkpoint["regime_memory"]["regimes"][0]["name"] == "persistent_hot"
    assert checkpoint["oos"] == {
        "causal": True,
        "standardized": True,
        "cohort": "fixed-checkpoint",
    }
    assert payload["manual_live_checkpoints"] == []
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


def test_manual_live_is_exported_as_additional_nonstandardized_oos() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    captured = datetime(2026, 8, 31, 13, 17, tzinfo=timezone.utc)
    with factory() as session:
        session.add(
            ForecastSnapshot(
                airport="LEMD",
                target_date=date(2026, 8, 31),
                captured_at=captured,
                timing="D0 live",
                raw_model_mean_c=33.0,
                bias_corrected_c=33.1,
                final_forecast_c=33.2,
                raw_spread_c=0.8,
                bias_corrected_spread_c=0.8,
                final_spread_c=0.9,
                day_phase="active",
                model_count=6,
                checkpoint_label="Manual Live",
                checkpoint_at=captured,
                checkpoint_recorded_at=captured,
                checkpoint_status="manual-causal-oos",
                evidence_class="complete",
                freshness_status="fresh",
            )
        )
        session.commit()
        payload = build_daily_analysis_export(
            session,
            generated_at=datetime(2026, 8, 31, 19, 15, tzinfo=timezone.utc),
            days=1,
        )

    assert payload["checkpoints"] == []
    assert len(payload["manual_live_checkpoints"]) == 1
    manual = payload["manual_live_checkpoints"][0]
    assert manual["checkpoint"] == "Manual Live"
    assert manual["oos"] == {
        "causal": True,
        "standardized": False,
        "cohort": "manual-live",
    }


def test_daily_analysis_export_recursively_sanitizes_embedded_json() -> None:
    payload = {
        "collector_coverage": [
            {
                "metrics": {
                    "diagnostic": (
                        "HTTP 429 for https://example.invalid/weather?"
                        "apikey=do-not-publish-this&format=json"
                    ),
                    "api_key": "also-do-not-publish-this",
                }
            }
        ]
    }

    sanitized = _sanitize_public_value(payload)
    serialized = json.dumps(sanitized)

    assert "do-not-publish-this" not in serialized
    assert "also-do-not-publish-this" not in serialized
    assert "api_key" not in serialized
    assert "apikey=REDACTED" in serialized
    assert_export_safe(sanitized)


def test_pipeline_health_normalizes_delayed_closeout_fallback() -> None:
    local_day = date(2026, 8, 31)
    regular = CollectionRun(
        run_id="regular-1907",
        scheduled_at=datetime(2026, 8, 31, 19, 7, tzinfo=timezone.utc),
        started_at=datetime(2026, 8, 31, 19, 8, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 31, 19, 9, tzinfo=timezone.utc),
        collector_version="1.0.5",
        trigger="cloudflare",
        overall_status="success",
    )
    closeout_fallback = CollectionRun(
        run_id="closeout-fallback-1922",
        # Older fallback collectors infer the preceding regular slot.
        scheduled_at=datetime(2026, 8, 31, 19, 7, tzinfo=timezone.utc),
        started_at=datetime(2026, 8, 31, 19, 22, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 31, 19, 24, tzinfo=timezone.utc),
        collector_version="1.0.5",
        trigger="closeout-fallback",
        overall_status="success",
    )

    health = _pipeline_health_payload(
        datetime(2026, 8, 31, 19, 30, tzinfo=timezone.utc),
        local_day,
        [regular, closeout_fallback],
    )

    assert health["closeout_success"] is True
    assert health["successful_expected_slots"] == 2
    closeout_match = next(
        item
        for item in health["normalized_slot_matches"]
        if item["trigger"] == "closeout-fallback"
    )
    assert closeout_match["expected_slot"] == "2026-08-31T19:15:00+00:00"
    assert closeout_match["reported_scheduled_at"] == "2026-08-31T19:07:00+00:00"
    assert closeout_match["offset_minutes"] == -8.0


def test_pipeline_health_does_not_count_arbitrary_manual_run_as_slot() -> None:
    manual = CollectionRun(
        run_id="manual-refresh",
        scheduled_at=datetime(2026, 8, 31, 19, 16, tzinfo=timezone.utc),
        started_at=datetime(2026, 8, 31, 19, 16, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 31, 19, 17, tzinfo=timezone.utc),
        collector_version="1.0.5",
        trigger="workflow_dispatch",
        overall_status="success",
    )

    health = _pipeline_health_payload(
        datetime(2026, 8, 31, 19, 30, tzinfo=timezone.utc),
        date(2026, 8, 31),
        [manual],
    )

    assert health["closeout_success"] is False
    assert health["processed_expected_slots"] == 0
    assert health["unmatched_runs"] == 1


def test_aemet_physical_export_is_separate_from_market_actual(monkeypatch) -> None:
    monkeypatch.setattr(
        export_module,
        "settings",
        SimpleNamespace(aemet_public_base_url="https://weather.example.workers.dev"),
    )
    monkeypatch.setattr(
        export_module,
        "fetch_public_aemet_json",
        lambda _base, _path: {
            "classification": "AEMET PHYSICAL OBSERVATIONS — NOT MARKET RESOLUTION",
            "station": {"id": "3129"},
            "local_date": "2026-09-02",
            "physical_tmax": {"value_c": 36.2},
            "observations": [],
        },
    )

    result = _aemet_physical_payload(
        date(2026, 9, 2),
        date(2026, 9, 2),
        {
            date(2026, 9, 2): [
                {"observed_at": "2026-09-02T16:00:00Z", "temp_c": 36.0}
            ]
        },
    )

    assert result["days"][0]["data"]["physical_tmax"]["value_c"] == 36.2
    assert result["market_resolution_actual"] is None
    assert result["metar_replacement"] is False
    diagnostics = result["days"][0]["data"]["research_diagnostics"]
    assert diagnostics["ground_truth"]["daily_max_series_gap_c"] == 0.2
    assert diagnostics["ground_truth"]["daily_max_series_gap_role"] == (
        "series_difference_not_sensor_bias"
    )
    assert diagnostics["physical_stall"]["probability"] is None
    assert diagnostics["metar_bucket_persistence"]["champion_impact_c"] == 0.0
