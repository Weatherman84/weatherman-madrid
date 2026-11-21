"""Offline diagnostic parameter study using existing policy math, never trainer overrides."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path


from weatherman import checkpoint_challengers as existing
from weatherman import d1_evening_research as d1
from weatherman.historical_archive import ArchiveError, atomic_json, utc
from weatherman.historical_replay import CHECKPOINTS, clean, metrics
from weatherman.research_tracks import probability_ranking


def policies(checkpoint):
    if checkpoint == existing.D0_CHECKPOINT:
        return {
            name: (lambda row, name=name: existing._d0_anchor_delta(row, name))
            for name in existing.D0_ANCHOR_POLICIES
        }
    if checkpoint == existing.LATE_LIVE_CHECKPOINT:
        return existing._late_active_policies()
    return {"current_protected_forecast": lambda row: 0.0}


def prior_cases(rows, current, *, window=None):
    cutoff = utc(current["checkpoint_at"])
    target = date.fromisoformat(current["target_date"])
    return [
        row
        for row in rows
        if row["checkpoint_label"] == current["checkpoint_label"]
        and row["evidence_class"] == current["evidence_class"]
        and row.get("actual_complete")
        and row.get("actual_bucket") is not None
        and row.get("champion_center_c") is not None
        and date.fromisoformat(row["target_date"]) < target
        and (
            window is None
            or date.fromisoformat(row["target_date"]) >= target - timedelta(days=window)
        )
        and row.get("actual_available_at") is not None
        and utc(row["actual_available_at"]) <= cutoff
    ]


def scenario(row, name, builder):
    """Existing additive correction/distribution-shift scenario; not a full engine rerun."""
    # These builders read pre-label correction contributions, never current Actual.
    delta = float(builder(row))
    blind = {k: v for k, v in row.items() if not k.startswith("actual_")}
    blind.pop("stored_metar_actual_c", None)
    blind.pop("archive_metar_max_c", None)
    candidate = existing._counterfactual(
        blind,
        delta_c=delta,
        version="existing_parameter_policy_simulation",
        policy=name,
        reason_codes=[],
        prior_n=0,
        status="diagnostic_partial_policy_scenario",
    )
    result = {
        **row,
        "champion_center_c": candidate["challenger_center_c"],
        "champion_modal_bucket": candidate["challenger_modal_bucket"],
        "champion_probabilities": candidate["probabilities"],
        "selected_policy": name,
        "center_adjustment_c": delta,
    }
    for index, (bucket, probability) in enumerate(
        probability_ranking(candidate["probabilities"])[:3], 1
    ):
        result[f"top{index}_bucket"] = bucket
        result[f"top{index}_probability"] = probability
    return result


def select(prior, family):
    # Pure existing selector; the public trainers' scheduled-causal guards are untouched.
    chosen, metadata = existing._select_conservative_policy(prior, family)
    return chosen, {**metadata, "status": "diagnostic_prior_availability_only"}


def native_variants(rows):
    results = []
    for name in sorted(
        {name for row in rows for name in row.get("engine_challenger_variants", {})}
    ):
        candidates = []
        for row in rows:
            variant = row.get("engine_challenger_variants", {}).get(name)
            if not variant:
                continue
            ranking = probability_ranking(variant["probabilities"])
            candidates.append(
                {
                    **row,
                    "champion_center_c": variant["forecast_mean_c"],
                    "champion_probabilities": variant["probabilities"],
                    "champion_modal_bucket": ranking[0][0],
                    "top1_probability": ranking[0][1],
                }
            )
        results.append({"existing_variant": name, **metrics(candidates)})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    root = args.archive.resolve()
    source = root / "derived/checkpoints.json"
    plan_path = root / "results/research-stage-plan.json"
    plan = json.loads(plan_path.read_text())
    rows = json.loads(source.read_text())
    if any(not r.get("research_only") or r.get("automatic_promotion") for r in rows):
        raise ArchiveError("Research isolation not satisfied")
    fingerprint = hashlib.sha256(
        source.read_bytes()
        + Path(__file__).read_bytes()
        + json.dumps(
            {k: plan[k] for k in ("holdout_start", "excluded_dry_run_dates")}, sort_keys=True
        ).encode()
    ).hexdigest()
    destination = root / "results/historical-parameter-study.json"
    if destination.exists():
        saved = json.loads(destination.read_text())
        if saved.get("fingerprint") == fingerprint:
            print("Existing frozen study reused; holdout not evaluated again")
            return 0
        raise ArchiveError("Holdout already evaluated under another study; no automatic retuning")
    excluded = set(plan["excluded_dry_run_dates"])
    usable = [
        r
        for r in rows
        if r.get("champion_center_c") is not None and r["target_date"] not in excluded
    ]
    split = plan["holdout_start"]
    report = {
        "fingerprint": fingerprint,
        "research_only": True,
        "automatic_promotion": False,
        "evidence_class": "historical_partial_fidelity",
        "conditional_on_assumed_availability": True,
        "certified_oos": False,
        "full_engine_counterfactual_rerun": False,
        "method": "existing_single_factor_policy_math_and_distribution_shifts",
        "new_regimes": 0,
        "new_challengers": 0,
        "holdout_start": split,
        "excluded_dry_run_dates": sorted(excluded),
        "parameter_policy_results": [],
        "native_engine_variants_training_only": [],
        "public_challenger_trainers": [],
        "trading_pnl": {"status": "not_run_missing_executable_quotes"},
    }
    records = []
    plan["parameter_search_started"] = True
    atomic_json(plan_path, plan)
    for evidence in sorted({r["evidence_class"] for r in usable}):
        for checkpoint, _, _ in CHECKPOINTS:
            group = [
                r
                for r in usable
                if r["checkpoint_label"] == checkpoint and r["evidence_class"] == evidence
            ]
            train = [r for r in group if r["target_date"] < split]
            holdout = [r for r in group if r["target_date"] >= split]
            family = policies(checkpoint)
            # Freeze using only labels known at the FIRST calendar holdout checkpoint.
            from weatherman.historical_replay import schedule

            freeze_at = dict(schedule(date.fromisoformat(split)))[checkpoint]
            anchor = {
                "target_date": split,
                "checkpoint_at": utc(freeze_at).isoformat(),
                "checkpoint_label": checkpoint,
                "evidence_class": evidence,
            }
            fit = prior_cases(train, anchor)
            chosen, calibration = select(fit, family)
            training_matrix = [
                {"policy": name, **metrics([scenario(r, name, builder) for r in fit])}
                for name, builder in family.items()
            ]
            frozen = [scenario(r, chosen, family[chosen]) for r in holdout]
            result = {
                "checkpoint": checkpoint,
                "evidence_class": evidence,
                "diagnostic_only": True,
                "frozen_policy": chosen,
                "freeze_at": utc(freeze_at).isoformat(),
                "training_calibration": calibration,
                "training_parameter_matrix": training_matrix,
                "diagnostic_in_sample_upper_bound": max(
                    (m.get("modal_accuracy", 0) for m in training_matrix), default=0
                ),
                "holdout_baseline": metrics(holdout),
                "holdout_frozen_policy": metrics(frozen),
                "walk_forward": [],
            }
            for window in (None, 90):
                predicted = []
                for current in group:
                    prior = prior_cases(group, current, window=window)
                    policy, _ = select(prior, family)
                    candidate = scenario(current, policy, family[policy])
                    candidate["study_window"] = "expanding" if window is None else "rolling_90_days"
                    candidate["prior_diagnostic_n"] = len(prior)
                    predicted.append(candidate)
                result["walk_forward"].append(
                    {
                        "window": "expanding" if window is None else "rolling_90_days",
                        "baseline": metrics(group),
                        "existing_policy_simulation": metrics(predicted),
                    }
                )
                records.extend(predicted)
            report["parameter_policy_results"].append(result)
            report["native_engine_variants_training_only"].append(
                {
                    "checkpoint": checkpoint,
                    "evidence_class": evidence,
                    "variants": native_variants(fit),
                }
            )
            # Run public challenger wrappers unchanged: partial rows cannot enter their fit pools.
            if checkpoint == existing.D1_CHECKPOINT:
                public = d1.build_d1_walk_forward_challenger_v2(group)
                prior_n = max(
                    (r["challenger_v0_2"]["calibration"].get("prior_n", 0) for r in public),
                    default=0,
                )
            elif checkpoint == existing.D0_CHECKPOINT:
                public = existing.build_d0_walk_forward_challenger(group)
                prior_n = max(
                    (
                        r["forecast_research_shadow"]["walk_forward_calibration"].get("prior_n", 0)
                        for r in public
                    ),
                    default=0,
                )
            elif checkpoint == existing.LATE_LIVE_CHECKPOINT:
                public = existing.build_late_live_walk_forward_challenger(group)
                prior_n = max(
                    (
                        r["forecast_research_shadow"]["walk_forward_calibration"].get("prior_n", 0)
                        for r in public
                    ),
                    default=0,
                )
            else:
                continue
            report["public_challenger_trainers"].append(
                {
                    "checkpoint": checkpoint,
                    "eligible_training_n": prior_n,
                    "status": "executed_unchanged_no_qualified_scheduled_causal_history",
                }
            )
    plan["holdout_evaluated"] = True
    plan["status"] = "completed_conditional_parameter_study"
    atomic_json(root / "derived/historical-policy-study-records.json", clean(records))
    atomic_json(destination, clean(report))
    atomic_json(plan_path, plan)
    print(
        json.dumps(
            {
                "status": "completed_conditional_diagnostics",
                "checkpoints": len(report["parameter_policy_results"]),
                "holdout_evaluated_once": True,
                "public_trainers_unchanged": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
