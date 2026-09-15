from __future__ import annotations

from weatherman.research_tracks import (
    active_regimes,
    build_trading_shadow_decision,
    market_freshness,
    positive_temperature_bucket,
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
