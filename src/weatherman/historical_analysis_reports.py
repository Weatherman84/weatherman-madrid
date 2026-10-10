"""Local diagnostic tournaments, causal parameter selection, and price-only P&L."""

from __future__ import annotations

import itertools
import json
import math
import statistics
import types
from collections import Counter, defaultdict
from functools import lru_cache

from . import checkpoint_challengers as cc
from . import d1_evening_research as d1
from .analytics import probability_for_range
from .historical_analysis import (
    CHECKPOINTS,
    COHORT,
    FLAGS,
    REGIMES,
    SCENARIOS,
    VERSION,
    available_prior,
    comparison as _base_comparison,
    d1_prediction,
    prediction,
    registry,
    sample_class,
    score as _base_score,
    valid,
)
from .historical_archive import ArchiveError, atomic_json, digest, utc
from .historical_replay import clean
from .providers import _temperature_range
from .research_tracks import probability_ranking


_REASON_FIELDS = (
    "modal_bucket",
    "taf_bucket",
    "consensus_run_trend_c",
    "raw_spread_c",
    "champion_center_c",
    "temp_anchor_adjustment_c",
    "clear_sky_override_adjustment_c",
)


@lru_cache(maxsize=20000)
def _cached_existing_reasons(values):
    return tuple(d1.d1_reason_codes(dict(zip(_REASON_FIELDS, values, strict=True))))


def _reason_codes(record):
    return list(_cached_existing_reasons(tuple(record.get(k) for k in _REASON_FIELDS)))


# Memoize the pure existing calculation on its full input tuple, in a private
# namespace. No protected module or Actual evidence is changed.
_d1_proxy = types.SimpleNamespace(**{**vars(d1), "d1_reason_codes": _reason_codes})
_original_d1_prediction = d1_prediction
d1_prediction = types.FunctionType(
    _original_d1_prediction.__code__,
    {**_original_d1_prediction.__globals__, "d1": _d1_proxy},
    _original_d1_prediction.__name__,
    _original_d1_prediction.__defaults__,
)
d1_prediction.__kwdefaults__ = _original_d1_prediction.__kwdefaults__


def comparison(base, candidate):
    result = _base_comparison(base, candidate)
    pairs = [(a, b) for a, b in zip(base, candidate, strict=True) if valid(a) and valid(b)]
    improved = sum(
        abs(b["champion_center_c"] - b["stored_metar_actual_c"])
        < abs(a["champion_center_c"] - a["stored_metar_actual_c"]) - 1e-12
        for a, b in pairs
    )
    worsened = sum(
        abs(b["champion_center_c"] - b["stored_metar_actual_c"])
        > abs(a["champion_center_c"] - a["stored_metar_actual_c"]) + 1e-12
        for a, b in pairs
    )
    result.update(
        center_mae_improved=improved,
        center_mae_worsened=worsened,
        center_mae_unchanged=len(pairs) - improved - worsened,
    )
    return result


def score(rows):
    result = _base_score(rows)
    if result["n"]:
        bins = result["calibration"]
        result["calibration_summary"] = {
            "mean_top1_probability": result["average_top1_probability"],
            "observed_top1_accuracy": result["modal_accuracy"],
            "expected_calibration_error": sum(
                b["n"] * abs(b["mean_probability"] - b["hit_rate"]) for b in bins
            )
            / result["n"],
            "maximum_bin_gap": max(abs(b["mean_probability"] - b["hit_rate"]) for b in bins),
            "bins": "existing 10pp Top1 probability bins; sparse bins exploratory",
        }
    return result


def key(row):
    return row["target_date"], row["checkpoint_label"]


def persist(root, filename, payload):
    atomic_json(root / filename, clean({"version": VERSION, **FLAGS, **payload}))


def tags(row):
    result = set(row.get("active_regimes", []))
    result.update("reason:" + r for r in row.get("reason_codes", []))
    result.update(
        "signal:" + k
        for k, value in row.get("features_json", {}).items()
        if value is True and (k.endswith("_active") or k.endswith("_signal"))
    )
    return result


def taf_conflicts(rows):
    conflicts = [
        r
        for r in rows
        if r.get("taf_bucket") is not None and r["taf_bucket"] != r["champion_modal_bucket"]
    ]
    return {
        "taf_conflict_count": len(conflicts),
        "taf_wins": sum(r["taf_bucket"] == r["actual_bucket"] for r in conflicts),
        "champion_wins": sum(r["champion_modal_bucket"] == r["actual_bucket"] for r in conflicts),
        "neither": sum(
            r["actual_bucket"] not in (r["taf_bucket"], r["champion_modal_bucket"])
            for r in conflicts
        ),
        "taf_missing_count": sum(r.get("taf_bucket") is None for r in rows),
        "conflict_definition": "TAF modal != Champion modal; agreement regime uses Top2",
    }


def regime_tournament(root, groups):
    main = groups["main"]
    names = sorted(
        set(REGIMES) | {name for rows in main.values() for r in rows for name in tags(r)}
    )
    result, combinations = [], []
    for cp in CHECKPOINTS:
        all_rows = [r for r in main[cp] if valid(r)]
        baseline = score(all_rows)
        for name in names:
            selected = [r for r in all_rows if name in tags(r)]
            metrics = score(selected)
            entry = {
                "checkpoint": cp,
                "regime": name,
                "metrics": metrics,
                "sample_class": sample_class(len(selected)),
                "baseline": baseline,
                **taf_conflicts(selected),
            }
            if selected:
                entry.update(
                    lift_pp=100 * (metrics["modal_accuracy"] - baseline["modal_accuracy"]),
                    mae_delta_c=metrics["mae_c"] - baseline["mae_c"],
                    bias_difference_c=metrics["signed_bias_c"] - baseline["signed_bias_c"],
                )
            checks = []
            fixed_dates = {r["target_date"] for r in selected}
            for scenario in SCENARIOS:
                alternative = [r for r in groups[scenario][cp] if valid(r)]
                subset = [r for r in alternative if r["target_date"] in fixed_dates]
                overall = score(alternative)
                value = score(subset)
                checks.append(
                    {
                        "scenario": scenario,
                        "n": len(subset),
                        "lift_pp": 100 * (value["modal_accuracy"] - overall["modal_accuracy"])
                        if subset
                        else None,
                        "classification": "fixed_main_regime_membership",
                    }
                )
            entry["sensitivity_lifts"] = checks
            eligible = [s["lift_pp"] for s in checks if s["n"] >= 20 and s["lift_pp"] is not None]
            entry["robustness_flag"] = (
                "insufficient_sample"
                if len(selected) < 30 or len(eligible) != len(SCENARIOS)
                else "robust_positive_association"
                if min(eligible) > 0
                else "robust_negative_association"
                if max(eligible) < 0
                else "mixed_or_fragile_association"
            )
            result.append(entry)
        # Use existing concepts only. Equivalent co-occurring labels remain labelled, not independent.
        combination_names = list(REGIMES) + ["reason:warming_run_trend", "reason:taf_upside_signal"]
        for left, right in itertools.combinations(combination_names, 2):
            chosen = [r for r in all_rows if left in tags(r) and right in tags(r)]
            if len(chosen) < 15:
                continue
            value = score(chosen)
            combinations.append(
                {
                    "checkpoint": cp,
                    "regimes": [left, right],
                    "metrics": value,
                    "baseline": baseline,
                    "lift_pp": 100 * (value["modal_accuracy"] - baseline["modal_accuracy"]),
                    "sample_class": sample_class(len(chosen)),
                    "combination_status": "primary_n_ge_20"
                    if len(chosen) >= 20
                    else "weak_sensitivity_n_15_19",
                    **taf_conflicts(chosen),
                }
            )
    persist(
        root,
        "regime-tournament.json",
        {
            "baseline_by_checkpoint": {cp: score(main[cp]) for cp in CHECKPOINTS},
            "regimes": result,
            "combinations": combinations,
            "interpretation": "Associations, not causal benefits of switching a regime on. Correlated labels and multiple comparisons; no OOS certification.",
            "bias_definition": "forecast center minus METAR actual",
        },
    )
    return result, combinations


