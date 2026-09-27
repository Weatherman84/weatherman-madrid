"""Checkpoint-specific, research-only walk-forward challengers.

The functions in this module operate exclusively on the compact checkpoint
research records after they have been read.  They are deliberately not
imported by the collector, nowcast, decision, or trading runtime.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from .d1_evening_research import (
    D1_CHALLENGER_V2_VERSION,
    build_d1_walk_forward_challenger_v2,
    compare_d1_challenger_versions,
    shift_bucket_distribution,
)
from .research_tracks import (
    AUTOMATIC_PROMOTION,
    RESEARCH_ONLY,
    SCHEDULED_CAUSAL_EVIDENCE,
    positive_temperature_bucket,
    probability_ranking,
)


D1_CHECKPOINT = "D-1 Evening @20:00"
D0_CHECKPOINT = "D0 Morning @09:00"
FIRST_LIVE_CHECKPOINT = "First Live @12:00"
LATE_LIVE_CHECKPOINT = "Late Live @16:00"

D0_CHALLENGER_VERSION = "d0_morning_challenger_v0.1"
LATE_LIVE_CHALLENGER_VERSION = "late_live_ablation_challenger_v0.1"
MIN_PRIOR_CASES = 10
MIN_MAE_GAIN_C = 0.02

D0_ANCHOR_POLICIES: dict[str, float | None] = {
    "current_anchor_100pct": 1.0,
    "anchor_75pct": 0.75,
    "anchor_50pct": 0.50,
    "anchor_25pct": 0.25,
    "anchor_disabled": 0.0,
    "anchor_capped_0_10c": None,
}
D0_CLEAR_SKY_POLICIES = {
    "clear_sky_100pct": 1.0,
    "clear_sky_75pct": 0.75,
    "clear_sky_50pct": 0.50,
    "clear_sky_disabled": 0.0,
}
LATE_CLEAR_SKY_MULTIPLIERS = (0.0, 0.25, 0.50, 0.75, 1.0)
LATE_TAF_MULTIPLIERS = (0.0, 0.50, 1.0)
LATE_DRY_MIXING_MULTIPLIERS = (0.0, 0.50, 1.0)


def _number(value: object) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(parsed) else float(parsed)


def _probabilities(record: Mapping[str, Any]) -> object:
    return record.get("champion_probabilities") or record.get("probability_window", {}).get(
        "buckets", {}
    )


def _actual_bucket(record: Mapping[str, Any]) -> int | None:
    value = record.get("actual_bucket")
    return int(value) if value is not None else positive_temperature_bucket(
        record.get("stored_metar_actual_c")
    )


def _counterfactual(
    record: Mapping[str, Any],
    *,
    delta_c: float,
    version: str,
    policy: str,
    reason_codes: Sequence[str],
    prior_n: int,
    status: str,
) -> dict[str, Any]:
    center = _number(record.get("champion_center_c"))
    shifted = shift_bucket_distribution(_probabilities(record), delta_c)
    ranked = probability_ranking(shifted)
    modal = ranked[0][0] if ranked else record.get("champion_modal_bucket")
    actual = _number(record.get("stored_metar_actual_c"))
    actual_bucket = _actual_bucket(record)
    challenger_center = center + delta_c if center is not None else None
    champion_error = center - actual if center is not None and actual is not None else None
    challenger_error = (
        challenger_center - actual
        if challenger_center is not None and actual is not None
        else None
    )
    if champion_error is None or challenger_error is None:
        outcome = "unresolved"
    elif abs(challenger_error) < abs(champion_error) - 1e-12:
        outcome = "improved"
    elif abs(challenger_error) > abs(champion_error) + 1e-12:
        outcome = "worsened"
    else:
        outcome = "unchanged"
    return {
        "challenger_version": version,
        "status": status,
        "selected_policy": policy,
        "champion_center_c": center,
        "champion_modal_bucket": record.get("champion_modal_bucket"),
        "challenger_center_c": challenger_center,
        "challenger_modal_bucket": modal,
        "center_adjustment_c": delta_c,
        "probabilities": shifted,
        "top1_bucket": ranked[0][0] if ranked else None,
        "top1_probability": ranked[0][1] if ranked else None,
        "top2_bucket": ranked[1][0] if len(ranked) > 1 else None,
        "top2_probability": ranked[1][1] if len(ranked) > 1 else None,
        "top3_bucket": ranked[2][0] if len(ranked) > 2 else None,
        "top3_probability": ranked[2][1] if len(ranked) > 2 else None,
        "correction_deltas": {"total_c": delta_c},
        "applied_challenger_rules": [policy],
        "reason_codes": list(reason_codes),
        "active_regimes": list(record.get("active_regimes", [])),
        "taf_signal": {
            "bucket": record.get("taf_bucket"),
            "agreement": record.get("taf_agreement"),
            "outside_top2": record.get("taf_outside_top2"),
        },
        "model_trend_signal": record.get("model_history", {}).get(
            "consensus_movement", "unavailable"
        ),
        "evidence_class": record.get("evidence_class"),
        "prior_scheduled_causal_n": prior_n,
        "stored_metar_actual_c": actual,
        "actual_bucket": actual_bucket,
        "champion_error_c": champion_error,
        "challenger_error_c": challenger_error,
        "champion_hit": (
            record.get("champion_modal_bucket") == actual_bucket
            if actual_bucket is not None else None
        ),
        "challenger_hit": modal == actual_bucket if actual_bucket is not None else None,
        "outcome_vs_champion": outcome,
        "research_only": RESEARCH_ONLY,
        "automatic_promotion": AUTOMATIC_PROMOTION,
    }


def _distribution_scores(
    probabilities: object, actual_bucket: int | None
) -> tuple[float | None, float | None]:
    ranked = probability_ranking(probabilities)
    if not ranked or actual_bucket is None:
        return None, None
    values = dict(ranked)
    buckets = set(values) | {actual_bucket}
    brier = sum(
        (values.get(bucket, 0.0) - (1.0 if bucket == actual_bucket else 0.0)) ** 2
        for bucket in buckets
    )
    return brier, -math.log(max(1e-12, values.get(actual_bucket, 0.0)))


def _metrics(
    rows: Sequence[Mapping[str, Any]], *, challenger_key: str | None = None
) -> dict[str, Any]:
    eligible = [row for row in rows if _actual_bucket(row) is not None]
    if not eligible:
        return {"n": 0}
    centers: list[float] = []
    modals: list[int | None] = []
    rankings: list[list[tuple[int, float]]] = []
    actuals: list[int] = []
    briers: list[float] = []
    logs: list[float] = []
    for row in eligible:
        actual = int(_actual_bucket(row))
        actuals.append(actual)
        if challenger_key:
            candidate = row.get(challenger_key, {})
            center = _number(candidate.get("challenger_center_c"))
            modal = candidate.get("challenger_modal_bucket")
            probabilities = candidate.get("probabilities")
        else:
            center = _number(row.get("champion_center_c"))
            modal = row.get("champion_modal_bucket")
            probabilities = _probabilities(row)
        actual_c = _number(row.get("stored_metar_actual_c"))
        if center is not None and actual_c is not None:
            centers.append(center - actual_c)
        modals.append(int(modal) if modal is not None else None)
        ranking = probability_ranking(probabilities)
        rankings.append(ranking)
        brier, log_loss = _distribution_scores(probabilities, actual)
        if brier is not None:
            briers.append(brier)
        if log_loss is not None:
            logs.append(log_loss)
    n = len(eligible)
    return {
        "n": n,
        "modal_accuracy": sum(m == a for m, a in zip(modals, actuals, strict=True)) / n,
        "top2_coverage": sum(
            a in {bucket for bucket, _ in ranking[:2]}
            for ranking, a in zip(rankings, actuals, strict=True)
        ) / n,
        "top3_coverage": sum(
            a in {bucket for bucket, _ in ranking[:3]}
            for ranking, a in zip(rankings, actuals, strict=True)
        ) / n,
        "center_mae_c": (
            sum(abs(value) for value in centers) / len(centers) if centers else None
        ),
        "center_bias_c": sum(centers) / len(centers) if centers else None,
        "brier_score": sum(briers) / len(briers) if briers else None,
        "log_loss": sum(logs) / len(logs) if logs else None,
    }


def _policy_metrics(
    prior: Sequence[Mapping[str, Any]], delta_builder
) -> dict[str, Any]:
    translated: list[dict[str, Any]] = []
    for row in prior:
        candidate = _counterfactual(
            row,
            delta_c=float(delta_builder(row)),
            version="policy_evaluation_only",
            policy="policy_evaluation_only",
            reason_codes=[],
            prior_n=0,
            status="historical_research",
        )
        translated.append({**row, "candidate": candidate})
    return _metrics(translated, challenger_key="candidate")


def _select_conservative_policy(
    prior: Sequence[Mapping[str, Any]], policies: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    baseline_name = next(iter(policies))
    matrix = {
        name: _policy_metrics(prior, builder) for name, builder in policies.items()
    }
    if len(prior) < MIN_PRIOR_CASES:
        return baseline_name, {
            "status": "warmup_insufficient_prior_cases",
            "prior_n": len(prior),
            "minimum_n": MIN_PRIOR_CASES,
            "policy_metrics": matrix,
        }
    baseline = matrix[baseline_name]
    candidates: list[tuple[str, Mapping[str, Any]]] = []
    for name, metrics in matrix.items():
        modal_gain = metrics["modal_accuracy"] - baseline["modal_accuracy"]
        mae_gain = baseline["center_mae_c"] - metrics["center_mae_c"]
        if name == baseline_name or modal_gain > 0 or (
            modal_gain >= 0 and mae_gain >= MIN_MAE_GAIN_C
        ):
            candidates.append((name, metrics))
    selected = min(
        candidates,
        key=lambda item: (
            -item[1]["modal_accuracy"],
            item[1]["center_mae_c"],
            abs(item[1]["center_bias_c"]),
            0 if item[0] == baseline_name else 1,
            item[0],
        ),
    )[0]
    return selected, {
        "status": "selected_from_prior_scheduled_causal_only",
        "prior_n": len(prior),
        "minimum_n": MIN_PRIOR_CASES,
        "minimum_mae_gain_c_when_modal_tied": MIN_MAE_GAIN_C,
        "policy_metrics": matrix,
    }


def _d0_anchor_delta(record: Mapping[str, Any], policy: str) -> float:
    anchor = _number(record.get("correction_contributions", {}).get(
        "temp_anchor_adjustment_c"
    )) or 0.0
    multiplier = D0_ANCHOR_POLICIES[policy]
    if multiplier is None:
        applied = max(-0.10, min(0.10, anchor))
    else:
        applied = anchor * multiplier
    return applied - anchor


def build_d0_walk_forward_challenger(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build a causal D0 anchor challenger using only earlier completed days."""
    ordered = sorted(records, key=lambda row: str(row.get("target_date")))
    prior: list[dict[str, Any]] = []
    result: list[dict[str, Any]] = []
    for source in ordered:
        record = dict(source)
        policies = {
            name: (lambda row, name=name: _d0_anchor_delta(row, name))
            for name in D0_ANCHOR_POLICIES
        }
        selected, calibration = _select_conservative_policy(prior, policies)
        delta = _d0_anchor_delta(record, selected)
        reasons = ["temperature_anchor_ablation"]
        if record.get("taf_agreement") == "disagreement":
            reasons.append("taf_disagreement_context")
        if "Bucket Boundary" in record.get("active_regimes", []):
            reasons.append("bucket_boundary_context")
        shadow = _counterfactual(
            record,
            delta_c=delta,
            version=D0_CHALLENGER_VERSION,
            policy=selected,
            reason_codes=reasons,
            prior_n=len(prior),
            status=calibration["status"],
        )
        shadow["correction_deltas"] = {
            "temperature_anchor_c": delta,
            "clear_sky_c": 0.0,
            "total_c": delta,
        }
        shadow["walk_forward_calibration"] = calibration
        record["forecast_research_shadow"] = shadow
        result.append(record)
        if (
            record.get("evidence_class") == SCHEDULED_CAUSAL_EVIDENCE
            and _actual_bucket(record) is not None
        ):
            prior.append(record)
    return result


