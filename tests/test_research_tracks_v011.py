from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

import pandas as pd

from weatherman.research_tracks import (
    active_regimes,
    build_trading_shadow_decision,
    checkpoint_evidence_class,
    late_live_overlap_guard_shadow,
    market_freshness,
    positive_temperature_bucket,
    regime_matrix_views,
    score_regime_matrix,
)


def _market(age=30):
    return {
        "status": "available",
        "market_captured_at": "2026-09-13T09:30:00+00:00",
        "market_snapshot_age_minutes": age,
        "markets": [
            {"bucket_c": 31, "best_bid": 0.18, "best_ask": 0.20, "spread": 0.02},
            {"bucket_c": 32, "best_bid": 0.48, "best_ask": 0.50, "spread": 0.02},
            {"bucket_c": 33, "best_bid": 0.28, "best_ask": 0.30, "spread": 0.02},
        ],
    }


def test_first_live_adds_external_taf_hedge_and_preserves_total_weight():
    decision = build_trading_shadow_decision(
        target_date="2026-09-13",
        checkpoint="First Live @12:00",
        generated_at="2026-09-13T10:01:00+00:00",
        probabilities={31: 0.55, 32: 0.35, 33: 0.10},
        taf_bucket=33,
        market_checkpoint=_market(),
    )
    weights = {row["bucket_c"]: row["weight"] for row in decision["selected_buckets"]}
    assert decision["research_only"] is True
    assert decision["automatic_promotion"] is False
    assert decision["taf_outside_top2"] is True
    assert weights[33] == 0.30
    assert round(sum(weights.values()), 8) == 1.0
    assert decision["strategy_code"] == "first_live_top2_plus_external_taf_hedge_30pct"


def test_late_live_never_adds_taf_hedge_and_early_checkpoints_observe_only():
    late = build_trading_shadow_decision(
        target_date="2026-09-13", checkpoint="Late Live @16:00",
        generated_at="2026-09-13T14:01:00+00:00",
        probabilities={31: 0.55, 32: 0.35, 33: 0.10}, taf_bucket=33,
        market_checkpoint=_market(),
    )
    assert {row["bucket_c"] for row in late["selected_buckets"]} == {31, 32}
    early = build_trading_shadow_decision(
        target_date="2026-09-13", checkpoint="D0 Morning @09:00",
        generated_at="2026-09-13T07:01:00+00:00",
        probabilities={31: 0.55, 32: 0.45}, taf_bucket=33,
        market_checkpoint=_market(),
    )
    assert early["status"] == "observe_only"
    assert early["selected_buckets"] == []


def test_market_freshness_bands_and_half_up_bucket_are_explicit():
    assert [market_freshness(value) for value in (60, 61, 180, 181, 360, 361)] == [
        "le_60m", "le_180m", "le_180m", "le_360m", "le_360m",
        "gt_360m_sensitivity_only",
    ]
    assert positive_temperature_bucket(32.5) == 33


def test_checkpoint_evidence_classes_keep_reconstruction_separate():
    assert checkpoint_evidence_class("scheduled-causal") == "scheduled_causal"
    assert checkpoint_evidence_class("reconstructed-causal") == "reconstructed_research"
    assert checkpoint_evidence_class("scheduled-causal", True) == "reconstructed_research"
    assert checkpoint_evidence_class("manual-causal-oos") == "other_research"


def test_late_live_overlap_guard_is_detection_only_without_champion_impact():
    result = late_live_overlap_guard_shadow({
        "checkpoint": "Late Live @16:00",
        "future_outlook_status": "MODEL PEAK PASSED",
        "remaining_model_rise_c": 0.1,
        "observed_max_c": 28.0,
        "taf_bucket": 28,
        "pre_taf_modal_bucket_c": 29,
        "clear_sky_override_adjustment_c": 0.30,
        "cloud_adjustment_c": 0.20,
        "radiation_adjustment_c": 0.148,
    })
    assert result["status"] == "candidate_detected"
    assert result["overlap_uplift_sum_c"] == 0.648
    assert result["counterfactual_cap_c"] is None
    assert result["upper_tail_damping"] is None
    assert result["champion_impact_c"] == 0.0
    assert result["research_only"] is True
    assert result["automatic_promotion"] is False