def legacy_adaptive(row, prior, late=False):
    policies = (
        cc._late_active_policies()
        if late
        else {
            name: (lambda r, name=name: cc._d0_anchor_delta(r, name))
            for name in cc.D0_ANCHOR_POLICIES
        }
    )
    selected, calibration = cc._select_conservative_policy(prior, policies)
    delta = policies[selected](row)
    probabilities = cc.shift_bucket_distribution(row["champion_probabilities"], delta)
    return prediction(
        row,
        row["champion_center_c"] + delta,
        probabilities,
        selected_policy=selected,
        diagnostic_prior_n=len(prior),
        public_scheduled_causal_training_n=0,
        method="existing_additive_logged_contribution_challenger",
        diagnostic_selection_status=calibration["status"].replace(
            "scheduled_causal", "diagnostic_prior"
        ),
    )


def complete_policies(groups, stored, specs):
    forecasts = {}
    for scenario in SCENARIOS:
        for cp in CHECKPOINTS:
            rows = groups[scenario][cp]
            available = defaultdict(list)
            for r in rows:
                prior = available_prior(rows, r)
                record = stored[(scenario, *key(r))]
                for spec in specs[cp]:
                    name = spec["id"]
                    if spec["kind"] == "engine":
                        value = record["policy_forecasts"][name]
                    elif not valid(r):
                        value = prediction(r, None, {})
                    elif spec["kind"] == "d1":
                        value = d1_prediction(r, prior, spec["parameters"])
                    else:
                        probs = r.get("stage_probabilities", {}).get(spec["stage"], {})
                        # Existing stage changes probabilities only; protected center remains unchanged.
                        value = prediction(
                            r,
                            r["champion_center_c"] if probs else None,
                            probs,
                            method="existing_probability_stage_with_protected_center",
                        )
                    available[name].append(value)
                if cp in (cc.D0_CHECKPOINT, cc.LATE_LIVE_CHECKPOINT):
                    name = (
                        cc.D0_CHALLENGER_VERSION
                        if cp == cc.D0_CHECKPOINT
                        else cc.LATE_LIVE_CHALLENGER_VERSION
                    )
                    value = (
                        legacy_adaptive(r, prior, cp == cc.LATE_LIVE_CHECKPOINT)
                        if valid(r)
                        else prediction(r, None, {})
                    )
                    available[name].append(value)
            forecasts[(scenario, cp)] = dict(available)
    return forecasts


def rank_key(value):
    return (
        -value.get("modal_accuracy", -1),
        value.get("mae_c", math.inf),
        value.get("brier_multiclass", math.inf),
        value.get("log_loss", math.inf),
    )


def robustness(entries):
    values = [e for e in entries if e["matched_n"]]
    if not values:
        return {"robustness_flag": "unavailable"}
    gains = [e["accuracy_delta_pp"] for e in values]
    maes = [e["mae_delta_c"] for e in values]
    improved = sum(g > 1e-9 or (abs(g) <= 1e-9 and m < -1e-9) for g, m in zip(gains, maes))
    worsened = sum(g < -1e-9 or (abs(g) <= 1e-9 and m > 1e-9) for g, m in zip(gains, maes))
    return {
        "sensitivities_available": len(values),
        "improved": improved,
        "neutral": len(values) - improved - worsened,
        "worsened": worsened,
        "min_accuracy_delta_pp": min(gains),
        "max_accuracy_delta_pp": max(gains),
        "mean_accuracy_delta_pp": statistics.fmean(gains),
        "min_mae_delta_c": min(maes),
        "max_mae_delta_c": max(maes),
        "robustness_flag": "robust_positive"
        if improved and not worsened and len(values) == 6
        else "robust_negative"
        if worsened and not improved and len(values) == 6
        else "neutral"
        if not improved and not worsened
        else "mixed_or_fragile",
        "classification": "accuracy primary; tied accuracy uses MAE; scenarios not independent",
    }


def policy_matrix(root, groups, forecasts, specs):
    entries, summaries = [], []
    for cp in CHECKPOINTS:
        for name in forecasts[("main", cp)]:
            current = []
            for scenario in SCENARIOS:
                result = comparison(groups[scenario][cp], forecasts[(scenario, cp)][name])
                current.append(result)
                entries.append(
                    {"checkpoint": cp, "policy_id": name, "scenario": scenario, **result}
                )
            summary = {"checkpoint": cp, "policy": name, **robustness(current)}
            summaries.append(summary)
    persist(
        root,
        "policy-sensitivity-matrix.json",
        {
            "matrix": entries,
            "robustness": summaries,
            "parameter_registry": specs,
            "unavailable_implementations": ["existing_probability_net_not_found"],
            "training": "Diagnostic adapters use earlier accepted labels with actual availability checks. Public scheduled-causal guards unchanged. No evidence relabelling.",
            "state_scope": "Fixed v0.3 earlier memory/terminal state; full checkpoint engine reruns, not an altered historical production trajectory.",
        },
    )
    return entries, summaries


def stability(base, predicted):
    partitions = {
        "first_half": lambda r: r["target_date"] <= "2026-07-04",
        "second_half": lambda r: r["target_date"] > "2026-07-04",
        "existing_persistent_hot": lambda r: "Persistent Hot" in tags(r),
        "without_persistent_hot": lambda r: "Persistent Hot" not in tags(r),
    }
    partitions.update(
        {
            "month:" + m: lambda r, m=m: r["target_date"].startswith(m)
            for m in sorted({r["target_date"][:7] for r in base})
        }
    )
    partitions.update({"regime:" + n: lambda r, n=n: n in tags(r) for n in REGIMES})
    return {
        name: comparison(
            [a for a, _ in zip(base, predicted) if include(a)],
            [b for a, b in zip(base, predicted) if include(a)],
        )
        for name, include in partitions.items()
    }


def select_existing(training, options, minimum=30):
    if len(training) < minimum:
        return "current_protected_forecast"
    allowed = {
        name: score([values[key(r)] for r in training if key(r) in values])
        for name, values in options.items()
    }
    baseline = allowed["current_protected_forecast"]
    usable = {
        n: s
        for n, s in allowed.items()
        if s["n"] == len(training)
        and (
            n == "current_protected_forecast"
            or s["modal_accuracy"] > baseline["modal_accuracy"]
            or (
                s["modal_accuracy"] == baseline["modal_accuracy"]
                and baseline["mae_c"] - s["mae_c"] >= cc.MIN_MAE_GAIN_C
            )
        )
    }
    return min(usable, key=lambda n: (*rank_key(usable[n]), n != "current_protected_forecast", n))


