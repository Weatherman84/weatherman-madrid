from __future__ import annotations

from copy import deepcopy

import pytest

from weatherman.seasonal_timing_research import (
    build_research_report,
    load_handoff_registry,
)


def _variant(name: str, factor: str | None, center: float, probabilities: dict[str, float]):
    return {
        "variant": name,
        "factor": factor,
        "forecast_c": center,
        "bucket_probabilities": probabilities,
    }


def _checkpoint(
    target: str,
    label: str,
    captured: str,
    peak: str,
    *,
    post_peak: bool = False,
):
    variants = [
        _variant("Champion", None, 20.8, {"20": 0.3, "21": 0.6, "22": 0.1}),
        _variant("Analog", "regime_memory_analog", 20.2, {"20": 0.6, "21": 0.3, "22": 0.1}),
    ]
    if post_peak:
        variants.append(
            _variant("Post-Peak", "post_peak_upper_tail", 20.1, {"20": 0.7, "21": 0.2, "22": 0.1})
        )
    return {
        "target_date": target,
        "checkpoint": label,
        "captured_at": captured,
        "expected_peak_at": peak,
        "checkpoint_status": "scheduled-causal",
        "evidence_class": "complete",
        "checkpoint_reconstructed": False,
        "freshness_status": "fresh",
        "observed_max_c": 20.0,
        "forecast_drivers": {
            "observed_heating_rate_30m_cph": 0.0,
            "observed_heating_rate_60m_cph": 0.0,
            "observed_heating_rate_120m_cph": 0.5,
            "observed_cloud_cover_pct": 10.0,
            "observed_dryness_c": 8.0,
        },
        "adjustment_impacts": {"temperature_anchor_c": 0.4, "wind_c": 0.1},
        "champion_and_challengers": variants,
    }


def _export() -> dict:
    target = "2026-11-10"
    conditions = [
        {"condition": "oos_days", "actual": 75, "threshold": 30, "status": "PASS"},
        {"condition": "mae_gain_c", "actual": 0.111, "threshold": 0.12, "status": "FAIL"},
    ]
    return {
        "airport": "LEMD",
        "research_only": True,
        "generated_at": "2026-11-11T00:00:00+00:00",
        "application_version": "1.0.20",
        "export_engine_version": "v10.7.11",
        "forecast_engine_baseline": "v10.7.11",
        "protected_forecast_baseline": "v10.7.10",
        "actuals": [
            {
                "target_date": target,
                "stored_metar_max_c": 20.0,
                "is_final_station_actual": True,
            }
        ],
        "checkpoints": [
            _checkpoint(
                target,
                "D0 Morning @09:00",
                "2026-11-10T08:00:00+00:00",
                "2026-11-10T15:00:00+00:00",
            ),
            _checkpoint(
                target,
                "First Live @12:00",
                "2026-11-10T11:00:00+00:00",
                # Later revision must not change the frozen morning selector.
                "2026-11-10T18:00:00+00:00",
            ),
            _checkpoint(
                target,
                "Late Live @16:00",
                "2026-11-10T14:00:00+00:00",
                "2026-11-10T14:00:00+00:00",
                post_peak=True,
            ),
        ],
        "analog_promotion_gate": {
            "evaluation_version": "gate-v1",
            "oos_start_date": "2026-08-31",
            "gates": {
                "late_live_16_00": {
                    "eligible": False,
                    "oos_days": 75,
                    "conditions": conditions,
                    "blockers": ["mae_gain_c"],
                    "metrics": {
                        "champion_mae_c": 0.599,
                        "analog_mae_c": 0.488,
                        "mae_gain_c": 0.111,
                        "brier_gain": 0.055,
                        "champion_modal_bucket_hits": 43,
                        "analog_modal_bucket_hits": 45,
                    },
                }
            },
        },
    }


def test_report_keeps_release_blocked_and_preserves_exact_analog_gate() -> None:
    report = build_research_report([_export()])

    analog = report["candidates"]["late_analog"]
    assert analog["oos_days"] == 75
    assert analog["mae_gain_shortfall_c"] == pytest.approx(0.009)
    assert analog["release_recommendation"] == "CONTINUE_SHADOW"
    assert report["release_decision"]["status"] == "NO_RELEASE_CONTINUE_SHADOW"
    assert report["production_changed"] is False
    assert report["automatic_promotion"] is False


def test_post_peak_scores_modal_top2_brier_and_minimum_days() -> None:
    report = build_research_report([_export()])
    candidate = report["candidates"]["post_peak_upper_tail"]

    assert candidate["evidence_days"] == 1
    assert candidate["metrics"]["champion"]["modal_hits"] == 0
    assert candidate["metrics"]["candidate"]["modal_hits"] == 1
    assert candidate["metrics"]["candidate"]["top2_hits"] == 1
    assert "oos_days" in candidate["blockers"]
    assert candidate["release_recommendation"] == "CONTINUE_SHADOW"


def test_peak_relative_policy_freezes_morning_forecast_and_does_not_reconstruct() -> None:
    report = build_research_report([_export()])

    peak60 = report["timing"]["peak_minus_60"]
    assert peak60["policy_version"] == "morning_freeze_v1"
    assert peak60["selected_days"][0]["planned_at"] == "2026-11-10T14:00:00+00:00"
    assert peak60["selected_days"][0]["selected_checkpoint"] == "Late Live @16:00"
    assert peak60["selected_days"][0]["selection_gap_minutes"] == 0.0

    changed = deepcopy(_export())
    changed["checkpoints"][-1]["captured_at"] = "2026-11-10T15:00:00+00:00"
    missing = build_research_report([changed])["timing"]["peak_minus_60"]
    assert missing["matched_days"] == 0
    assert missing["missing_days"][0]["reason"] == "no_snapshot_within_5_minutes"


def test_newer_export_wins_for_overlapping_target_day() -> None:
    old = _export()
    new = deepcopy(old)
    new["generated_at"] = "2026-11-12T00:00:00+00:00"
    new["actuals"][0]["stored_metar_max_c"] = 21.0

    report = build_research_report([new, old])
    used = report["candidates"]["post_peak_upper_tail"]["used_days"]
    assert used[0]["actual_c"] == 21.0


def test_full_handoff_registry_covers_all_documented_days_and_counterexamples() -> None:
    registry, provenance = load_handoff_registry()
    report = build_research_report(
        [_export()], handoff_registry=registry, handoff_provenance=provenance
    )
    handoffs = report["full_handoff_case_evidence"]

    assert handoffs["date_range"] == {"start": "2026-10-11", "end": "2026-11-13"}
    assert handoffs["total_calendar_records"] == 34
    assert handoffs["evaluated_days"] == 33
    assert handoffs["unavailable_days"] == ["2026-10-23"]
    assert "2026-10-14" in handoffs["plateau_cap_protection_dates"]
    assert "2026-11-02" in handoffs["plateau_cap_protection_dates"]
    assert "2026-11-10" in handoffs["late_warm_error_dates"]
    assert report["handoff_registry_provenance"]["sha256"]
