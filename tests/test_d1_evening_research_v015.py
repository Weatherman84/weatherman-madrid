from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

from weatherman.d1_evening_research import (
    D1_CHALLENGER_VERSION,
    build_d1_walk_forward_challenger,
    compare_d1_challenger,
    d1_reason_codes,
    shift_bucket_distribution,
)


def _record(day: date, *, actual: float = 31.0) -> dict:
    return {
        "target_date": day.isoformat(),
        "checkpoint_at": f"{day - timedelta(days=1)}T18:00:00+00:00",
        "checkpoint": "D-1 Evening @20:00",
        "checkpoint_status": "scheduled-causal",
        "checkpoint_reconstructed": False,
        "evidence_class": "scheduled_causal",
        "champion_center_c": 30.0,
        "champion_probabilities": {30: 0.6, 31: 0.4},
        "modal_bucket": 30,
        "top1_top2_gap_pp": 20.0,
        "top2_bucket": 31,
        "top3_bucket": 29,
        "taf_bucket": 31,
        "raw_spread_c": 0.8,
        "consensus_run_trend_c": 0.3,
        "stored_metar_actual_c": actual,
    }


def test_reason_codes_use_only_checkpoint_features():
    reasons = d1_reason_codes(_record(date(2026, 9, 1)))
    assert "taf_upside_signal" in reasons
    assert "warming_run_trend" in reasons
    assert "model_split_high" not in reasons


def test_fractional_distribution_shift_preserves_probability_mass():
    shifted = shift_bucket_distribution({30: 0.6, 31: 0.4}, 0.5)
    assert shifted == {30: 0.3, 31: 0.5, 32: 0.2}
    assert abs(sum(shifted.values()) - 1.0) < 1e-12


def test_walk_forward_waits_for_ten_prior_scheduled_cases():
    start = date(2026, 8, 1)
    records = [_record(start + timedelta(days=index)) for index in range(12)]
    scored = build_d1_walk_forward_challenger(records)
    assert all(
        row["challenger"]["center_adjustment_c"] == 0.0 for row in scored[:10]
    )
    assert scored[10]["challenger"]["center_adjustment_c"] == 0.5
    assert scored[10]["challenger"]["version"] == D1_CHALLENGER_VERSION
    assert scored[10]["challenger"]["research_only"] is True
    assert scored[10]["challenger"]["automatic_promotion"] is False


def test_future_actual_never_changes_an_earlier_challenger():
    start = date(2026, 8, 1)
    base = [_record(start + timedelta(days=index)) for index in range(11)]
    before = build_d1_walk_forward_challenger(deepcopy(base))[-1]["challenger"]
    after_records = deepcopy(base) + [_record(start + timedelta(days=11), actual=50.0)]
    after = build_d1_walk_forward_challenger(after_records)[10]["challenger"]
    assert before == after


def test_comparison_reports_forecast_and_probability_metrics_separately():
    start = date(2026, 8, 1)
    scored = build_d1_walk_forward_challenger([
        _record(start + timedelta(days=index)) for index in range(12)
    ])
    comparison = compare_d1_challenger(scored)
    assert comparison["champion"]["n"] == 12
    assert comparison["challenger"]["modal_accuracy"] > comparison["champion"]["modal_accuracy"]
    assert comparison["cases_improved"] == 2
    assert comparison["historical_replay_not_oos"] is True


def test_d1_workflow_is_unique_bounded_and_dry_run_first():
    workflow = open(
        ".github/workflows/export-d1-evening-research.yml", encoding="utf-8"
    ).read()
    script = open("scripts/export_d1_evening_research.py", encoding="utf-8").read()
    exporter = open("src/weatherman/d1_evening_export.py", encoding="utf-8").read()
    assert "name: 11 -" in workflow
    assert 'default: true' in workflow
    assert "SET TRANSACTION READ ONLY" in script
    assert 'transaction.rollback()' in script
    assert "automatic_backfill" in script
    assert ".limit(" in exporter
    assert "TafReport.raw_taf" not in exporter
    assert "HourlyForecast" not in exporter


def test_export_keeps_evidence_classes_and_targets_separate():
    from weatherman.d1_evening_export import build_d1_evening_export

    target = date(2026, 9, 20)
    captured = pd.Timestamp("2026-09-19T18:00:00Z")
    snapshot = pd.DataFrame([{
        "target_date": target,
        "captured_at": captured,
        "checkpoint_label": "D-1 Evening @20:00",
        "checkpoint_at": captured,
        "checkpoint_status": "scheduled-causal",
        "checkpoint_reconstructed": False,
        "evidence_class": "complete",
        "raw_model_mean_c": 30.0,
        "bias_corrected_c": 30.2,
        "final_forecast_c": 30.3,
        "raw_spread_c": 0.8,
        "taf_max_temp_c": 31.0,
        "used_models_json": '["ecmwf_ifs025"]',
        "features_json": "{}",
    }])
    champion = pd.DataFrame([{
        "target_date": target,
        "captured_at": captured,
        "forecast_c": 30.3,
        "spread_c": 0.8,
        "probabilities_json": '{"30":0.55,"31":0.45}',
        "forecast_confidence": 70,
    }])
    with (
        patch("weatherman.d1_evening_export._final_madrid_dates", return_value=[target]),
        patch("weatherman.d1_evening_export._checkpoint_rows", return_value=snapshot),
        patch("weatherman.d1_evening_export._champion_rows", return_value=champion),
        patch("weatherman.d1_evening_export._actuals", return_value={target: 31.0}),
        patch("weatherman.d1_evening_export._model_rows", return_value=pd.DataFrame()),
        patch("weatherman.d1_evening_export._taf_rows", return_value=pd.DataFrame()),
    ):
        payload = build_d1_evening_export(
            object(), days=1, generated_at=datetime(2026, 9, 21, tzinfo=timezone.utc)
        )
    record = payload["records"][0]
    assert payload["research_only"] is True
    assert payload["automatic_promotion"] is False
    assert payload["evidence_counts"]["scheduled_causal"] == 1
    assert record["stored_metar_actual_c"] == 31.0
    assert record["aemet_physical_tmax_c"] is None
    assert record["market_resolution_actual"] is None
    assert record["challenger"]["status"] == "warmup_insufficient_prior_cases"
    assert payload["regime_research"]["default_view"] == "scheduled_causal_only"
    assert payload["regime_research"]["scheduled_causal_only"]["checkpoint_records"] == 1
    assert payload["error_avoidance_analysis"][0]["uses_post_checkpoint_information"] is False
    assert "taf_upside_signal" in payload["error_avoidance_analysis"][0][
        "signals_that_warned_in_correct_direction"
    ]