def _late_delta(
    record: Mapping[str, Any], clear_sky: float, taf: float, dry_mixing: float
) -> float:
    adjustments = record.get("correction_contributions", {})
    clear = _number(adjustments.get("clear_sky_override_adjustment_c")) or 0.0
    taf_value = _number(adjustments.get("taf_adjustment_c")) or 0.0
    dry = _number(adjustments.get("late_dry_mixing_adjustment_c")) or 0.0
    return (clear_sky - 1.0) * clear + (taf - 1.0) * taf_value + (dry_mixing - 1.0) * dry


def _late_policy_name(clear_sky: float, taf: float, dry_mixing: float) -> str:
    return f"clear_{int(clear_sky * 100)}_taf_{int(taf * 100)}_dry_{int(dry_mixing * 100)}"


def _late_active_policies() -> dict[str, Any]:
    values = [(1.0, 1.0, 1.0)]
    values += [(value, 1.0, 1.0) for value in LATE_CLEAR_SKY_MULTIPLIERS[:-1]]
    values += [(1.0, value, 1.0) for value in LATE_TAF_MULTIPLIERS[:-1]]
    values += [(1.0, 1.0, value) for value in LATE_DRY_MIXING_MULTIPLIERS[:-1]]
    return {
        _late_policy_name(*values_): (
            lambda row, values_=values_: _late_delta(row, *values_)
        )
        for values_ in values
    }