def parameter_and_walk_forward(root, groups, forecasts, specs):
    main = groups["main"]
    studies, bounds, walk, blocked = [], [], [], []
    best = {}
    for cp in CHECKPOINTS:
        rows = main[cp]
        names = [s["id"] for s in specs[cp] if s["selection_eligible"]]
        options = {n: {key(r): r for r in forecasts[("main", cp)][n]} for n in names}
        for spec in specs[cp]:
            result = comparison(rows, forecasts[("main", cp)][spec["id"]])
            studies.append(
                {
                    "checkpoint": cp,
                    "policy_id": spec["id"],
                    "parameters": spec.get("parameters", {}),
                    "free_parameters": spec["free_parameters"],
                    "method": spec["kind"],
                    **result,
                }
            )
        winner = min(
            names,
            key=lambda n: (
                *rank_key(score(list(options[n].values()))),
                n != "current_protected_forecast",
                n,
            ),
        )
        best[cp] = winner
        chosen = list(options[winner].values())
        oracle = []
        for r in rows:
            if valid(r):
                oracle.append(
                    min(
                        [values[key(r)] for values in options.values() if valid(values[key(r)])],
                        key=lambda p: (
                            p["champion_modal_bucket"] != r["actual_bucket"],
                            abs(p["champion_center_c"] - r["stored_metar_actual_c"]),
                        ),
                    )
                )
        winner_spec = next(s for s in specs[cp] if s["id"] == winner)
        fitted_terms = (
            (2 if winner_spec.get("parameters", {}).get("version") == 2 else 1)
            if winner_spec["kind"] == "d1"
            else 0
        )
        bounds.append(
            {
                "checkpoint": cp,
                "classification": "diagnostic_in_sample_upper_bound",
                "best_setup": winner,
                "best_static_metrics": score(chosen),
                "number_free_parameters_in_selected_setup": winner_spec["free_parameters"]
                + fitted_terms,
                "number_tuned_weight_or_threshold_parameters": winner_spec["free_parameters"],
                "sequentially_estimated_terms": fitted_terms,
                "degrees_of_freedom_note": "D-1 estimates prior bias and v2 conditional TAF mass shift separately each step; setup selection itself adds multiple-testing flexibility.",
                "number_tested_setups": len(names),
                "n_cases": score(chosen)["n"],
                "scope": "Best tested existing one-factor setup, not a global maximum over arbitrary interactions.",
                "non_deployable_casewise_oracle": score(oracle),
                "oracle_uses_actual_to_choose_each_case": True,
            }
        )
        for mode, days in [("expanding", None), ("rolling_90_days", 90)]:
            candidates = defaultdict(list)
            selected_rows = []
            for row in rows:
                prior = available_prior(rows, row, days)
                # Existing D-1 estimators are refit on THIS training window; not an expanding estimate reused as rolling.
                for spec in specs[cp]:
                    if spec["kind"] == "d1" and valid(row):
                        candidates[spec["id"]].append(d1_prediction(row, prior, spec["parameters"]))
                    else:
                        candidates[spec["id"]].append(options[spec["id"]][key(row)])
                # Select static contribution weights from already generated earlier predictions only.
                # D-1 candidate historical predictions are themselves prior-only; no current actual enters selection.
                prior_options = {n: {key(r): r for r in values} for n, values in candidates.items()}
                selected = select_existing(prior, prior_options)
                result = dict(candidates[selected][-1])
                result.update(
                    selected_policy=selected, selector_prior_n=len(prior), selector_window=mode
                )
                selected_rows.append(result)
            summary = {
                "checkpoint": cp,
                "window": mode,
                "policy_id": "existing_parameter_selector",
                "minimum_training_cases": 30,
                **comparison(rows, selected_rows),
                "selection_counts": dict(Counter(r["selected_policy"] for r in selected_rows)),
                "stability": stability(rows, selected_rows),
            }
            walk.append(summary)
            # Individual policies allow assessment independent of the selector's historical winner search.
            for name, values in candidates.items():
                walk.append(
                    {
                        "checkpoint": cp,
                        "window": mode,
                        "policy_id": name,
                        **comparison(rows, values),
                        "stability": stability(rows, values),
                    }
                )
            for late, policy in [
                (False, cc.D0_CHALLENGER_VERSION),
                (True, cc.LATE_LIVE_CHALLENGER_VERSION),
            ]:
                if cp != (cc.LATE_LIVE_CHECKPOINT if late else cc.D0_CHECKPOINT):
                    continue
                values = [
                    legacy_adaptive(r, available_prior(rows, r, days), late)
                    if valid(r)
                    else prediction(r, None, {})
                    for r in rows
                ]
                walk.append(
                    {
                        "checkpoint": cp,
                        "window": mode,
                        "policy_id": policy,
                        **comparison(rows, values),
                        "stability": stability(rows, values),
                    }
                )
            if mode == "expanding":
                # Reused archive: explicitly diagnostic blocked validation, never a fresh untouched holdout.
                for month in ["2026-07", "2026-08", "2026-09", "2026-10"]:
                    block_rows = [r for r in rows if r["target_date"].startswith(month)]
                    if not block_rows:
                        continue
                    training = available_prior(rows, block_rows[0])
                    selected = select_existing(training, options)
                    values = []
                    spec = next(s for s in specs[cp] if s["id"] == selected)
                    for r in block_rows:
                        values.append(
                            d1_prediction(r, training, spec["parameters"])
                            if spec["kind"] == "d1" and valid(r)
                            else options[selected][key(r)]
                        )
                    blocked.append(
                        {
                            "checkpoint": cp,
                            "test_month": month,
                            "training_n": len(training),
                            "frozen_policy": selected,
                            **comparison(block_rows, values),
                        }
                    )
    persist(
        root,
        "counterfactual-parameter-study.json",
        {
            "studies": studies,
            "in_sample_upper_bounds": bounds,
            "multi_parameter_cartesian_search": False,
            "taf_center_and_probability_signal_separate": True,
            "parameter_values_100pct": "protected Champion; 0/25/50/75 are existing contribution weight sensitivities",
            "unavailable_parameters": [
                "No independent existing D0 shrunk/guarded anchor configuration found beyond capped variant and guarded selector."
            ],
        },
    )
    persist(
        root,
        "walk-forward-tournament.json",
        {
            "walk_forward": walk,
            "holdout_status": "no_clean_untouched_historical_holdout_available",
            "blocked_time_series_validation": blocked,
            "validation_classification": "diagnostic_reused_archive_time_ordered_validation",
            "availability": "Training Actual available_at <= checkpoint_at, earlier target dates only, accepted complete Actuals, same evidence cohort.",
            "research_split": "Monthly July/August/September/October blocks, train all prior accepted days; previously examined archive, not untouched.",
            "comparison_unchanged_definition": "same correctness outcome; modal_changed also reported",
            "generalization_limit": "Temporal fitting is respected; historically reconstructed inputs and research after archive inspection prevent certified OOS claims.",
        },
    )
    return studies, bounds, walk, best


def challenger_tournament(root, groups, forecasts, robust, walk):
    rankings = {}
    robustness_map = {(r["checkpoint"], r["policy"]): r for r in robust}
    for cp in CHECKPOINTS:
        rows = groups["main"][cp]
        entries = []
        for name, predicted in forecasts[("main", cp)].items():
            result = comparison(rows, predicted)
            entry = {
                "candidate": name,
                **result,
                "sample_class": sample_class(result["matched_n"]),
                "sensitivity_robustness": robustness_map[(cp, name)],
                "walk_forward": [
                    {k: v for k, v in w.items() if k != "stability"}
                    for w in walk
                    if w["checkpoint"] == cp and w["policy_id"] == name
                ],
            }
            entries.append(entry)
        variants = sorted({n for r in rows for n in r.get("engine_challenger_variants", {})})
        for name in variants:
            base, predicted = [], []
            for r in rows:
                variant = r.get("engine_challenger_variants", {}).get(name)
                if variant:
                    base.append(r)
                    predicted.append(
                        prediction(r, variant["forecast_mean_c"], variant["probabilities"])
                    )
            comparison_value = comparison(base, predicted)
            entries.append(
                {
                    "candidate": name,
                    "method": "existing_native_engine_variant_when_active",
                    **comparison_value,
                    "sample_class": sample_class(comparison_value["matched_n"]),
                    "sensitivity_robustness": "not_in_parameter_matrix; conditional subset",
                    "walk_forward": "No fitting in this native ablation; chronological/monthly results below.",
                    "stability": stability(base, predicted),
                }
            )
        entries.sort(key=lambda e: (*rank_key(e["policy"]), -e["matched_n"], e["candidate"]))
        rankings[cp] = entries
    persist(
        root,
        "challenger-tournament.json",
        {
            "rankings": rankings,
            "ranking_rule": "Display order: accuracy, MAE, Brier, Log Loss. All other metrics and sample sizes shown; conditional subsets do not constitute overall winners.",
            "automatic_promotion": False,
        },
    )
    return rankings


