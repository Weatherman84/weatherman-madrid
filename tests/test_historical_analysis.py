"""Research isolation, time ordering, and hypothetical payout accounting."""

import copy

import pytest

from weatherman import d1_evening_research as d1
from weatherman.historical_analysis import (
    CHECKPOINTS,
    SCENARIOS,
    SOURCE_EVIDENCE,
    available_prior,
    d1_prediction,
    no_parameter_effect,
    prediction,
    sample_class,
    valid,
)
from weatherman.historical_analysis_reports import (
    d1_prediction as cached_d1_prediction,
    execute_price_trade,
    market_view,
    parameter_and_walk_forward,
    policy_matrix,
    strategy_legs,
    trading_metrics,
)


def row(day="2026-06-20"):
    return prediction(
        {
            "target_date": day,
            "checkpoint_label": "First Live @12:00",
            "checkpoint_at": day + "T10:00:00+00:00",
            "evidence_class": SOURCE_EVIDENCE,
            "status": "diagnostic_partial_replay",
            "stored_metar_actual_c": 30.0,
            "actual_bucket": 30,
            "actual_complete": True,
            "actual_available_at": day + "T23:00:00+00:00",
        },
        30.0,
        {30: 0.6, 31: 0.3, 32: 0.1},
    )


def market(bucket, price=0.3, winner=False, at="2026-06-20T09:55:00+00:00"):
    return {
        "token_id": str(bucket),
        "bucket_label": str(bucket) + "°C",
        "resolution_status": "resolved",
        "resolved_yes": winner,
        "historical_point": {"series_price": price, "price_at": at},
    }


def test_training_respects_actual_receipt_and_complete_actuals():
    current = row()
    older = row("2026-06-18")
    late = row("2026-06-19")
    late["actual_available_at"] = "2026-06-20T12:00:00+00:00"
    partial = row("2026-06-17")
    partial["actual_complete"] = False
    future = row("2026-06-21")
    other_cohort = row("2026-06-16")
    other_cohort["evidence_class"] = "scheduled_causal"
    assert available_prior([older, late, partial, future, other_cohort, current], current) == [
        older
    ]
    assert not available_prior([older], current, window=1)
    assert not valid(partial)


def test_diagnostic_training_adapter_keeps_public_guard_and_current_actual_out():
    current = row()
    current.update(modal_bucket=30, reason_codes=[], signed_error_c=0.2)
    prior = [
        {**row(f"2026-06-{day:02d}"), "signed_error_c": 0.2, "reason_codes": []}
        for day in range(1, 16)
    ]
    protected_constant = d1.SCHEDULED_CAUSAL_EVIDENCE
    assert d1._calibration_adjustment(prior, [])[0] == 0
    result = d1_prediction(current, prior, {"version": 1})
    assert result["champion_center_c"] == pytest.approx(30.2)
    assert result["evidence_class"] == SOURCE_EVIDENCE
    changed = {**current, "actual_bucket": 40, "stored_metar_actual_c": 40.0, "signed_error_c": 10}
    other = d1_prediction(changed, prior, {"version": 1})
    assert other["champion_probabilities"] == result["champion_probabilities"]
    assert other["champion_center_c"] == result["champion_center_c"]
    assert d1.SCHEDULED_CAUSAL_EVIDENCE == protected_constant
    assert result["public_scheduled_causal_training_n"] == 0


def test_zero_effect_requires_raw_contribution_not_guarded_contribution():
    assert no_parameter_effect({"taf": 0}, {"taf": 0})
    assert not no_parameter_effect({"taf": 0}, {"taf": 0.1})
    assert not no_parameter_effect({"temperature_anchor": None}, {"temperature_anchor": 0.2})
    assert no_parameter_effect({"temperature_anchor": None}, {"temperature_anchor": 0.05})
    assert not no_parameter_effect({"unknown_factor": 0}, {})


def test_reason_memoization_matches_existing_d1_formula():
    current = row()
    current.update(
        modal_bucket=30,
        taf_bucket=31,
        consensus_run_trend_c=0.3,
        raw_spread_c=2.5,
        temp_anchor_adjustment_c=-0.2,
        clear_sky_override_adjustment_c=0.1,
        signed_error_c=0.2,
    )
    prior = [
        {**current, "target_date": f"2026-06-{day:02d}", "signed_error_c": -0.3}
        for day in range(1, 16)
    ]
    for params in [
        {"version": 1},
        {"version": 2},
        {"version": 2, "directional_guard": False},
        {"version": 2, "boundary_threshold": 0.2, "bias_strength": 0.5},
    ]:
        assert cached_d1_prediction(current, prior, params) == d1_prediction(current, prior, params)


