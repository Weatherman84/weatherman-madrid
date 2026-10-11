from __future__ import annotations

from copy import deepcopy

import pytest

from weatherman.forward_shadow import (
    _shadow_engine_decision, _validate_shadow_engine_decision,
)
from weatherman.shadow_engine_v1 import build_shadow_mirror


def record(checkpoint: str, *, anchor: float = -0.4, dry: float = 0.18):
    return {
        "target_date": "2026-10-10",
        "checkpoint_label": checkpoint,
        "checkpoint_at": "2026-10-10T14:00:00+00:00",
        "generated_at": "2026-10-10T14:07:00+00:00",
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
                "bucket_c": 25, "yes_price": 0.37, "best_bid": 0.36,
                "best_ask": 0.37, "spread": 0.01,
            }],
        },
    }


def test_d0_uses_fixed_anchor_25pct_policy():
    mirror = build_shadow_mirror(record("D0 Morning @09:00"))
    assert mirror["selected_policy"] == "anchor_25pct"
    assert mirror["center_delta_c"] == pytest.approx(0.3)
    assert mirror["shadow_center_c"] == pytest.approx(24.68)
    assert mirror["research_only"] is True
    assert mirror["automatic_promotion"] is False


def test_late_uses_fixed_dry_mixing_zero_policy():
    mirror = build_shadow_mirror(record("Late Live @16:00"))
    assert mirror["selected_policy"] == "late_dry_mixing_0pct"
    assert mirror["center_delta_c"] == pytest.approx(-0.18)
    assert mirror["shadow_center_c"] == pytest.approx(24.20)


@pytest.mark.parametrize(
    "checkpoint",
    ["D-1 Evening @20:00", "First Live @12:00"],
)
def test_protected_checkpoints_mirror_champion(checkpoint):
    mirror = build_shadow_mirror(record(checkpoint))
    assert mirror["selected_policy"] == "current_protected_forecast"
    assert mirror["center_delta_c"] == 0
    assert mirror["production_probabilities"] == mirror["shadow_probabilities"]


def test_missing_drawdown_caps_high_interest_candidate_at_watch():
    source = record("D0 Morning @09:00", anchor=-0.4)
    source["champion_probabilities"] = {24: 0.30, 25: 0.55, 26: 0.15}
    mirror = build_shadow_mirror(source, market=source["market_checkpoint"])
    signal = mirror["trading_opportunity"]
    assert signal["classification"] == "WATCH"
    assert "high_interest_candidate_but_drawdown_unavailable" in signal["reason_codes"]
    assert signal["automatic_execution"] is False


def test_shadow_engine_decision_is_immutable_and_hash_protected():
    decision = _shadow_engine_decision(record("D0 Morning @09:00"))
    _validate_shadow_engine_decision(decision)
    changed = deepcopy(decision)
    changed["shadow_center_c"] += 0.1
    with pytest.raises(RuntimeError, match="Immutable Shadow Engine"):
        _validate_shadow_engine_decision(changed)

