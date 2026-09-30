from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

import weatherman.forward_shadow as shadow


def _record(checkpoint: str = "D0 Morning @09:00", actual: float | None = None):
    return {
        "target_date": "2026-09-29",
        "checkpoint_label": checkpoint,
        "checkpoint_at": "2026-09-29T07:00:00+00:00",
        "generated_at": "2026-09-29T07:07:00+00:00",
        "evidence_class": "scheduled_causal",
        "forecast_snapshot_id": 42,
        "champion_center_c": 30.4,
        "champion_modal_bucket": 30,
        "champion_probabilities": {29: 0.1, 30: 0.55, 31: 0.35},
        "modal_bucket": 30,
        "top1_probability": 0.55,
        "top2_bucket": 31,
        "top2_probability": 0.35,
        "top3_bucket": 29,
        "top3_probability": 0.1,
        "top1_top2_gap_pp": 20.0,
        "distance_to_bucket_boundary_c": 0.1,
        "model_spread_c": 1.0,
        "raw_spread_c": 1.0,
        "model_history": {
            "consensus_movement": "stable",
            "consensus_run_trend_c": 0.0,
        },
        "consensus_run_trend_c": 0.0,
        "taf_bucket": 31,
        "taf_report_id": 7,
        "taf_issue_time": "2026-09-29T05:00:00+00:00",
        "taf_content_hash": "abc",
        "taf_agreement": "agreement",
        "taf_outside_top2": False,
        "taf_modal_bucket_flip": False,
        "stored_metar_actual_c": actual,
        "actual_bucket": int(actual) if actual is not None else None,
        "temp_anchor_adjustment_c": -0.2,
        "clear_sky_override_adjustment_c": 0.1,
        "correction_contributions": {
            "temp_anchor_adjustment_c": -0.2,
            "clear_sky_override_adjustment_c": 0.1,
            "taf_adjustment_c": 0.0,
            "late_dry_mixing_adjustment_c": 0.0,
            "cloud_adjustment_c": 0.0,
            "radiation_adjustment_c": 0.0,
            "heating_rate_adjustment_c": 0.0,
        },
        "features_json": {},
        "active_regimes": ["Negative Temperature Anchor", "Bucket Boundary"],
        "freshness_summary": {"status": "fresh", "used_models": 9},
    }


def test_d1_creates_both_frozen_versions():
    record = _record("D-1 Evening @20:00")
    candidates = shadow._candidate_decisions(record, [])
    decisions = [shadow._decision(record, candidate) for candidate in candidates]
    assert {row["challenger_version"] for row in decisions} == {
        "d1_evening_challenger_v0.1",
        "d1_evening_challenger_v0.2",
    }
    assert all(row["status"] == "FROZEN SHADOW" for row in decisions)
    assert all(row["decision_evidence_class"] == "live_shadow" for row in decisions)
    assert all(row["research_only"] and not row["automatic_promotion"] for row in decisions)


def test_first_live_is_champion_protected():
    record = _record("First Live @12:00")
    candidate = shadow._candidate_decisions(record, [])[0]
    decision = shadow._decision(record, candidate)
    assert decision["challenger_version"] == "first_live_champion_protected"
    assert decision["status"] == "champion_protected_no_active_forecast_challenger"
    assert decision["applied_delta_c"] == 0.0


def test_outcome_keeps_three_targets_separate():
    record = _record()
    candidate = shadow._candidate_decisions(record, [])[0]
    decision = shadow._decision(record, candidate)
    outcome = shadow._outcome(decision, 31.0)
    assert outcome["outcome_evidence_class"] == "sequential_oos"
    assert outcome["stored_metar_actual_bucket"] == 31
    assert outcome["aemet_tmax"] is None
    assert outcome["resolved_market_bucket"] is None
    assert outcome["champion"]["top2_hit"] is True


def test_existing_decision_hash_mismatch_stops_recalculation(monkeypatch):
    record = _record()
    candidate = shadow._candidate_decisions(record, [])[0]
    decision = shadow._decision(record, candidate)
    changed = deepcopy(decision)
    changed["decision_hash"] = "0" * 64
    existing = {
        "calibration_seed": [],
        "forward_records": [],
        "decisions": [changed],
    }
    monkeypatch.setattr(
        shadow,
        "_forward_records",
        lambda *_args, **_kwargs: ([record], {}),
    )
    with pytest.raises(RuntimeError, match="Immutable decision mismatch"):
        shadow.build_forward_shadow_journal(
            object(),
            existing=existing,
            start_date=date(2026, 9, 29),
            end_date=date(2026, 9, 29),
            generated_at=datetime(2026, 9, 29, 8, tzinfo=timezone.utc),
        )


def test_scorecard_contains_only_resolved_forward_cases():
    record = _record(actual=31.0)
    candidate = shadow._candidate_decisions(record, [])[0]
    resolved = shadow._decision(record, candidate)
    resolved["outcome"] = shadow._outcome(resolved, 31.0)
    pending = shadow._decision({**record, "target_date": "2026-09-30"}, candidate)
    scorecard = shadow._scorecard([resolved, pending])
    assert scorecard[0]["n"] == 1
    assert scorecard[0]["challenger_top2"] == 1.0


def test_forward_workflow_and_builder_are_read_only_and_bounded():
    workflow = open(
        ".github/workflows/publish-forward-shadow.yml", encoding="utf-8"
    ).read()
    builder = open(
        "scripts/build_forward_shadow_journal.py", encoding="utf-8"
    ).read()
    assert "name: 13 -" in workflow
    assert "publish-forward-shadow" in workflow
    assert "forward-shadow-journal.json" in workflow
    assert "SET TRANSACTION READ ONLY" in builder
    assert "session.rollback()" in builder
    assert "session.commit()" not in builder
    assert shadow.OPERATIONAL_LOOKBACK_DAYS == 2
    assert shadow.MAX_FORWARD_DAYS == 120
    assert shadow.MAX_MODEL_ROWS_PER_TARGET == 180


def test_forward_outcomes_never_refit_the_frozen_calibration(monkeypatch):
    first = _record(actual=31.0)
    second = {**_record(actual=30.0), "target_date": "2026-09-30"}
    history_sizes = []
    monkeypatch.setattr(
        shadow,
        "_forward_records",
        lambda *_args, **_kwargs: ([first, second], {}),
    )
    monkeypatch.setattr(
        shadow,
        "_candidate_decisions",
        lambda _record, history: history_sizes.append(len(history)) or [],
    )
    shadow.build_forward_shadow_journal(
        object(),
        existing={
            "calibration_seed": [{"target_date": "2026-09-28"}],
            "forward_records": [],
            "decisions": [],
        },
        start_date=date(2026, 9, 29),
        end_date=date(2026, 9, 30),
        generated_at=datetime(2026, 9, 30, 20, tzinfo=timezone.utc),
    )
    assert history_sizes == [1, 1]