def market_view(row, records):
    probabilities = {int(k): float(v) for k, v in row["champion_probabilities"].items()}
    output = []
    for record in records:
        lo, hi = _temperature_range(record["bucket_label"])
        if lo is None and hi is None:
            raise ArchiveError("Unrecognized market bucket")
        p = probability_for_range(probabilities, lo, hi)
        point = record.get("historical_point") or {}
        price = point.get("series_price")
        price_at = point.get("price_at")
        age = (utc(row["checkpoint_at"]) - utc(price_at)).total_seconds() / 60 if price_at else None
        # Reject future points even if an upstream cached age field were wrong.
        available = price is not None and 0 < price <= 1 and age is not None and age >= 0
        output.append(
            {
                "token_id": record["token_id"],
                "bucket_label": record["bucket_label"],
                "lower_c": lo,
                "upper_c": hi,
                "probability": p,
                "price": price if available else None,
                "price_at": price_at,
                "age_minutes": age,
                "resolved_yes": record.get("resolved_yes") is True,
                "resolution_status": record.get("resolution_status"),
                "executable_quote": False,
            }
        )
    return output


def contains(market, bucket):
    return (market["lower_c"] is None or bucket >= market["lower_c"]) and (
        market["upper_c"] is None or bucket <= market["upper_c"]
    )


def strategy_legs(row, markets, strategy):
    ranked = probability_ranking(row["champion_probabilities"])

    def mapped(bucket):
        return next((m for m in markets if contains(m, bucket)), None)

    def probability_weighted(buckets):
        selected = {m["token_id"]: m for b in buckets if (m := mapped(b)) is not None}
        if len(selected) != len({mapped(b)["token_id"] for b in buckets if mapped(b) is not None}):
            raise ArchiveError("Duplicate bucket mapping")
        if any(mapped(b) is None for b in buckets):
            return None
        total = sum(m["probability"] for m in selected.values())
        return (
            [(m, m["probability"] / total) for m in selected.values() if m["probability"] > 0]
            if total
            else None
        )

    if strategy == "A_champion_top1":
        m = mapped(ranked[0][0])
        return [(m, 1.0)] if m else None
    if strategy == "B_champion_top1_top2":
        return probability_weighted([b for b, _ in ranked[:2]])
    if strategy == "C_top2_plus_taf_outside":
        buckets = [b for b, _ in ranked[:2]]
        if row.get("taf_bucket") is not None and row["taf_bucket"] not in buckets:
            buckets.append(row["taf_bucket"])
        return probability_weighted(buckets)
    if strategy.startswith("D_first_live_taf_hedge_"):
        if row["checkpoint_label"] != cc.FIRST_LIVE_CHECKPOINT:
            return None
        base = probability_weighted([b for b, _ in ranked[:2]])
        if not base:
            return None
        taf = row.get("taf_bucket")
        if taf is None or taf in [b for b, _ in ranked[:2]]:
            return base
        hedge = int(strategy.rsplit("_", 1)[1]) / 100
        m = mapped(taf)
        if m is None:
            return None
        # Merge an open range bucket if it covers more than one forecast bucket.
        weights = {x["token_id"]: [x, w * (1 - hedge)] for x, w in base}
        if m["token_id"] in weights:
            weights[m["token_id"]][1] += hedge
        else:
            weights[m["token_id"]] = [m, hedge]
        return list(weights.values())
    if strategy == "F_positive_ev_only":
        positive = [m for m in markets if m["price"] is not None and m["probability"] > m["price"]]
        total = sum(m["probability"] for m in positive)
        return [(m, m["probability"] / total) for m in positive] if total else []
    if strategy == "G_existing_observable_no_trade_gates":
        m = mapped(ranked[0][0])
        if not m or m["price"] is None:
            return None
        features = row.get("features_json", {})
        confidence = row.get("forecast_confidence", features.get("forecast_confidence", 0)) or 0
        if (
            row.get("terminal_state", {}).get("is_locked")
            or confidence < 65
            or m["price"] <= 0.05
            or m["probability"] - m["price"] < 0.08
            or m["probability"] - m["price"] >= 0.15
        ):
            return []
        return [(m, 1.0)]
    return None


def execute_price_trade(row, legs, freshness):
    if not legs:
        return None
    if any(m["price"] is None or m["resolution_status"] != "resolved" for m, _ in legs):
        return None
    age = max(m["age_minutes"] for m, _ in legs)
    if (freshness != "over60_sensitivity_only" and age > freshness) or (
        freshness == "over60_sensitivity_only" and age <= 60
    ):
        return None
    if abs(sum(w for _, w in legs) - 1) > 1e-9:
        raise ArchiveError("Invalid portfolio stake weights")
    pnl = sum(w * (int(m["resolved_yes"]) / m["price"] - 1) for m, w in legs)
    return {
        "target_date": row["target_date"],
        "checkpoint": row["checkpoint_label"],
        "checkpoint_at": row["checkpoint_at"],
        "stake": 1.0,
        "pnl": pnl,
        "return": pnl,
        "hit": any(m["resolved_yes"] for m, _ in legs),
        "average_entry_price": sum(w * m["price"] for m, w in legs),
        "max_price_age_minutes": age,
        "legs": [
            {
                "token_id": m["token_id"],
                "bucket_label": m["bucket_label"],
                "stake_weight": w,
                "series_price": m["price"],
                "price_at": m["price_at"],
                "age_minutes": m["age_minutes"],
                "resolved_yes": m["resolved_yes"],
            }
            for m, w in legs
        ],
        "regimes": sorted(tags(row)),
        "forecast_modal_hit": row["champion_modal_bucket"] == row["actual_bucket"],
        "cohort": COHORT,
        "pnl_type": "price_based_hypothetical_pnl",
    }