def test_future_price_is_rejected_and_range_probability_is_summed():
    r = row()
    raw = [market(30, winner=True, at="2026-06-20T10:01:00+00:00"), market(31)]
    raw[1]["bucket_label"] = "31°C or higher"
    view = market_view(r, raw)
    assert view[0]["price"] is None
    assert view[1]["probability"] == pytest.approx(0.4)
    assert all(m["executable_quote"] is False for m in view)


def test_one_unit_basket_payout_and_missing_leg_are_not_renormalized():
    r = row()
    view = market_view(r, [market(30, 0.6, True), market(31, 0.3), market(32, 0.1)])
    legs = strategy_legs(r, view, "B_champion_top1_top2")
    trade = execute_price_trade(r, legs, 10)
    assert sum(leg["stake_weight"] for leg in trade["legs"]) == pytest.approx(1)
    assert trade["pnl"] == pytest.approx(1 / 0.9 - 1)
    missing = copy.deepcopy(legs)
    missing[1][0]["price"] = None
    assert execute_price_trade(r, missing, 30) is None
    stale = copy.deepcopy(legs)
    stale[1][0]["age_minutes"] = 31
    assert execute_price_trade(r, stale, 30) is None
    assert execute_price_trade(r, stale, 60) is not None


def test_strategy_selection_does_not_use_resolution():
    r = row()
    view = market_view(r, [market(30, 0.4, True), market(31, 0.2), market(32, 0.1)])
    other = copy.deepcopy(view)
    for m in other:
        m["resolved_yes"] = not m["resolved_yes"]
    selected = strategy_legs(r, view, "F_positive_ev_only")
    changed = strategy_legs(r, other, "F_positive_ev_only")
    assert [(m["token_id"], w) for m, w in selected] == [(m["token_id"], w) for m, w in changed]


def test_existing_taf_hedge_allocates_requested_fraction():
    r = row()
    r["taf_bucket"] = 32
    view = market_view(r, [market(30, 0.4), market(31, 0.2), market(32, 0.1, True)])
    for value in [10, 20, 30]:
        legs = strategy_legs(r, view, f"D_first_live_taf_hedge_{value}")
        assert next(w for m, w in legs if m["token_id"] == "32") == pytest.approx(value / 100)
        assert sum(w for _, w in legs) == pytest.approx(1)


def test_drawdown_and_losing_streak_use_pnl_not_winning_bucket():
    template = execute_price_trade(
        row(),
        strategy_legs(row(), market_view(row(), [market(30, 0.5, True)]), "A_champion_top1"),
        30,
    )
    trades = [
        {**template, "target_date": f"2026-06-{i:02d}", "pnl": pnl, "return": pnl}
        for i, pnl in enumerate([2, -1, -0.5, 1, -2], start=1)
    ]
    result = trading_metrics(trades)
    assert result["hypothetical_pnl"] == pytest.approx(-0.5)
    assert result["max_drawdown"] == pytest.approx(2.5)
    assert result["longest_losing_streak"] == 2
    assert result["positive_trade_pct"] == 40
    assert trading_metrics([])["roi"] is None


def test_tournament_output_preserves_policy_identifier_and_metrics(tmp_path):
    groups = {
        s: {
            cp: [
                {**row("2026-06-18"), "checkpoint_label": cp},
                {**row("2026-06-19"), "checkpoint_label": cp},
            ]
            for cp in CHECKPOINTS
        }
        for s in SCENARIOS
    }
    specs = {
        cp: [
            {
                "id": "current_protected_forecast",
                "selection_eligible": True,
                "kind": "engine",
                "parameters": {},
                "free_parameters": 0,
            }
        ]
        for cp in CHECKPOINTS
    }
    forecasts = {
        (s, cp): {"current_protected_forecast": rows}
        for s, checkpoint_rows in groups.items()
        for cp, rows in checkpoint_rows.items()
    }
    entries, _ = policy_matrix(tmp_path, groups, forecasts, specs)
    assert len(entries) == 24
    assert all(
        e["policy_id"] == "current_protected_forecast" and e["policy"]["n"] == 2 for e in entries
    )
    studies, _, walk, _ = parameter_and_walk_forward(tmp_path, groups, forecasts, specs)
    assert all(s["policy_id"] == "current_protected_forecast" for s in studies)
    selected = [w for w in walk if w["policy_id"] == "existing_parameter_selector"]
    assert len(selected) == 8
    assert all(w["policy"]["n"] == 2 and w["matched_n"] == 2 for w in selected)


@pytest.mark.parametrize(
    "n,label",
    [
        (9, "exploratory_only"),
        (10, "weak_evidence"),
        (29, "weak_evidence"),
        (30, "moderate_evidence"),
        (59, "moderate_evidence"),
        (60, "stronger_diagnostic_evidence"),
    ],
)
def test_sample_size_classes(n, label):
    assert sample_class(n) == label