def test_regime_matrix_reports_checkpoint_and_global_without_combinations():
    record = {
        "checkpoint": "First Live @12:00", "champion_center_c": 32.2,
        "modal_bucket": 32, "top2_bucket": 33, "top3_bucket": 31,
        "top1_top2_gap_pp": 20.0, "raw_spread_c": 0.8, "taf_bucket": 33,
        "stored_metar_actual_c": 33.0, "rapid_heat_ramp_active": True,
        "features_json": '{"observed_heating_rate_60m_cph":1.0,"observed_dryness_c":12}',
    }
    record["active_regimes"] = active_regimes(record)
    matrix = score_regime_matrix([record])
    rapid = [row for row in matrix if row["regime"] == "Rapid Heat Ramp"]
    assert {row["checkpoint"] for row in rapid} == {"ALL", "First Live @12:00"}
    assert all(row["n"] == 1 and row["small_sample"] for row in rapid)
    assert rapid[0]["top2_coverage"] == 1.0


def test_regime_matrix_views_default_to_scheduled_and_do_not_mix_reconstruction():
    base = {
        "checkpoint": "First Live @12:00", "champion_center_c": 32.2,
        "modal_bucket": 32, "top2_bucket": 33, "top3_bucket": 31,
        "top1_top2_gap_pp": 20.0, "raw_spread_c": 0.8, "taf_bucket": 33,
        "stored_metar_actual_c": 33.0, "active_regimes": ["TAF Agreement"],
    }
    views = regime_matrix_views([
        {**base, "evidence_class": "scheduled_causal"},
        {**base, "evidence_class": "reconstructed_research"},
        {**base, "evidence_class": "other_research"},
    ])
    assert views["default_view"] == "scheduled_causal_only"
    assert views["scheduled_causal_only"]["checkpoint_records"] == 1
    assert views["reconstructed_research"]["checkpoint_records"] == 1
    assert views["all_research_evidence"]["checkpoint_records"] == 3
    assert views["scheduled_causal_only"]["matrix"][0]["n"] == 1
    assert views["all_research_evidence"]["matrix"][0]["n"] == 3


def test_trading_decision_carries_checkpoint_status_and_evidence_class():
    decision = build_trading_shadow_decision(
        target_date="2026-09-13", checkpoint="First Live @12:00",
        generated_at="2026-09-13T10:01:00+00:00",
        probabilities={31: 0.55, 32: 0.45}, taf_bucket=33,
        market_checkpoint=_market(), checkpoint_status="reconstructed-causal",
        checkpoint_reconstructed=True,
    )
    assert decision["checkpoint_status"] == "reconstructed-causal"
    assert decision["checkpoint_reconstructed"] is True
    assert decision["evidence_class"] == "reconstructed_research"
    explicit = build_trading_shadow_decision(
        target_date="2026-09-13", checkpoint="First Live @12:00",
        generated_at="2026-09-13T10:01:00+00:00",
        probabilities={31: 0.55, 32: 0.45}, taf_bucket=33,
        market_checkpoint=_market(), checkpoint_status="reconstructed-causal",
        checkpoint_reconstructed=True, evidence_class="sequential_live_shadow",
    )
    assert explicit["checkpoint_reconstructed"] is True
    assert explicit["evidence_class"] == "reconstructed_research"


