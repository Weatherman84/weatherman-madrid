from __future__ import annotations

from copy import deepcopy

import pytest

from weatherman.forward_shadow import (
    _shadow_engine_decision,
    _shadow_engine_outcome,
    _shadow_engine_scorecard,
    _validate_shadow_engine_decision,
)
from weatherman.shadow_engine_v1 import build_shadow_mirror, season_for_target_date


def record(
    checkpoint: str, *, target_date: str = "2026-10-10",
    anchor: float = -0.4, dry: float = 0.18,
):
    return {
        "target_date": target_date,
        "checkpoint_label": checkpoint,
        "checkpoint_at": f"{target_date}T14:00:00+00:00",
        "generated_at": f"{target_date}T14:07:00+00:00",
        "forecast_snapshot_id": 10,
        "evidence_class": "scheduled_causal",
        "champion_center_c": 24.38,
        "champion_probabilities": {23: 0.10, 24: 0.44, 25: 0.36, 26: 0.10},
        "champion_modal_bucket": 24,
        "model_spread_c": 1.1,
        "taf_bucket": 24,
        "taf_agreement": "agreement",
        "active_regimes": ["Stable / High Confidence", "Rapid Heat Ramp"],
        "correction_contributions": {
            "temp_anchor_adjustment_c": anchor,
            "late_dry_mixing_adjustment_c": dry,
            "clear_sky_override_adjustment_c": 0.15,
            "taf_adjustment_c": 0.1,
        },
        "market_checkpoint": {
            "status": "available",
            "market_snapshot_age_minutes": 4,
            "markets": [{
                "bucket_c": 25, "yes_price": 0.20, "best_bid": 0.19,
                "best_ask": 0.20, "spread": 0.01,
            }],
        },
    }


@pytest.mark.parametrize(
    ("target_date", "season"),
    [("2026-01-10", "Winter"), ("2026-04-10", "Spring"),
     ("2026-07-10", "Summer"), ("2026-10-10", "Autumn")],
)
def test_season_comes_from_target_date(target_date, season):
    assert season_for_target_date(target_date) == season


@pytest.mark.parametrize(
    ("target_date", "policy", "delta"),
    [("2026-01-10", "anchor_50pct", 0.2),
     ("2026-04-10", "current_protected_forecast", 0.0),
     ("2026-07-10", "anchor_25pct", 0.3),
     ("2026-10-10", "anchor_25pct", 0.3)],
)
def test_d0_uses_frozen_seasonal_mapping(target_date, policy, delta):
    mirror = build_shadow_mirror(record("D0 Morning @09:00", target_date=target_date))
    assert mirror["selected_policy"] == policy
    assert mirror["center_delta_c"] == pytest.approx(delta)
    assert mirror["candidate_policies"] == [
        "current_protected_forecast", "anchor_disabled", "anchor_25pct",
        "anchor_50pct", "anchor_75pct",
    ]
    assert mirror["research_only"] is True
    assert mirror["automatic_promotion"] is False


@pytest.mark.parametrize("target_date", ["2026-01-10", "2026-04-10", "2026-07-10", "2026-10-10"])
def test_late_uses_25pct_and_keeps_all_variants(target_date):
    mirror = build_shadow_mirror(record("Late Live @16:00", target_date=target_date))
    assert mirror["selected_policy"] == "late_dry_mixing_25pct"
    assert mirror["center_delta_c"] == pytest.approx(-0.135)
    assert len(mirror["candidate_variants"]) == 5


@pytest.mark.parametrize("checkpoint", ["D-1 Evening @20:00", "First Live @12:00"])
def test_protected_checkpoints_mirror_champion(checkpoint):
    mirror = build_shadow_mirror(record(checkpoint))
    assert mirror["selected_policy"] == "current_protected_forecast"
    assert mirror["center_delta_c"] == 0
    assert mirror["production_probabilities"] == mirror["shadow_probabilities"]


def test_winter_without_forward_trading_evidence_never_high_interest():
    source = record("D0 Morning @09:00", target_date="2026-01-10")
    source["champion_probabilities"] = {24: 0.20, 25: 0.65, 26: 0.15}
    mirror = build_shadow_mirror(source, market=source["market_checkpoint"])
    signal = mirror["trading_opportunity"]
    assert signal["classification"] != "HIGH INTEREST"
    assert signal["winter_watch_cap"] is True
    assert "winter_trading_roi_unavailable_caps_at_watch" in signal["reason_codes"]


def test_d0_temperature_plateau_is_no_trade():
    source = record("D0 Morning @09:00")
    source["active_regimes"] = ["Temperature Plateau"]
    mirror = build_shadow_mirror(source, market=source["market_checkpoint"])
    assert mirror["trading_opportunity"]["classification"] == "NO TRADE"
    assert "d0_temperature_plateau_robust_negative" in mirror["trading_opportunity"]["warning_codes"]


def test_shadow_engine_decision_is_immutable_and_stores_all_candidates():
    decision = _shadow_engine_decision(record("D0 Morning @09:00"))
    assert len(decision["candidate_variants"]) == 5
    assert decision["season"] == "Autumn"
    _validate_shadow_engine_decision(decision)
    changed = deepcopy(decision)
    changed["candidate_forecast_centers"]["anchor_25pct"] += 0.1
    with pytest.raises(RuntimeError, match="Immutable Shadow Engine"):
        _validate_shadow_engine_decision(changed)


def test_forward_scorecard_is_checkpoint_and_season_specific():
    decision = _shadow_engine_decision(record("D0 Morning @09:00"))
    decision["outcome"] = _shadow_engine_outcome(decision, 25.0)
    score = _shadow_engine_scorecard([decision])[0]
    assert score["checkpoint_label"] == "D0 Morning @09:00"
    assert score["season"] == "Autumn"
    assert score["oos_n"] == 1
    assert set(decision["outcome"]["candidate_outcomes"]) == set(decision["candidate_policies"])