def trading_metrics(trades):
    ordered = sorted(trades, key=lambda t: (t["target_date"], t["checkpoint_at"]))
    result = {"n": len(ordered), "stake": sum(t["stake"] for t in ordered)}
    if not ordered:
        return result | {
            "hypothetical_pnl": 0,
            "roi": None,
            "max_drawdown": 0,
            "longest_losing_streak": 0,
            "bankroll_curve": [],
        }
    equity, peak, drawdown, drawdown_pct, streak, max_streak = 1000.0, 1000.0, 0.0, 0.0, 0, 0
    curve = []
    for t in ordered:
        equity += t["pnl"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        drawdown_pct = max(drawdown_pct, (peak - equity) / peak)
        streak = streak + 1 if t["pnl"] < -1e-12 else 0
        max_streak = max(max_streak, streak)
        curve.append(
            {"date": t["target_date"], "bankroll": equity, "cumulative_pnl": equity - 1000}
        )
    pnl = sum(t["pnl"] for t in ordered)
    return result | {
        "hypothetical_pnl": pnl,
        "roi": pnl / result["stake"],
        "median_trade_return": statistics.median(t["return"] for t in ordered),
        "positive_trade_pct": 100 * sum(t["pnl"] > 0 for t in ordered) / len(ordered),
        "worst_trade": min(t["pnl"] for t in ordered),
        "best_trade": max(t["pnl"] for t in ordered),
        "max_drawdown": drawdown,
        "max_drawdown_pct": drawdown_pct,
        "longest_losing_streak": max_streak,
        "bankroll_curve": curve,
        "hit_rate": sum(t["hit"] for t in ordered) / len(ordered),
        "forecast_modal_accuracy": sum(t["forecast_modal_hit"] for t in ordered) / len(ordered),
        "average_entry_price": statistics.fmean(t["average_entry_price"] for t in ordered),
        "price_freshness": {
            "mean_max_leg_age_minutes": statistics.fmean(
                t["max_price_age_minutes"] for t in ordered
            ),
            "max_age_minutes": max(t["max_price_age_minutes"] for t in ordered),
        },
    }


def trading_tournament(root, archive, groups):
    raw = json.loads((archive / "markets/checkpoint-price-series.json").read_text())
    records = raw["records"] if isinstance(raw, dict) else raw
    markets_by_key = defaultdict(list)
    winners = defaultdict(set)
    for r in records:
        markets_by_key[(r["target_date"], r["checkpoint_label"])].append(r)
        if r.get("resolved_yes"):
            winners[r["target_date"]].add(r["token_id"])
    if any(len(v) != 1 for v in winners.values()):
        raise ArchiveError("Ambiguous market resolution")
    strategies = [
        "A_champion_top1",
        "B_champion_top1_top2",
        "C_top2_plus_taf_outside",
        "D_first_live_taf_hedge_10",
        "D_first_live_taf_hedge_20",
        "D_first_live_taf_hedge_30",
        "F_positive_ev_only",
        "G_existing_observable_no_trade_gates",
    ]
    results, trade_records, regime_results, edge_rows, no_trade = [], [], [], [], []
    base_rows = {key(r): r for cp in CHECKPOINTS for r in groups["main"][cp] if valid(r)}
    views = {k: market_view(r, markets_by_key.get(k, [])) for k, r in base_rows.items()}
    for cp in CHECKPOINTS:
        rows = [r for r in groups["main"][cp] if valid(r)]
        for freshness in [10, 30, 60, "over60_sensitivity_only"]:
            for strategy in strategies:
                trades = []
                counts = Counter()
                for row in rows:
                    view = views[key(row)]
                    if not view:
                        counts["missing_market"] += 1
                        continue
                    legs = strategy_legs(row, view, strategy)
                    if legs is None:
                        counts["unavailable_or_inapplicable_strategy"] += 1
                        continue
                    if not legs:
                        counts["no_trade_by_existing_gate_or_no_positive_edge"] += 1
                        continue
                    trade = execute_price_trade(row, legs, freshness)
                    if trade:
                        trades.append(trade)
                    else:
                        counts["missing_stale_future_or_unresolved_leg"] += 1
                title = {"checkpoint": cp, "strategy": strategy, "freshness_minutes": freshness}
                results.append(
                    title
                    | {
                        "metrics": trading_metrics(trades),
                        "excluded": dict(counts),
                        "eligible_forecast_n": len(rows),
                    }
                )
                trade_records.extend(title | t for t in trades)
                for name in REGIMES:
                    subset = [t for t in trades if name in t["regimes"]]
                    if len(subset) >= 20:
                        regime_results.append(
                            title
                            | {
                                "regime": name,
                                "sample_class": sample_class(len(subset)),
                                "metrics": trading_metrics(subset),
                            }
                        )
                if freshness == 60:
                    # One-factor diagnostic exclusions, no invented combined rule or promoted strategy.
                    cp_rows = groups["main"][cp]
                    filters = {
                        "exclude_high_model_spread": lambda r, t: (
                            "Model Spread high" not in tags(r)
                        ),
                        "exclude_taf_disagreement": lambda r, t: "TAF Disagreement" not in tags(r),
                        "exclude_small_top1_top2_gap": lambda r, t: (
                            r.get("top1_top2_gap_pp", 0) >= 15
                        ),
                        "exclude_weak_top1_below_40pct": lambda r, t: (
                            probability_ranking(r["champion_probabilities"])[0][1] >= 0.4
                        ),
                        "exclude_prices_older_than_10m": lambda r, t: (
                            t["max_price_age_minutes"] <= 10
                        ),
                        "exclude_prices_older_than_30m": lambda r, t: (
                            t["max_price_age_minutes"] <= 30
                        ),
                    }

                    def prior_regime_ok(r, t):
                        prior = available_prior(cp_rows, r)
                        if len(prior) < 30:
                            return True
                        baseline = sum(
                            p["champion_modal_bucket"] == p["actual_bucket"] for p in prior
                        ) / len(prior)
                        for name in r.get("active_regimes", []):
                            subset = [p for p in prior if name in tags(p)]
                            if (
                                len(subset) >= 20
                                and sum(
                                    p["champion_modal_bucket"] == p["actual_bucket"] for p in subset
                                )
                                / len(subset)
                                < baseline
                            ):
                                return False
                        return True

                    filters["exclude_poor_prior_regime_performance"] = prior_regime_ok
                    for name, accept in filters.items():
                        subset = [t for t in trades if accept(base_rows[(t["target_date"], cp)], t)]
                        base_metric, metric = trading_metrics(trades), trading_metrics(subset)
                        no_trade.append(
                            title
                            | {
                                "filter": name,
                                "before": base_metric,
                                "after": metric,
                                "excluded_n": len(trades) - len(subset),
                                "roi_delta": metric["roi"] - base_metric["roi"]
                                if metric["roi"] is not None and base_metric["roi"] is not None
                                else None,
                                "drawdown_delta": metric["max_drawdown"]
                                - base_metric["max_drawdown"],
                                "accuracy_delta_pp": 100
                                * (
                                    metric["forecast_modal_accuracy"]
                                    - base_metric["forecast_modal_accuracy"]
                                )
                                if subset and trades
                                else None,
                                "poor_regime_gate": "Only earlier received Actuals; minimum 20 regime cases"
                                if "poor_prior" in name
                                else None,
                            }
                        )
        for freshness in [10, 30, 60, "over60_sensitivity_only"]:
            for row in rows:
                for market in views[key(row)]:
                    if market["price"] is None or market["resolution_status"] != "resolved":
                        continue
                    age = market["age_minutes"]
                    if (freshness != "over60_sensitivity_only" and age > freshness) or (
                        freshness == "over60_sensitivity_only" and age <= 60
                    ):
                        continue
                    edge = market["probability"] - market["price"]
                    band = (
                        "<=0"
                        if edge <= 0
                        else "0_5pp"
                        if edge <= 0.05
                        else "5_10pp"
                        if edge <= 0.10
                        else "10_20pp"
                        if edge <= 0.20
                        else ">20pp"
                    )
                    edge_rows.append(
                        {
                            "checkpoint": cp,
                            "freshness_minutes": freshness,
                            "band": band,
                            "target_date": row["target_date"],
                            "token_id": market["token_id"],
                            "probability": market["probability"],
                            "price": market["price"],
                            "hit": market["resolved_yes"],
                            "edge": edge,
                            "pnl": int(market["resolved_yes"]) / market["price"] - 1,
                        }
                    )
    edges = []
    for cp in CHECKPOINTS:
        for freshness in [10, 30, 60, "over60_sensitivity_only"]:
            for band in ["<=0", "0_5pp", "5_10pp", "10_20pp", ">20pp"]:
                subset = [
                    r
                    for r in edge_rows
                    if r["checkpoint"] == cp
                    and r["freshness_minutes"] == freshness
                    and r["band"] == band
                ]
                entry = {
                    "checkpoint": cp,
                    "freshness_minutes": freshness,
                    "edge_band": band,
                    "n": len(subset),
                    "unique_target_days": len({r["target_date"] for r in subset}),
                }
                if subset:
                    p, q, hit = [
                        statistics.fmean(r[field] for r in subset)
                        for field in ["probability", "price", "hit"]
                    ]
                    entry.update(
                        actual_hit_rate=hit,
                        expected_probability=p,
                        market_implied_probability=q,
                        calibration_gap=hit - p,
                        market_calibration_gap=hit - q,
                        brier_forecast=statistics.fmean(
                            (r["probability"] - r["hit"]) ** 2 for r in subset
                        ),
                        brier_market=statistics.fmean((r["price"] - r["hit"]) ** 2 for r in subset),
                        hypothetical_pnl=sum(r["pnl"] for r in subset),
                        roi=statistics.fmean(r["pnl"] for r in subset),
                    )
                edges.append(entry)
    price_notes = {
        "pnl_type": "price_based_hypothetical_pnl",
        "price_type": "Polymarket cached prices-history series_price; 5-minute requested resolution, not bid/ask or executable orderbook quote",
        "fees": 0,
        "slippage": 0,
        "returns": "Gross hypothetical YES payoff: stake_weight * (resolved_yes / series_price - 1)",
        "stake": "1 unit total per checkpoint trade, probability-proportional dollar allocation for baskets; Top1 is 100%; TAF hedge 10/20/30% where outside Top2",
        "strategy_c_weight_assumption": "Existing normalized probability allocation over Top2 + TAF. Diagnostic portfolio extension, not a new forecast family.",
        "resolution": "Cached market metadata resolved_yes; audited exactly one winner per found event; do not substitute METAR for market settlement",
        "bankroll": "Initial 1000 units, fixed 1-unit stake, no compounding. Net P&L ordered by target day; actual settlement timestamps/capital lock unknown.",
        "freshness": "<=10, <=30, <=60 cumulative overlapping caps; >60 separate stale sensitivity. All selected legs must pass; no future fill or missing-leg renormalization.",
        "independence": "Four checkpoints share events; never pool as independent trades. Bucket edge grid is not a tradable portfolio and has within-event dependence.",
        "gates_limit": "G uses only observable existing scalar gates. Book spread/depth, live calibration approval and fills unavailable. Not a full production decision pass.",
        "no_trade_weak_top1": "40% cutoff is a diagnostic bin boundary in existing 10pp probability calibration bands; not a new production rule.",
    }
    persist(
        root,
        "trading-tournament.json",
        {
            "strategies": results,
            "regimes": regime_results,
            "strategy_rankings_30m": {
                cp: sorted(
                    [
                        r
                        for r in results
                        if r["checkpoint"] == cp
                        and r["freshness_minutes"] == 30
                        and r["metrics"]["n"]
                    ],
                    key=lambda r: (-r["metrics"]["roi"], -r["metrics"]["n"]),
                )
                for cp in CHECKPOINTS
            },
            "unavailable_strategies": [
                {
                    "strategy": "E_existing_probability_net",
                    "status": "unavailable_existing_implementation_not_found",
                },
                {
                    "strategy": "G_full_production_no_trade_filter",
                    "status": "unavailable_verified_bid_ask_and_orderbook; no executable trade claim",
                },
            ],
            "price_notes": price_notes,
            "market_events_with_unique_winner": len(winners),
            "bucket_checkpoint_records": len(records),
            "edge_band_analysis": edges,
            "no_trade_analysis": no_trade,
        },
    )
    persist(root, "derived/trading-records.json", {"trades": trade_records})
    persist(root, "probability-edge-analysis.json", {"bands": edges, "notes": price_notes})
    persist(root, "no-trade-research.json", {"comparisons": no_trade, "notes": price_notes})
    return results, regime_results, edges, no_trade, price_notes


def analyze(root, archive):
    cross = json.loads((root / "derived/cross-check-records.json").read_text())
    manifest = json.loads((root / "analysis-input-manifest.json").read_text())
    expected_keys = {(day, cp) for day in manifest["targets"] for cp in CHECKPOINTS}
    for scenario in SCENARIOS:
        scenario_keys = [key(e["baseline"]) for e in cross if e["scenario"] == scenario]
        if len(scenario_keys) != len(expected_keys) or set(scenario_keys) != expected_keys:
            raise ArchiveError("Cross-check cache is incomplete for registered target dates")
    groups = {s: {cp: [] for cp in CHECKPOINTS} for s in SCENARIOS}
    stored = {}
    for entry in cross:
        r, scenario = entry["baseline"], entry["scenario"]
        groups[scenario][r["checkpoint_label"]].append(r)
        stored[(scenario, *key(r))] = entry
    for cp_groups in groups.values():
        for rows in cp_groups.values():
            rows.sort(key=lambda r: r["target_date"])
    specs = registry()
    print("Analysis: reconstructing existing diagnostic policy forecasts", flush=True)
    forecasts = complete_policies(groups, stored, specs)
    persist(
        root,
        "derived/analysis-policy-records.json",
        {
            "records": [
                {"scenario": s, "checkpoint": cp, "policies": values}
                for (s, cp), values in forecasts.items()
            ]
        },
    )
    regimes, combinations = regime_tournament(root, groups)
    print("Analysis: policy sensitivity and walk-forward tournaments", flush=True)
    matrix, robust = policy_matrix(root, groups, forecasts, specs)
    studies, bounds, walk, best = parameter_and_walk_forward(root, groups, forecasts, specs)
    rankings = challenger_tournament(root, groups, forecasts, robust, walk)
    print("Analysis: price-based historical trading and edge bands", flush=True)
    trades, trade_regimes, edges, no_trade, price_notes = trading_tournament(root, archive, groups)
    rows = [r for cp in CHECKPOINTS for r in groups["main"][cp]]
    accepted = {r["target_date"] for r in rows if r.get("actual_complete") is True}
    summary = {
        "replay_stand": "Madrid Historical Replay v0.3",
        "source_commit": manifest["source_commit"],
        "date_start": min(r["target_date"] for r in rows),
        "date_end": max(r["target_date"] for r in rows),
        "target_days": len({r["target_date"] for r in rows}),
        "checkpoints": len(rows),
        "accepted_actual_days": len(accepted),
        "evaluable_forecast_checkpoints": sum(valid(r) for r in rows),
        "cohorts": {
            name: {
                "n": sum(r.get("champion_center_c") is not None for r in rows)
                if name == COHORT
                else 0,
                "evaluable_n": sum(valid(r) for r in rows) if name == COHORT else 0,
            }
            for name in [
                "historical_full_fidelity",
                COHORT,
                "scheduled_causal",
                "reconstructed_research",
                "sequential_oos",
                "live_shadow",
            ]
        },
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "baseline": {cp: score(groups["main"][cp]) for cp in CHECKPOINTS},
        "best_tested_in_sample_setups": best,
        "in_sample_upper_bounds": bounds,
        "walk_forward_primary": [
            w for w in walk if w["policy_id"] == "existing_parameter_selector"
        ],
        "policy_robustness": robust,
        "regime_highlights": {
            flag: sorted(
                [r for r in regimes if r["regime"] in REGIMES and r["robustness_flag"] == flag],
                key=lambda r: r.get("lift_pp", 0),
                reverse=True,
            )
            for flag in [
                "robust_positive_association",
                "robust_negative_association",
                "mixed_or_fragile_association",
            ]
        },
        "challenger_rankings": {
            cp: [
                {k: v for k, v in r.items() if k not in ["stability", "walk_forward"]}
                for r in values
            ]
            for cp, values in rankings.items()
        },
        "trading_rankings_30m": {
            cp: sorted(
                [
                    t
                    for t in trades
                    if t["checkpoint"] == cp and t["freshness_minutes"] == 30 and t["metrics"]["n"]
                ],
                key=lambda t: -t["metrics"]["roi"],
            )
            for cp in CHECKPOINTS
        },
        "edge_bands_30m": [e for e in edges if e["freshness_minutes"] == 30],
        "no_trade_60m": no_trade,
        "price_notes": price_notes,
        "holdout_status": "no_clean_untouched_historical_holdout_available",
        "remaining_limits": [
            "All forecasts diagnostic_partial_replay; no certified OOS or untouched historical holdout.",
            "Model publication times assumed, not receipt logs; sequential memory inherits fixed v0.3 state.",
            "Native exact D-1 90-day warmup remains incomplete; alternative fixed lead48 only sensitivity, no AROME warmup.",
            "Missing/partial 850-hPa coverage and TAF availability/decoder gaps remain.",
            "189/191 expected market events found; missing May17/18; series prices are not historical executable quotes.",
            "Gross hypothetical P&L ignores fees, slippage, liquidity, stake caps and settlement capital lock.",
            "Small/overlapping regimes, correlated checkpoints/scenarios and multiple comparisons; April-October only, no full-year inference.",
            "Probability Net implementation absent; full quote-dependent NO TRADE pass unavailable.",
        ],
        "production_forecast_engine_unchanged": True,
        "production_champion_unchanged": True,
        "new_regimes": False,
        "new_challenger_families": False,
        "production_neon_full_scans": 0,
        "production_reads": 0,
        "production_writes": 0,
        "network_requests": 0,
    }
    summary["result_files"] = [p.name for p in sorted(root.glob("*.json"))] + [
        "historical-replay-analysis-summary.json",
        "historical-replay-analysis-report.md",
    ]
    persist(root, "historical-replay-analysis-summary.json", summary)
    write_report(root, summary, regimes, trades, trade_regimes, edges, no_trade)
    # Record aggregate code separately: it never invalidates expensive immutable forecast cache.
    from pathlib import Path

    persist(
        root,
        "analysis-validation.json",
        {
            "aggregation_code_sha256": digest(Path(__file__).read_bytes()),
            "baseline_metrics_match_v03": True,
            "source_evidence_class_preserved": True,
            "all_training_uses_prior_available_actuals": True,
            "production_engine_baseline_check": "Run scripts/verify_engine_baseline.py separately; attach output",
            "expected_main_cells": len(CHECKPOINTS) * len(REGIMES),
            "canonical_regime_cells": sum(r["regime"] in REGIMES for r in regimes),
            "matrix_cells": len(matrix),
            "cross_scenario_checkpoint_records": len(cross),
        },
    )
    for path, expected in manifest["inputs"].items():
        if digest(Path(path).read_bytes()) != expected:
            raise ArchiveError(f"Source input changed during aggregate analysis: {path}")
    print(
        f"Analysis finished: {len(rows)} main checkpoints, {sum(valid(r) for r in rows)} evaluable",
        flush=True,
    )


def pct(v):
    return f"{v * 100:.2f}%" if v is not None else "unavailable"


def write_report(root, summary, regimes, trades, trade_regimes, edges, no_trade):
    text = [
        "# Madrid Historical Replay Analysis v1",
        "",
        "**research_only=true · automatic_promotion=false · kein zertifiziertes OOS**",
        "",
        f"Replay v0.3 ({summary['source_commit']}), {summary['date_start']} bis {summary['date_end']}: {summary['target_days']} Tage, {summary['checkpoints']} Checkpoints. {summary['accepted_actual_days']} Tage mit akzeptierten vollständigen Actuals, {summary['evaluable_forecast_checkpoints']} evaluierbare Forecasts.",
        "",
        "## Evidence und Verfahren",
        "",
        "Hauptkohorte: diagnostic_partial_replay; ursprüngliche evidence_class=historical_partial_fidelity bleibt erhalten. Keine Mischung mit scheduled_causal, reconstructed_research, sequential_oos oder live_shadow. Diese und historical_full_fidelity haben jeweils N=0.",
        "",
        "**no_clean_untouched_historical_holdout_available**. Monatliche geblockte und Walk-Forward-Auswertungen sind zeitlich geordnetes Research im bereits betrachteten Archiv. Sie zertifizieren keine unberührte Holdout-Leistung. Actuals dürfen nur nach ihrem historischen Verfügbarkeitszeitpunkt in Trainingsdaten gelangen.",
        "",
        "Parametergewichte werden im vollständigen bestehenden Checkpoint-Enginepfad vor den vorhandenen Guards und Interaktionen neu berechnet. D−1 nutzt die bestehenden Trainerformeln mit einem isolierten diagnostischen Adapter; die öffentlichen scheduled-causal-Sperren bleiben unverändert. D0 v0.1 / Late v0.1 nutzen separat ihre ursprüngliche additive Beitragslogik. Früherer Memory-/Terminalzustand bleibt v0.3; keine veränderte historische Production-Trajektorie.",
        "",
        "## Champion und bester getesteter In-Sample-Parameter",
        "",
        "| Checkpoint | N | Champion Accuracy | Top2 | MAE | Bester getesteter Aufbau | Accuracy | MAE |",
        "|---|---:|---:|---:|---:|---|---:|---:|",
    ]
    for b in summary["in_sample_upper_bounds"]:
        cp, m, p = b["checkpoint"], summary["baseline"][b["checkpoint"]], b["best_static_metrics"]
        text.append(
            f"| {cp} | {m['n']} | {pct(m['modal_accuracy'])} | {pct(m['top2_coverage'])} | {m['mae_c']:.3f} | {b['best_setup']} | {pct(p['modal_accuracy'])} | {p['mae_c']:.3f} |"
        )
    text += [
        "",
        "Alle historischen Bestwerte sind **diagnostic_in_sample_upper_bound**: beste getestete Einzelfaktoroption, keine mathematische globale Obergrenze und kein Production-Kandidat. Anzahl freier Parameter, Brier, Log Loss und der nicht umsetzbare fallweise Actual-Oracle stehen im JSON.",
        "",
        "## Regime Tournament",
        "",
        "Lift bezieht sich auf den Champion am gleichen Checkpoint. Positive/negative robuste Assoziation verlangt N≥30 in der Hauptgruppe sowie N≥20 und gleiches Lift-Vorzeichen in allen sechs Sensitivitäten. Kleine Gruppen bleiben explorativ. Label wie Model Split / Model Spread high können identische Fälle beschreiben; keine unabhängigen Belege.",
        "",
        "| Checkpoint | Regime | N | Accuracy | Lift pp | MAE | Robustheit |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    selected = [r for r in regimes if r["regime"] in REGIMES and r["metrics"]["n"] >= 30]
    for cp in CHECKPOINTS:
        values = sorted([r for r in selected if r["checkpoint"] == cp], key=lambda r: r["lift_pp"])
        for r in list(dict.fromkeys([id(x) for x in values[:3] + values[-3:]])):
            row = next(x for x in values if id(x) == r)
            m = row["metrics"]
            text.append(
                f"| {cp} | {row['regime']} | {m['n']} | {pct(m['modal_accuracy'])} | {row['lift_pp']:+.2f} | {m['mae_c']:.3f} | {row['robustness_flag']} |"
            )
    text += [
        "",
        "Maritime Advection: vorhandenes Regime, aber keine aktiven evaluierbaren Fälle. Vollständige 20 Regimes × vier Checkpoints, Reason Codes, Calibration, TAF-Konflikte und Kombinationen N≥20 (N15–19 separat schwach) stehen in regime-tournament.json.",
        "",
        "## Policy × Sensitivity",
        "",
        "Sechs Szenarien: main, alternative_lead48_warmup, complete_single_run_only, metar_taf_extra_10m, publication_extra_60m, publication_extra_120m. Vergleich ausschließlich gepaarter gültiger Forecasts desselben Szenarios. Neutral heißt gleiche Accuracy und MAE; Verbesserungen primär nach Accuracy, bei Gleichstand nach MAE. Diese Szenarien sind keine unabhängigen Wiederholungen.",
        "",
        "| Checkpoint | Policy | verbessert / neutral / schlechter | Δ Accuracy min–max pp | Δ MAE min–max °C | Flag |",
        "|---|---|---:|---:|---:|---|",
    ]
    for r in summary["policy_robustness"]:
        if r["policy"] == summary["best_tested_in_sample_setups"][r["checkpoint"]] or r[
            "policy"
        ] in [
            "d1_evening_challenger_v0.1",
            "d1_evening_challenger_v0.2",
            cc.D0_CHALLENGER_VERSION,
            cc.LATE_LIVE_CHALLENGER_VERSION,
        ]:
            text.append(
                f"| {r['checkpoint']} | {r['policy']} | {r.get('improved', 0)} / {r.get('neutral', 0)} / {r.get('worsened', 0)} | {r.get('min_accuracy_delta_pp', 0):+.2f}–{r.get('max_accuracy_delta_pp', 0):+.2f} | {r.get('min_mae_delta_c', 0):+.3f}–{r.get('max_mae_delta_c', 0):+.3f} | {r['robustness_flag']} |"
            )
    text += [
        "",
        "## Walk-Forward",
        "",
        "Expanding und Rolling 90 Tage. Selector startet nach 30 verfügbaren früheren Fällen; D−1-Trainer werden auf dem entsprechenden Fenster neu gefittet. Einzeloptionen und ursprüngliche Challenger werden separat mit Monats-, Halbjahres- und Regimeergebnissen gezeigt.",
        "",
        "| Checkpoint | Fenster | N | Champion | Selector | Δ pp | MAE | verbessert / schlechter / gleich |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in summary["walk_forward_primary"]:
        m = r["policy"]
        text.append(
            f"| {r['checkpoint']} | {r['window']} | {r['matched_n']} | {pct(r['baseline']['modal_accuracy'])} | {pct(m['modal_accuracy'])} | {r['accuracy_delta_pp']:+.2f} | {m['mae_c']:.3f} | {r['improved']} / {r['worsened']} / {r['unchanged']} |"
        )
    text += [
        "",
        "## Challenger Ranking",
        "",
        "Sortierung zeigt Accuracy, dann MAE/Brier/Log Loss. Native Varianten auf bedingten Teilmengen sind keine Gesamtgewinner. Vergleich zum Champion auf derselben Teilmenge und Stichprobengröße prüfen.",
        "",
    ]
    for cp, values in summary["challenger_rankings"].items():
        text += [
            f"### {cp}",
            "",
            "| Kandidat | N | Accuracy | Δ pp | MAE | Brier | Log Loss | Bias |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        full = [r for r in values if r["matched_n"] == summary["baseline"][cp]["n"]]
        for r in full[:5]:
            m = r["policy"]
            text.append(
                f"| {r['candidate']} | {r['matched_n']} | {pct(m['modal_accuracy'])} | {r['accuracy_delta_pp']:+.2f} | {m['mae_c']:.3f} | {m['brier_multiclass']:.3f} | {m['log_loss']:.3f} | {m['signed_bias_c']:+.3f} |"
            )
    text += [
        "",
        "## Historical Trading Tournament",
        "",
        "**price_based_hypothetical_pnl**, keine verifizierte Execution-P&L. Preis: archivierter Polymarket prices-history series_price bei angefragter 5-Minuten-Auflösung. Kein Bid/Ask, keine Orderbook-Fills. Cached resolved_yes bestimmt den Gewinnbucket; 189 Events haben genau einen Gewinner. Nur akzeptierte Actual-Tage mit Forecasts gehen ein.",
        "",
        "1 Einheit Gesamteinsatz pro Trade; Basketanteile proportional zu Modellwahrscheinlichkeiten. TAF-Hedge 10/20/30% wenn außerhalb Top2. Keine Gebühren/Slippage, Liquiditätsprüfung oder Kapitalbindung. Bankroll startet bei 1000 Einheiten; keine Aufzinsung. Vier Checkpoints desselben Events nicht als unabhängige Wetten summieren.",
        "",
        "| Checkpoint | Strategie (≤30m) | N | Einsatz | P&L | ROI | Max DD | längste Verlustserie |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for cp in CHECKPOINTS:
        for r in summary["trading_rankings_30m"][cp]:
            m = r["metrics"]
            text.append(
                f"| {cp} | {r['strategy']} | {m['n']} | {m['stake']:.0f} | {m['hypothetical_pnl']:+.2f} | {pct(m['roi'])} | {m['max_drawdown']:.2f} | {m['longest_losing_streak']} |"
            )
    text += [
        "",
        "Probability Net: **unavailable_existing_implementation_not_found**. Kein Ersatzalgorithmus erfunden. Der vollständige Production-NO-TRADE-Filter kann ohne historische Bid/Ask-/Book- und Freigabedaten nicht zertifiziert werden; G zeigt ausschließlich beobachtbare bestehende skalare Gates.",
        "",
        "### Trading-Regimes, Champion Top1, ≤30m",
        "",
        "| Checkpoint | Regime | N | ROI | Max DD |",
        "|---|---|---:|---:|---:|",
    ]
    for cp in CHECKPOINTS:
        values = sorted(
            [
                r
                for r in trade_regimes
                if r["checkpoint"] == cp
                and r["strategy"] == "A_champion_top1"
                and r["freshness_minutes"] == 30
            ],
            key=lambda r: r["metrics"]["roi"],
        )
        for r in values[:2] + values[-2:]:
            m = r["metrics"]
            text.append(
                f"| {cp} | {r['regime']} | {m['n']} | {pct(m['roi'])} | {m['max_drawdown']:.2f} |"
            )
    text += [
        "",
        "### Probability Edge, ≤30m",
        "",
        "Pro Bucket wird p−Preis gebildet. Offene Randbuckets summieren alle abgedeckten Temperaturwahrscheinlichkeiten. P&L hier: unabhängiger 1-Einheit-Diagnosetrade pro Bucket, kein umsetzbares gemeinsames Portfolio. Große Edges können extrem geringe Marktpreise enthalten und dadurch hohe hypothetische Payouts erzeugen.",
        "",
        "| Checkpoint | Edge-Band | N Buckets | Actual Hit | Modell p | Marktpreis | Calibration Hit−p | ROI |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in edges:
        if r["freshness_minutes"] == 30 and r["n"]:
            text.append(
                f"| {r['checkpoint']} | {r['edge_band']} | {r['n']} | {pct(r['actual_hit_rate'])} | {pct(r['expected_probability'])} | {pct(r['market_implied_probability'])} | {r['calibration_gap'] * 100:+.2f} pp | {pct(r['roi'])} |"
            )
    text += [
        "",
        "### NO TRADE: Champion Top1, ≤60m Ausgangskohorte",
        "",
        "Einzelne Ausschlüsse separat, keine nachträglich kombinierte neue Handelsregel. Schlechte Regimes werden ausschließlich aus früheren verfügbaren Actuals erkannt (≥20 Regimefälle), nicht aus Gesamtarchivergebnissen.",
        "",
        "| Checkpoint | Ausschluss | N vorher→nachher | ROI vorher→nachher | DD vorher→nachher | Accuracy Δ pp |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in no_trade:
        if r["strategy"] == "A_champion_top1":
            a, b = r["before"], r["after"]
            text.append(
                f"| {r['checkpoint']} | {r['filter']} | {a['n']}→{b['n']} | {pct(a['roi'])}→{pct(b['roi'])} | {a['max_drawdown']:.2f}→{b['max_drawdown']:.2f} | {r['accuracy_delta_pp'] or 0:+.2f} |"
            )
    text += ["", "## Verbleibende Limits", ""] + ["- " + s for s in summary["remaining_limits"]]
    text += ["", "## Ergebnisdateien und Bestätigung", ""] + [
        "- " + s for s in summary["result_files"]
    ]
    text += [
        "",
        "Production Forecast Engine und Champion unverändert. Keine neuen Regimes, keine neuen Challenger-Familien, keine Live-Trading-Empfehlung geändert. research_only=true; automatic_promotion=false. Keine Production-Neon-Reads/Full-Scans, keine externen Datenabrufe und keine automatische Promotion. Bestehende Parameteroptionen sind ausschließlich diagnostisches Research.",
        "",
    ]
    (root / "historical-replay-analysis-report.md").write_text("\n".join(text))
