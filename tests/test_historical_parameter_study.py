import importlib.util
from pathlib import Path

from weatherman import checkpoint_challengers as existing


spec = importlib.util.spec_from_file_location(
    "historical_parameter_study",
    Path(__file__).resolve().parents[1] / "scripts/historical_parameter_study.py",
)
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


def record(**overrides):
    return {
        "target_date": "2026-08-11",
        "checkpoint_at": "2026-08-11T07:00:00+00:00",
        "checkpoint_label": existing.D0_CHECKPOINT,
        "evidence_class": "historical_partial_fidelity",
        "actual_complete": True,
        "actual_bucket": 30,
        "actual_available_at": "2026-08-11T22:10:00+00:00",
        "stored_metar_actual_c": 30.0,
        "champion_center_c": 30.2,
        "champion_modal_bucket": 30,
        "champion_probabilities": {"29": 0.2, "30": 0.6, "31": 0.2},
        "correction_contributions": {"temp_anchor_adjustment_c": 0.2},
        **overrides,
    }


def test_prior_labels_require_receipt_and_same_checkpoint_evidence():
    current = record(target_date="2026-08-12", checkpoint_at="2026-08-11T18:00:00Z")
    assert study.prior_cases([record()], current) == []
    current["checkpoint_at"] = "2026-08-12T07:00:00Z"
    good = record()
    assert study.prior_cases(
        [
            good,
            record(evidence_class="scheduled_causal"),
            record(actual_complete=False),
            record(checkpoint_label=existing.LATE_LIVE_CHECKPOINT),
            record(target_date="2026-08-12"),
        ],
        current,
    ) == [good]


def test_rolling_window_excludes_old_cases():
    current = record(target_date="2026-08-12", checkpoint_at="2026-08-12T07:00:00Z")
    old = record(target_date="2026-04-01", actual_available_at="2026-04-01T22:10:00Z")
    assert study.prior_cases([old], current) == [old]
    assert study.prior_cases([old], current, window=90) == []


def test_every_existing_scenario_is_invariant_to_current_actual():
    for checkpoint in [existing.D0_CHECKPOINT, existing.LATE_LIVE_CHECKPOINT]:
        first = record(checkpoint_label=checkpoint)
        second = record(checkpoint_label=checkpoint, actual_bucket=99, stored_metar_actual_c=99)
        for name, builder in study.policies(checkpoint).items():
            a, b = study.scenario(first, name, builder), study.scenario(second, name, builder)
            for field in ["champion_center_c", "champion_modal_bucket", "champion_probabilities"]:
                assert a[field] == b[field]


def test_public_trainer_keeps_partial_cases_out_of_training():
    rows = [record(target_date=f"2026-08-{day:02}") for day in range(1, 15)]
    outputs = existing.build_d0_walk_forward_challenger(rows)
    assert all(
        row["forecast_research_shadow"]["walk_forward_calibration"]["prior_n"] == 0
        for row in outputs
    )