def build_late_live_walk_forward_challenger(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build a simple one-factor-at-a-time Late Live ablation challenger."""
    ordered = sorted(records, key=lambda row: str(row.get("target_date")))
    prior: list[dict[str, Any]] = []
    result: list[dict[str, Any]] = []
    policies = _late_active_policies()
    for source in ordered:
        record = dict(source)
        selected, calibration = _select_conservative_policy(prior, policies)
        parts = selected.split("_")
        multipliers = (float(parts[1]) / 100, float(parts[3]) / 100, float(parts[5]) / 100)
        delta = _late_delta(record, *multipliers)
        reasons = ["late_live_positive_correction_ablation"]
        adjustments = record.get("correction_contributions", {})
        positive_overlap = sum(max(0.0, _number(adjustments.get(field)) or 0.0) for field in (
            "clear_sky_override_adjustment_c", "cloud_adjustment_c",
            "radiation_adjustment_c", "late_dry_mixing_adjustment_c",
            "heating_rate_adjustment_c",
        ))
        if positive_overlap >= 0.5:
            reasons.append("positive_driver_overlap_watch")
        if record.get("taf_modal_bucket_flip"):
            reasons.append("taf_modal_flip_watch")
        shadow = _counterfactual(
            record,
            delta_c=delta,
            version=LATE_LIVE_CHALLENGER_VERSION,
            policy=selected,
            reason_codes=reasons,
            prior_n=len(prior),
            status=calibration["status"],
        )
        clear, taf_value, dry = multipliers
        shadow["correction_deltas"] = {
            "clear_sky_override_c": (clear - 1.0) * (
                _number(adjustments.get("clear_sky_override_adjustment_c")) or 0.0
            ),
            "taf_center_adjustment_c": (taf_value - 1.0) * (
                _number(adjustments.get("taf_adjustment_c")) or 0.0
            ),
            "late_dry_mixing_c": (dry - 1.0) * (
                _number(adjustments.get("late_dry_mixing_adjustment_c")) or 0.0
            ),
            "total_c": delta,
        }
        shadow["walk_forward_calibration"] = calibration
        shadow["selection_policy"] = "current_or_single_factor_ablation_only"
        record["forecast_research_shadow"] = shadow
        result.append(record)
        if (
            record.get("evidence_class") == SCHEDULED_CAUSAL_EVIDENCE
            and _actual_bucket(record) is not None
        ):
            prior.append(record)
    return result


def _comparison(
    rows: Sequence[Mapping[str, Any]], challenger_key: str = "forecast_research_shadow"
) -> dict[str, Any]:
    eligible = [row for row in rows if _actual_bucket(row) is not None]
    outcomes = Counter(
        row.get(challenger_key, {}).get("outcome_vs_champion", "unresolved")
        for row in eligible
    )
    midpoint = len(eligible) // 2
    return {
        "champion": _metrics(eligible),
        "challenger": _metrics(eligible, challenger_key=challenger_key),
        "cases_improved": outcomes.get("improved", 0),
        "cases_worsened": outcomes.get("worsened", 0),
        "cases_unchanged": outcomes.get("unchanged", 0),
        "performance_over_time": {
            "first_half": {
                "champion": _metrics(eligible[:midpoint]),
                "challenger": _metrics(
                    eligible[:midpoint], challenger_key=challenger_key
                ),
            },
            "second_half": {
                "champion": _metrics(eligible[midpoint:]),
                "challenger": _metrics(
                    eligible[midpoint:], challenger_key=challenger_key
                ),
            },
        },
        "applied_rule_sample_sizes": dict(Counter(
            row.get(challenger_key, {}).get("selected_policy", "unavailable")
            for row in eligible
        )),
        "historical_replay_not_oos": True,
    }


def _research_context_matrix(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Report context sample sizes without turning small groups into rules."""
    expanded: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        labels = list(row.get("active_regimes", []))
        taf = row.get("taf_agreement")
        if taf:
            labels.append(f"TAF {taf}")
        movement = row.get("model_history", {}).get("consensus_movement")
        if movement:
            labels.append(f"Model trend {movement}")
        spread = _number(row.get("model_spread_c"))
        if spread is not None:
            labels.append(
                "Model spread high" if spread >= 2.0
                else "Model spread low" if spread <= 1.0
                else "Model spread middle"
            )
        for label in dict.fromkeys(labels):
            expanded.setdefault(label, []).append(row)
    result = []
    for label, group in sorted(expanded.items()):
        metrics = _metrics(group)
        result.append({
            "context": label,
            "n": len(group),
            "eligible_for_conditioned_rule": len(group) >= MIN_PRIOR_CASES,
            "champion": metrics,
        })
    return result


def _full_sensitivity_matrix(
    rows: Sequence[Mapping[str, Any]], checkpoint: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if checkpoint == D0_CHECKPOINT:
        for name in D0_ANCHOR_POLICIES:
            metrics = _policy_metrics(rows, lambda row, name=name: _d0_anchor_delta(row, name))
            result.append({"policy": name, "signal_layer": "anchor_center", **metrics})
        for name, multiplier in D0_CLEAR_SKY_POLICIES.items():
            metrics = _policy_metrics(rows, lambda row, multiplier=multiplier: (
                (multiplier - 1.0) * (
                    _number(row.get("correction_contributions", {}).get(
                        "clear_sky_override_adjustment_c"
                    )) or 0.0
                )
            ))
            result.append({
                "policy": name,
                "signal_layer": "clear_sky_center",
                "sensitivity_only": True,
                **metrics,
            })
        for multiplier in (0.0, 0.5, 1.0):
            metrics = _policy_metrics(rows, lambda row, multiplier=multiplier: (
                (multiplier - 1.0) * (
                    _number(row.get("correction_contributions", {}).get(
                        "taf_adjustment_c"
                    )) or 0.0
                )
            ))
            result.append({
                "policy": f"taf_center_{int(multiplier * 100)}pct",
                "signal_layer": "taf_center",
                "sensitivity_only": True,
                **metrics,
            })
    elif checkpoint == LATE_LIVE_CHECKPOINT:
        for clear in LATE_CLEAR_SKY_MULTIPLIERS:
            for taf in LATE_TAF_MULTIPLIERS:
                for dry in LATE_DRY_MIXING_MULTIPLIERS:
                    metrics = _policy_metrics(
                        rows,
                        lambda row, clear=clear, taf=taf, dry=dry: _late_delta(
                            row, clear, taf, dry
                        ),
                    )
                    result.append({
                        "policy": _late_policy_name(clear, taf, dry),
                        "clear_sky_multiplier": clear,
                        "taf_multiplier": taf,
                        "late_dry_mixing_multiplier": dry,
                        "active_selection_eligible": sum(
                            value != 1.0 for value in (clear, taf, dry)
                        ) <= 1,
                        **metrics,
                    })
    return result


def apply_checkpoint_research_challengers(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach immutable research shadows and checkpoint-specific evaluations."""
    grouped = {
        checkpoint: [
            dict(row) for row in records
            if row.get("checkpoint_label") == checkpoint
            and row.get("evidence_class") == SCHEDULED_CAUSAL_EVIDENCE
        ]
        for checkpoint in (
            D1_CHECKPOINT, D0_CHECKPOINT, FIRST_LIVE_CHECKPOINT, LATE_LIVE_CHECKPOINT
        )
    }
    output = [dict(row) for row in records]
    by_key = {
        (row.get("target_date"), row.get("checkpoint_label")): row for row in output
    }

    d1_input = []
    for row in grouped[D1_CHECKPOINT]:
        d1_input.append({
            **row,
            "champion_probabilities": _probabilities(row),
            "checkpoint": D1_CHECKPOINT,
            "modal_bucket": row.get("champion_modal_bucket"),
            "raw_spread_c": row.get("model_spread_c"),
            "consensus_run_trend_c": row.get("model_history", {}).get(
                "consensus_run_trend_c"
            ),
            "temp_anchor_adjustment_c": row.get("correction_contributions", {}).get(
                "temp_anchor_adjustment_c"
            ),
            "clear_sky_override_adjustment_c": row.get(
                "correction_contributions", {}
            ).get("clear_sky_override_adjustment_c"),
        })
    d1_scored = build_d1_walk_forward_challenger_v2(d1_input)
    for row in d1_scored:
        candidate = row.get("challenger_v0_2", {})
        actual = _number(row.get("stored_metar_actual_c"))
        champion = _number(row.get("champion_center_c"))
        challenger = _number(candidate.get("center_c"))
        if actual is None or champion is None or challenger is None:
            outcome = "unresolved"
        elif abs(challenger - actual) < abs(champion - actual) - 1e-12:
            outcome = "improved"
        elif abs(challenger - actual) > abs(champion - actual) + 1e-12:
            outcome = "worsened"
        else:
            outcome = "unchanged"
        shadow = {
            "challenger_version": D1_CHALLENGER_V2_VERSION,
            "status": candidate.get("status"),
            "selected_policy": candidate.get("calibration", {}).get(
                "selected_bias_policy", "warmup_no_adjustment"
            ),
            "champion_center_c": champion,
            "champion_modal_bucket": row.get("champion_modal_bucket"),
            "challenger_center_c": challenger,
            "challenger_modal_bucket": candidate.get("modal_bucket"),
            "center_adjustment_c": candidate.get("center_adjustment_c"),
            "probabilities": candidate.get("probabilities"),
            "correction_deltas": {
                "expanding_bias_c": candidate.get("center_adjustment_c"),
                "total_c": candidate.get("center_adjustment_c"),
            },
            "applied_challenger_rules": [
                candidate.get("calibration", {}).get(
                    "selected_bias_policy", "warmup_no_adjustment"
                )
            ],
            "reason_codes": candidate.get("reason_codes", []),
            "active_regimes": row.get("active_regimes", []),
            "taf_signal": {
                "bucket": row.get("taf_bucket"),
                "directional_guard": candidate.get("calibration", {}).get(
                    "directional_guard"
                ),
            },
            "model_trend_signal": "logged_only_no_center_adjustment",
            "evidence_class": row.get("evidence_class"),
            "prior_scheduled_causal_n": candidate.get("calibration", {}).get("prior_n"),
            "stored_metar_actual_c": actual,
            "actual_bucket": _actual_bucket(row),
            "champion_error_c": champion - actual if actual is not None and champion is not None else None,
            "challenger_error_c": challenger - actual if actual is not None and challenger is not None else None,
            "champion_hit": row.get("champion_modal_bucket") == _actual_bucket(row),
            "challenger_hit": candidate.get("modal_bucket") == _actual_bucket(row),
            "outcome_vs_champion": outcome,
            "walk_forward_calibration": candidate.get("calibration"),
            "bucket_boundary_taf_interaction": candidate.get(
                "bucket_boundary_taf_interaction"
            ),
            "research_only": RESEARCH_ONLY,
            "automatic_promotion": AUTOMATIC_PROMOTION,
        }
        by_key[(row.get("target_date"), D1_CHECKPOINT)]["forecast_research_shadow"] = shadow

    d0_scored = build_d0_walk_forward_challenger(grouped[D0_CHECKPOINT])
    for row in d0_scored:
        by_key[(row.get("target_date"), D0_CHECKPOINT)]["forecast_research_shadow"] = row[
            "forecast_research_shadow"
        ]

    late_scored = build_late_live_walk_forward_challenger(grouped[LATE_LIVE_CHECKPOINT])
    for row in late_scored:
        by_key[(row.get("target_date"), LATE_LIVE_CHECKPOINT)][
            "forecast_research_shadow"
        ] = row["forecast_research_shadow"]

    for row in grouped[FIRST_LIVE_CHECKPOINT]:
        by_key[(row.get("target_date"), FIRST_LIVE_CHECKPOINT)][
            "forecast_research_shadow"
        ] = {
            "challenger_version": None,
            "status": "champion_protected_no_active_forecast_challenger",
            "reason_codes": [
                "walk_forward_did_not_show_stable_forecast_improvement",
                "uncertainty_and_trading_research_only",
            ],
            "evidence_class": row.get("evidence_class"),
            "research_only": RESEARCH_ONLY,
            "automatic_promotion": AUTOMATIC_PROMOTION,
        }

    evaluation = {
        "default_evidence_class": SCHEDULED_CAUSAL_EVIDENCE,
        "d1_evening": {
            "version": D1_CHALLENGER_V2_VERSION,
            "comparison": compare_d1_challenger_versions(d1_scored),
        },
        "d0_morning": {
            "version": D0_CHALLENGER_VERSION,
            "comparison": _comparison(d0_scored),
            "full_sensitivity_matrix": _full_sensitivity_matrix(
                grouped[D0_CHECKPOINT], D0_CHECKPOINT
            ),
            "context_sample_matrix": _research_context_matrix(grouped[D0_CHECKPOINT]),
        },
        "first_live": {
            "version": None,
            "status": "champion_protected_no_active_forecast_challenger",
            "champion": _metrics(grouped[FIRST_LIVE_CHECKPOINT]),
        },
        "late_live": {
            "version": LATE_LIVE_CHALLENGER_VERSION,
            "comparison": _comparison(late_scored),
            "full_sensitivity_matrix": _full_sensitivity_matrix(
                grouped[LATE_LIVE_CHECKPOINT], LATE_LIVE_CHECKPOINT
            ),
            "context_sample_matrix": _research_context_matrix(
                grouped[LATE_LIVE_CHECKPOINT]
            ),
            "active_selection_policy": "current_or_single_factor_ablation_only",
        },
        "methodology": {
            "historical_replay_not_oos": True,
            "fit_policy": "expanding_prior_scheduled_causal_only",
            "future_actuals_used": False,
            "reconstructed_research_used_for_selection": False,
            "first_live_production_champion_unchanged": True,
            "probability_score_policy": (
                "exact_when_full_champion_probabilities_are_exported; legacy_v0.1_"
                "probability_window_replays_are_sensitivity_only"
            ),
        },
        "research_only": RESEARCH_ONLY,
        "automatic_promotion": AUTOMATIC_PROMOTION,
    }
    return output, evaluation
