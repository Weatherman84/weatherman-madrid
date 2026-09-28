from __future__ import annotations

from datetime import date, timedelta

from weatherman.checkpoint_challengers import (
    D0_CHALLENGER_VERSION,
    LATE_LIVE_CHALLENGER_VERSION,
    apply_checkpoint_research_challengers,
    build_d0_walk_forward_challenger,
    build_late_live_walk_forward_challenger,
)


def _row(
    day: date,
    checkpoint: str,
    *,
    center: float = 30.2,
    actual: float = 31.0,
    anchor: float = -0.2,
    clear: float = 0.2,
    taf_adjustment: float = 0.1,
    dry: float = 0.1,
) -> dict:
    return {
        "target_date": day.isoformat(),
        "checkpoint_label": checkpoint,
        "checkpoint_at": f"{day.isoformat()}T10:00:00+00:00",
        "evidence_class": "scheduled_causal",
        "champion_center_c": center,
        "champion_modal_bucket": 30,
        "champion_probabilities": {29: 0.1, 30: 0.55, 31: 0.35},
        "probability_window": {"buckets": {29: 0.1, 30: 0.55, 31: 0.35}},
        "top1_bucket": 30,
        "top2_bucket": 31,
        "top3_bucket": 29,
        "taf_bucket": 31,
        "taf_agreement": "agreement",
        "taf_outside_top2": False,
        "taf_modal_bucket_flip": False,
        "active_regimes": ["Negative Temperature Anchor"],
        "model_spread_c": 0.8,
        "model_history": {"consensus_movement": "stable"},
        "correction_contributions": {
            "temp_anchor_adjustment_c": anchor,
            "clear_sky_override_adjustment_c": clear,
            "taf_adjustment_c": taf_adjustment,
            "late_dry_mixing_adjustment_c": dry,
            "cloud_adjustment_c": 0.1,
            "radiation_adjustment_c": 0.1,
            "heating_rate_adjustment_c": 0.1,
        },
        "stored_metar_actual_c": actual,
        "actual_bucket": int(actual),
    }


def test_d0_walk_forward_uses_only_prior_scheduled_causal_cases():
    start = date(2026, 8, 1)
    rows = [
        _row(start + timedelta(days=index), "D0 Morning @09:00")
        for index in range(12)
    ]
    scored = build_d0_walk_forward_challenger(rows)
    assert scored[9]["forecast_research_shadow"]["selected_policy"] == (
        "current_anchor_100pct"
    )
    candidate = scored[10]["forecast_research_shadow"]
    assert candidate["challenger_version"] == D0_CHALLENGER_VERSION
    assert candidate["prior_scheduled_causal_n"] == 10
    assert candidate["research_only"] is True
    assert candidate["automatic_promotion"] is False


def test_future_actual_never_changes_earlier_d0_shadow():
    start = date(2026, 8, 1)
    rows = [
        _row(start + timedelta(days=index), "D0 Morning @09:00")
        for index in range(11)
    ]
    before = build_d0_walk_forward_challenger(rows)[-1]["forecast_research_shadow"]
    rows.append(
        _row(start + timedelta(days=11), "D0 Morning @09:00", actual=20.0)
    )
    after = build_d0_walk_forward_challenger(rows)[10]["forecast_research_shadow"]
    assert before["selected_policy"] == after["selected_policy"]
    assert before["challenger_center_c"] == after["challenger_center_c"]


def test_late_live_active_selection_is_single_factor_only():
    start = date(2026, 8, 1)
    rows = [
        _row(
            start + timedelta(days=index),
            "Late Live @16:00",
            center=31.0,
            actual=30.0,
        )
        for index in range(12)
    ]
    scored = build_late_live_walk_forward_challenger(rows)
    candidate = scored[-1]["forecast_research_shadow"]
    assert candidate["challenger_version"] == LATE_LIVE_CHALLENGER_VERSION
    assert candidate["selection_policy"] == "current_or_single_factor_ablation_only"
    changed = [
        value != "100" for value in candidate["selected_policy"].split("_")[1::2]
    ]
    assert sum(changed) <= 1


def test_first_live_is_explicitly_protected_and_reconstructed_is_excluded():
    day = date(2026, 9, 1)
    first = _row(day, "First Live @12:00")
    reconstructed = _row(day, "D0 Morning @09:00")
    reconstructed["evidence_class"] = "reconstructed_research"
    records, evaluation = apply_checkpoint_research_challengers([first, reconstructed])
    by_checkpoint = {row["checkpoint_label"]: row for row in records}
    assert by_checkpoint["First Live @12:00"]["forecast_research_shadow"]["status"] == (
        "champion_protected_no_active_forecast_challenger"
    )
    assert "forecast_research_shadow" not in by_checkpoint["D0 Morning @09:00"]
    assert evaluation["methodology"]["reconstructed_research_used_for_selection"] is False
