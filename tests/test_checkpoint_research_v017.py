from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

from weatherman.checkpoint_research_export import build_checkpoint_research_export
from weatherman.d1_evening_research import (
    D1_CHALLENGER_V2_VERSION,
    build_d1_walk_forward_challenger_v2,
    compare_d1_challenger_versions,
)


def _d1(day: date, *, taf: int, actual: float = 30.0) -> dict:
    return {
        "target_date": day.isoformat(),
        "checkpoint_at": f"{day - timedelta(days=1)}T18:00:00+00:00",
        "checkpoint": "D-1 Evening @20:00",
        "checkpoint_status": "scheduled-causal",
        "checkpoint_reconstructed": False,
        "evidence_class": "scheduled_causal",
        "champion_center_c": 30.4,
        "champion_probabilities": {30: 0.55, 31: 0.45},
        "modal_bucket": 30,
        "top1_top2_gap_pp": 10.0,
        "top2_bucket": 31,
        "top3_bucket": 29,
        "taf_bucket": taf,
        "raw_spread_c": 0.8,
        "stored_metar_actual_c": actual,
        "active_regimes": ["Bucket Boundary"],
    }


def test_v02_directional_guard_blocks_bias_against_taf_signal():
    start = date(2026, 8, 1)
    prior = [_d1(start + timedelta(days=index), taf=30, actual=29.0) for index in range(10)]
    current = _d1(start + timedelta(days=10), taf=31, actual=31.0)
    scored = build_d1_walk_forward_challenger_v2(prior + [current])
    candidate = scored[-1]["challenger_v0_2"]
    assert candidate["version"] == D1_CHALLENGER_V2_VERSION
    assert candidate["center_adjustment_c"] == 0.0
    assert candidate["calibration"]["directional_guard"] == (
        "blocked_downward_bias_against_taf_upside"
    )
    assert candidate["research_only"] is True
    assert candidate["automatic_promotion"] is False


def test_v02_comparison_preserves_v01_and_lists_changed_cases():
    start = date(2026, 8, 1)
    rows = [_d1(start + timedelta(days=index), taf=31, actual=31.0) for index in range(12)]
    scored = build_d1_walk_forward_challenger_v2(rows)
    comparison = compare_d1_challenger_versions(scored)
    assert comparison["champion"]["n"] == 12
    assert comparison["d1_evening_challenger_v0_1"]["n"] == 12
    assert comparison["d1_evening_challenger_v0_2"]["n"] == 12
    assert comparison["historical_replay_not_oos"] is True


def test_checkpoint_export_is_bounded_causal_and_counterfactual_ready():
    target = date(2026, 9, 26)
    captured = pd.Timestamp("2026-09-26T10:00:00Z")
    snapshot = pd.DataFrame([{
        "id": 123,
        "target_date": target,
        "captured_at": captured,
        "checkpoint_label": "First Live @12:00",
        "checkpoint_at": captured,
        "checkpoint_status": "scheduled-causal",
        "checkpoint_reconstructed": False,
        "evidence_class": "complete",
        "raw_model_mean_c": 33.0,
        "weighted_raw_c": 33.1,
        "bias_corrected_c": 33.2,
        "metar_conditioned_c": 33.6,
        "final_forecast_c": 33.7,
        "raw_spread_c": 0.9,
        "final_spread_c": 0.8,
        "model_count": 9,
        "taf_max_temp_c": 34.0,
        "taf_adjustment_c": 0.1,
        "live_adjustment_c": 0.4,
        "temp_anchor_adjustment_c": 0.2,
        "rapid_heat_ramp_active": True,
        "features_json": '{"observed_heating_rate_60m_cph":3.0,"rapid_heat_ramp_strength":0.8}',
        "used_models_json": '["ecmwf_ifs025"]',
    }])
    champion = pd.DataFrame([{
        "target_date": target,
        "captured_at": captured,
        "forecast_c": 33.7,
        "spread_c": 0.8,
        "probabilities_json": '{"33":0.25,"34":0.55,"35":0.20}',
        "forecast_confidence": 75,
    }])
    with (
        patch("weatherman.checkpoint_research_export._final_madrid_dates", return_value=[target]),
        patch("weatherman.checkpoint_research_export._checkpoint_rows", return_value=snapshot),
        patch("weatherman.checkpoint_research_export._champion_rows", return_value=champion),
        patch("weatherman.checkpoint_research_export._actuals", return_value={target: 34.0}),
        patch("weatherman.checkpoint_research_export._model_rows", return_value=pd.DataFrame()),
        patch("weatherman.checkpoint_research_export._taf_rows", return_value=pd.DataFrame()),
    ):
        payload = build_checkpoint_research_export(
            object(), days=1, generated_at=datetime(2026, 9, 27, tzinfo=timezone.utc)
        )
    record = payload["records"][0]
    assert payload["research_only"] is True
    assert payload["automatic_promotion"] is False
    assert payload["default_analysis_view"] == "scheduled_causal_only"
    assert record["evidence_class"] == "scheduled_causal"
    assert record["forecast_snapshot_id"] == 123
    assert record["actual_bucket"] == 34
    assert record["regime_raw_inputs"]["rapid_heat_ramp_strength"] == 0.8
    assert "temp_anchor_adjustment_c" in record["correction_contributions"]
    assert record["probability_window"]["mapping_metadata"].startswith("persisted")
    assert payload["transfer_policy"]["full_table_scans"] is False
    assert payload["transfer_policy"]["automatic_backfill"] is False


def test_checkpoint_workflow_defaults_to_zero_query_dry_run():
    workflow = open(
        ".github/workflows/export-checkpoint-research.yml", encoding="utf-8"
    ).read()
    script = open("scripts/export_checkpoint_research.py", encoding="utf-8").read()
    exporter = open(
        "src/weatherman/checkpoint_research_export.py", encoding="utf-8"
    ).read()
    assert "name: 12 -" in workflow
    assert "default: true" in workflow
    assert "SET TRANSACTION READ ONLY" in script
    assert "transaction.rollback()" in script
    assert '"production_queries_executed": 0' in script
    assert ".limit(" in exporter
    assert "raw_model_grids_exported" in exporter