def test_research_export_separates_scheduled_and_reconstructed_matrices():
    from weatherman.research_replay_export import build_research_replay_export

    target = date(2026, 9, 15)
    captured = pd.Timestamp("2026-09-15T10:00:00Z")
    snapshots = pd.DataFrame([
        {
            "target_date": target, "captured_at": captured,
            "checkpoint_label": "First Live @12:00",
            "checkpoint_at": captured, "checkpoint_status": "scheduled-causal",
            "checkpoint_reconstructed": False, "evidence_class": "complete",
            "taf_max_temp_c": 36.0, "raw_spread_c": 0.8,
        },
        {
            "target_date": target, "captured_at": captured + pd.Timedelta(hours=4),
            "checkpoint_label": "Late Live @16:00",
            "checkpoint_at": captured + pd.Timedelta(hours=4),
            "checkpoint_status": "reconstructed-causal",
            "checkpoint_reconstructed": True, "evidence_class": "complete",
            "taf_max_temp_c": 36.0, "raw_spread_c": 0.8,
        },
    ])
    champions = pd.DataFrame([
        {
            "target_date": target, "captured_at": captured, "forecast_c": 36.0,
            "spread_c": 0.8, "probabilities_json": '{"36":0.6,"37":0.4}',
            "forecast_confidence": 80.0,
        },
        {
            "target_date": target, "captured_at": captured + pd.Timedelta(hours=4),
            "forecast_c": 36.0, "spread_c": 0.8,
            "probabilities_json": '{"36":0.6,"37":0.4}',
            "forecast_confidence": 80.0,
        },
    ])
    market = {
        "checkpoints": [
            {"target_date": target.isoformat(), "checkpoint_label": label,
             "status": "unavailable", "markets": []}
            for label in ("First Live @12:00", "Late Live @16:00")
        ]
    }
    with (
        patch("weatherman.research_replay_export._final_madrid_dates", return_value=[target]),
        patch("weatherman.research_replay_export._checkpoint_rows", return_value=snapshots),
        patch("weatherman.research_replay_export._champion_rows", return_value=champions),
        patch("weatherman.research_replay_export._actual_map", return_value={target: 36.0}),
        patch("weatherman.research_replay_export._resolution_map", return_value={}),
        patch("weatherman.research_replay_export.build_market_replay_export", return_value=market),
    ):
        payload = build_research_replay_export(
            object(), days=1, generated_at=datetime(2026, 9, 16, tzinfo=timezone.utc)
        )

    decisions = payload["trading_challenger"]["decisions"]
    assert [row["evidence_class"] for row in decisions] == [
        "scheduled_causal", "reconstructed_research"
    ]
    assert decisions[1]["checkpoint_status"] == "reconstructed-causal"
    views = payload["regime_research"]["matrix_views"]
    assert payload["regime_research"]["matrix_default_view"] == "scheduled_causal_only"
    assert views["scheduled_causal_only"]["checkpoint_records"] == 1
    assert views["reconstructed_research"]["checkpoint_records"] == 1
    assert views["all_research_evidence"]["checkpoint_records"] == 2


def test_workflows_are_uniquely_numbered_and_research_isolated():
    market = open(".github/workflows/export-market-replay.yml", encoding="utf-8").read()
    research = open(".github/workflows/export-research-tracks.yml", encoding="utf-8").read()
    shadow = open(".github/workflows/capture-trading-shadow.yml", encoding="utf-8").read()
    prepare = open("scripts/prepare_replay_lab.py", encoding="utf-8").read()
    capture = open("scripts/capture_trading_shadow.py", encoding="utf-8").read()
    assert "name: 8 -" in market
    assert "name: 9 -" in research
    assert "name: 10 -" in shadow
    assert "REPLAY_DATABASE_URL" in shadow
    assert "SET TRANSACTION READ ONLY" in capture
    assert "COUNT(*) FROM forecasts" not in prepare
    assert "automatic_promotion BOOLEAN NOT NULL DEFAULT FALSE" in prepare
    assert "ADD COLUMN IF NOT EXISTS checkpoint_status" in prepare
