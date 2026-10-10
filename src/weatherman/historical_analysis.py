"""Offline, explicitly diagnostic analysis of existing Madrid research logic.

No database/client imports, no live decisions, and no change to public trainer guards.
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import math
import statistics
import types
from datetime import date, datetime, timedelta
from functools import lru_cache
from numbers import Number
from pathlib import Path

import pandas as pd

from . import checkpoint_challengers as cc, d1_evening_research as d1, nowcast
from .historical_archive import ArchiveError, utc
from .historical_replay import build_checkpoint, clean, metrics
from .research_tracks import active_regimes, probability_ranking

VERSION = "historical_replay_analysis_v1.0.0"
COHORT = "diagnostic_partial_replay"
SOURCE_EVIDENCE = "historical_partial_fidelity"
SCENARIOS = (
    "main",
    "alternative_lead48_warmup",
    "complete_single_run_only",
    "metar_taf_extra_10m",
    "publication_extra_60m",
    "publication_extra_120m",
)
CHECKPOINTS = (
    cc.D1_CHECKPOINT,
    cc.D0_CHECKPOINT,
    cc.FIRST_LIVE_CHECKPOINT,
    cc.LATE_LIVE_CHECKPOINT,
)
REGIMES = (
    "Stable / High Confidence",
    "Model Split",
    "Model Spread high",
    "Model Spread low",
    "Rapid Heat Ramp",
    "Temperature Plateau",
    "Continued Dry Heating",
    "Cloud / Radiation Suppression",
    "Front / Wind Shift Timing",
    "Negative Temperature Anchor",
    "TAF Agreement",
    "TAF Disagreement",
    "Bucket Boundary",
    "Clear Sky Override",
    "Late Dry Mixing",
    "Failed Convection",
    "Regional Cluster",
    "Persistent Hot",
    "Phase Anchor",
    "Maritime Advection",
)
FLAGS = {
    "research_only": True,
    "automatic_promotion": False,
    "certified_oos": False,
    "cohort": COHORT,
    "source_evidence_class": SOURCE_EVIDENCE,
}


def valid(row):
    return (
        row.get("actual_complete") is True
        and row.get("actual_bucket") is not None
        and row.get("champion_center_c") is not None
        and bool(row.get("champion_probabilities"))
    )


def score(rows):
    selected = [r for r in rows if valid(r)]
    result = metrics(selected)
    if selected:
        rankings = [probability_ranking(r["champion_probabilities"]) for r in selected]
        result["average_top1_probability"] = statistics.fmean(x[0][1] for x in rankings)
        result["average_top1_top2_gap_pp"] = statistics.fmean(
            (x[0][1] - (x[1][1] if len(x) > 1 else 0)) * 100 for x in rankings
        )
    return result


def sample_class(n):
    return (
        "exploratory_only"
        if n < 10
        else "weak_evidence"
        if n < 30
        else "moderate_evidence"
        if n < 60
        else "stronger_diagnostic_evidence"
    )


def prediction(source, center, probabilities, **metadata):
    ranked = probability_ranking(probabilities)
    return {
        k: source.get(k)
        for k in (
            "target_date",
            "checkpoint_label",
            "checkpoint_at",
            "evidence_class",
            "status",
            "stored_metar_actual_c",
            "actual_bucket",
            "actual_complete",
            "actual_available_at",
        )
    } | {
        "champion_center_c": center,
        "champion_probabilities": clean(probabilities),
        "champion_modal_bucket": ranked[0][0] if ranked else None,
        "top1_probability": ranked[0][1] if ranked else None,
        **FLAGS,
        **metadata,
    }


def registry():
    output = {
        cp: [
            {
                "id": "current_protected_forecast",
                "kind": "engine",
                "parameters": {},
                "free_parameters": 0,
                "selection_eligible": True,
            }
        ]
        for cp in CHECKPOINTS
    }
    for name, value in cc.D0_ANCHOR_POLICIES.items():
        if value == 1:
            continue
        output[cc.D0_CHECKPOINT].append(
            {
                "id": name,
                "kind": "engine",
                "parameters": {"temperature_anchor": value},
                "free_parameters": 1,
                "selection_eligible": True,
                "family": "existing_temperature_anchor",
            }
        )
    for factor in ("clear_sky_override", "taf", "taf_probability_spread"):
        for value in (0.0, 0.25, 0.5, 0.75):
            output[cc.D0_CHECKPOINT].append(
                {
                    "id": f"{factor}_{round(value * 100)}pct",
                    "kind": "engine",
                    "parameters": {factor: value},
                    "free_parameters": 1,
                    "selection_eligible": True,
                    "family": "existing_contribution_weight_sensitivity",
                }
            )
    for factor in ("clear_sky_override", "taf", "late_dry_mixing"):
        for value in (0.0, 0.25, 0.5, 0.75):
            parameters = {factor: value}
            output[cc.LATE_LIVE_CHECKPOINT].append(
                {
                    "id": f"{factor}_{round(value * 100)}pct",
                    "kind": "engine",
                    "parameters": parameters,
                    "free_parameters": 1,
                    "selection_eligible": True,
                    "family": "existing_late_single_factor_ablation",
                }
            )
    for version in (1, 2):
        output[cc.D1_CHECKPOINT].append(
            {
                "id": f"d1_evening_challenger_v0.{version}",
                "kind": "d1",
                "parameters": {"version": version},
                "free_parameters": 0,
                "selection_eligible": True,
                "diagnostic_training_adapter": True,
            }
        )
        for strength in (0, 0.25, 0.5, 0.75):
            output[cc.D1_CHECKPOINT].append(
                {
                    "id": f"d1_v{version}_bias_{round(strength * 100)}pct",
                    "kind": "d1",
                    "parameters": {"version": version, "bias_strength": strength},
                    "free_parameters": 1,
                    "selection_eligible": True,
                }
            )
    for threshold in (0.10, 0.20):
        output[cc.D1_CHECKPOINT].append(
            {
                "id": f"d1_v2_boundary_{threshold:.2f}c",
                "kind": "d1",
                "parameters": {"version": 2, "boundary_threshold": threshold},
                "free_parameters": 1,
                "selection_eligible": True,
            }
        )
    output[cc.D1_CHECKPOINT].append(
        {
            "id": "d1_v2_directional_guard_off",
            "kind": "d1",
            "parameters": {"version": 2, "directional_guard": False},
            "free_parameters": 1,
            "selection_eligible": True,
        }
    )
    for weight in (0, 0.25, 0.5, 0.75):
        output[cc.D1_CHECKPOINT].append(
            {
                "id": f"d1_v2_taf_probability_{round(weight * 100)}pct",
                "kind": "d1",
                "parameters": {"version": 2, "taf_probability_weight": weight},
                "free_parameters": 1,
                "selection_eligible": True,
            }
        )
    # These are stored existing stages, not a new probability/TAF-Net algorithm.
    output[cc.FIRST_LIVE_CHECKPOINT].append(
        {
            "id": "existing_metar_conditioned_pre_taf_stage",
            "kind": "stage",
            "stage": "METAR conditioned",
            "free_parameters": 0,
            "selection_eligible": True,
        }
    )
    return output


_RUNTIME = {"memo": {}, "capture": {}, "enabled": True}
_PREFIX_NAMES = (
    "fixed_d1_training_sample",
    "merge_complete_metar_actuals",
    "score_frame",
    "station_calibration_sample",
    "model_metrics",
    "model_weight_map",
)


def _prefix_memo(name):
    original = getattr(nowcast, name)

    def cached(*args, **kwargs):
        if not _RUNTIME["enabled"]:
            return original(*args, **kwargs)
        if name not in _RUNTIME["memo"]:
            _RUNTIME["memo"][name] = original(*args, **kwargs)
        return copy.deepcopy(_RUNTIME["memo"][name])

    return cached


@lru_cache(maxsize=128)
def analysis_engine(parameters_json):
    """Existing v0.3 hooks + a separate hook on the existing TAF spread term.

    Prefix memoization is scoped to ONE checkpoint/input scenario. Every cached
    function occurs once in the protected function and precedes all weight hooks.
    Returned values are copied; no protected module or global is patched.
    """
    params = json.loads(parameters_json)
    tree = ast.parse(inspect.getsource(nowcast.build_live_nowcast))
    function = tree.body[0]
    for name in _PREFIX_NAMES:
        calls = [
            n
            for n in ast.walk(function)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
        ]
        if len(calls) != 1:
            raise ArchiveError(f"Audited engine prefix changed: {name}")
    body = []
    hooks = 0
    for statement in function.body:
        body.append(statement)
        if not isinstance(statement, ast.Assign):
            continue
        assigned = {n.id for t in statement.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        for factor, signal in (
            ("late_dry_mixing", "late_dry_mixing_signal"),
            ("clear_sky_override", "clear_sky_signal"),
        ):
            if factor in assigned and signal in assigned and isinstance(statement.value, ast.Call):
                body.extend(
                    ast.parse(
                        f"analysis_runtime['capture'].setdefault('{factor}', {factor})\n"
                        f"{factor} *= research_parameters.get('{factor}', 1.0)\n"
                        f"if {factor} == 0: {signal} = None"
                    ).body
                )
                hooks += 1
        if "contributions" in assigned and isinstance(statement.value, ast.Dict):
            body.extend(
                ast.parse(
                    "analysis_runtime['capture'].setdefault('temperature_anchor', contributions.get('temperature_anchor', 0))\n"
                    "for factor, multiplier in research_parameters.items():\n"
                    "    if factor in contributions and factor not in {'late_dry_mixing', 'clear_sky_override'}:\n"
                    "        contributions[factor] = max(-0.10, min(0.10, contributions[factor])) if multiplier is None else contributions[factor] * multiplier"
                ).body
            )
            hooks += 1
        for variable, factor in (
            ("taf_center_adjustment", "taf"),
            ("taf_spread_addition", "taf_probability_spread"),
        ):
            if variable in assigned:
                body.extend(
                    ast.parse(
                        f"analysis_runtime['capture'].setdefault('{factor}', {variable})\n"
                        f"{variable} *= research_parameters.get('{factor}', 1.0)"
                    ).body
                )
                hooks += 1
    if hooks != 5:
        raise ArchiveError("Protected engine hook locations changed")
    function.body = body
    ast.fix_missing_locations(tree)
    namespace = {
        **vars(nowcast),
        "research_parameters": params,
        "analysis_runtime": _RUNTIME,
        **{name: _prefix_memo(name) for name in _PREFIX_NAMES},
    }
    exec(compile(tree, "<isolated_offline_analysis_engine>", "exec"), namespace)
    return namespace["build_live_nowcast"]


def prepared_builders():
    """Share immutable input preparation across full engine weight reruns."""
    namespace = dict(build_checkpoint.__globals__)
    cache = {}
    original_eligible = namespace["eligible"]
    original_daily = namespace["daily_forecasts"]

    def selected(frame, cutoff, *, policy):
        key = ("eligible", id(frame), str(cutoff), policy)
        if key not in cache:
            cache[key] = original_eligible(frame, cutoff, policy=policy)
        return cache[key].copy(deep=True)

    def daily(frame, *, include_stitched):
        key = ("daily", include_stitched)
        if key not in cache:
            cache[key] = original_daily(frame, include_stitched=include_stitched)
        return cache[key].copy(deep=True)

    namespace.update(eligible=selected, daily_forecasts=daily)
    baseline = types.FunctionType(
        build_checkpoint.__code__,
        namespace,
        build_checkpoint.__name__,
        build_checkpoint.__defaults__,
    )
    baseline.__kwdefaults__ = build_checkpoint.__kwdefaults__
    candidate_namespace = {
        **namespace,
        "enrich_nowcast_with_regime_memory": lambda value, *a, **k: value,
    }
    candidate = types.FunctionType(
        build_checkpoint.__code__,
        candidate_namespace,
        build_checkpoint.__name__,
        build_checkpoint.__defaults__,
    )
    candidate.__kwdefaults__ = build_checkpoint.__kwdefaults__
    _RUNTIME["memo"].clear()
    _RUNTIME["capture"].clear()
    return baseline, candidate


def no_parameter_effect(parameters, capture):
    for factor, multiplier in parameters.items():
        raw = float(capture.get(factor, float("nan")))
        if not math.isfinite(raw):
            return False
        changed = max(-0.10, min(0.10, raw)) if multiplier is None else raw * multiplier
        if changed != raw:
            return False
    return True


@lru_cache(maxsize=1)
def history_function():
    # Extract pure existing helpers; importing their exporter would import DB models.
    path = Path(__file__).with_name("d1_evening_export.py")
    tree = ast.parse(path.read_text())
    names = {"_safe", "_effective_time", "_empty_model_features", "model_history_features"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if len(nodes) != len(names):
        raise ArchiveError("Existing model-history helper changed")
    pure = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *nodes,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(pure)
    namespace = {
        "pd": pd,
        "statistics": statistics,
        "Number": Number,
        "date": date,
        "datetime": datetime,
    }
    exec(compile(pure, "<existing_pure_model_history>", "exec"), namespace)
    return namespace["model_history_features"]


def context(row):
    r = dict(row)
    if r.get("champion_center_c") is None:
        return r
    frame = pd.DataFrame(r.get("model_inputs", []))
    if not frame.empty:
        frame["target_date"] = pd.to_datetime(frame.target_date).dt.date
        for field in ("run_at", "model_run_at", "available_at", "fetched_at"):
            frame[field] = pd.to_datetime(frame[field], utc=True)
        frame["provenance_status"] = "diagnostic_assumed_availability"
    history = history_function()(
        frame, date.fromisoformat(r["target_date"]), utc(r["checkpoint_at"])
    )
    r["model_history"] = clean(history)
    r["consensus_run_trend_c"] = history["consensus_run_trend_c"]
    ranked = probability_ranking(r["champion_probabilities"])
    r["taf_agreement"] = (
        "unavailable"
        if r.get("taf_bucket") is None
        else "agreement"
        if r["taf_bucket"] in {b for b, _ in ranked[:2]}
        else "disagreement"
    )
    r["taf_outside_top2"] = r["taf_agreement"] == "disagreement"
    pre = probability_ranking(r.get("stage_probabilities", {}).get("METAR conditioned", {}))
    r["pre_taf_modal_bucket_c"] = pre[0][0] if pre else None
    r["taf_modal_bucket_flip"] = bool(pre and pre[0][0] != ranked[0][0])
    r["active_regimes"] = active_regimes(r)
    r["reason_codes"] = d1.d1_reason_codes(r)
    r["signed_error_c"] = (
        r["stored_metar_actual_c"] - r["champion_center_c"] if r.get("actual_complete") else None
    )
    r["cohort"] = COHORT
    return {
        k: v
        for k, v in r.items()
        if k
        not in {
            "model_inputs",
            "taf_history",
            "full_policy_forecasts",
            "regime_raw_inputs",
            "trading_shadow",
        }
    }


def available_prior(rows, current, window=None):
    cutoff = utc(current["checkpoint_at"])
    start = date.fromisoformat(current["target_date"]) - timedelta(days=window or 100000)
    return [
        r
        for r in rows
        if valid(r)
        and r.get("evidence_class") == current.get("evidence_class")
        and r["checkpoint_label"] == current["checkpoint_label"]
        and r["target_date"] < current["target_date"]
        and date.fromisoformat(r["target_date"]) >= start
        and r.get("actual_available_at")
        and utc(r["actual_available_at"]) <= cutoff
    ]


@lru_cache(maxsize=8)
def diagnostic_d1_kernel(name):
    original = getattr(d1, name)
    namespace = {**original.__globals__, "SCHEDULED_CAUSAL_EVIDENCE": SOURCE_EVIDENCE}
    result = types.FunctionType(
        original.__code__, namespace, original.__name__, original.__defaults__
    )
    result.__kwdefaults__ = original.__kwdefaults__
    return result


def d1_prediction(row, prior, params):
    version = params.get("version", 2)
    threshold = params.get("boundary_threshold", 0.15)

    def reasons(record):
        existing = [r for r in d1.d1_reason_codes(record) if r != "bucket_boundary_risk"]
        center = record.get("champion_center_c")
        if center is not None and abs((center - math.floor(center)) - 0.5) <= threshold:
            existing.append("bucket_boundary_risk")
        return existing

    current_reasons = reasons(row)
    training = [{**r, "reason_codes": reasons(r)} for r in prior]
    function = "_calibration_adjustment" if version == 1 else "_v2_calibration_adjustment"
    adjustment, calibration = diagnostic_d1_kernel(function)(training, current_reasons)
    if version == 2 and not params.get("directional_guard", True):
        raw = calibration.get("uncapped_adjustment_c", 0)
        adjustment = max(-d1.D1_V2_MAX_ADJUSTMENT_C, min(d1.D1_V2_MAX_ADJUSTMENT_C, raw))
    adjustment *= params.get("bias_strength", 1)
    probabilities = d1.shift_bucket_distribution(row["champion_probabilities"], adjustment)
    interaction = {"status": "not_applicable"}
    if version == 2:
        original = diagnostic_d1_kernel("_boundary_taf_recalibration")
        namespace = {**original.__globals__, "d1_reason_codes": reasons}
        recalibrate = types.FunctionType(original.__code__, namespace)
        updated, interaction = recalibrate(probabilities, row, training)
        weight = params.get("taf_probability_weight", 1)
        probabilities = {
            b: probabilities.get(b, 0) + weight * (updated.get(b, 0) - probabilities.get(b, 0))
            for b in set(probabilities) | set(updated)
        }
    return prediction(
        row,
        row["champion_center_c"] + adjustment,
        probabilities,
        diagnostic_prior_n=len(prior),
        public_scheduled_causal_training_n=0,
        calibration=calibration,
        probability_interaction=interaction,
    )


def comparison(base, candidate):
    pairs = [(a, b) for a, b in zip(base, candidate, strict=True) if valid(a) and valid(b)]
    a, b = [p[0] for p in pairs], [p[1] for p in pairs]
    left, right = score(a), score(b)
    result = {"baseline": left, "policy": right, "matched_n": len(pairs)}
    if not pairs:
        return result | {"status": "unavailable"}
    improved = sum(
        x["champion_modal_bucket"] != x["actual_bucket"]
        and y["champion_modal_bucket"] == y["actual_bucket"]
        for x, y in pairs
    )
    worsened = sum(
        x["champion_modal_bucket"] == x["actual_bucket"]
        and y["champion_modal_bucket"] != y["actual_bucket"]
        for x, y in pairs
    )
    return result | {
        "accuracy_delta_pp": 100 * (right["modal_accuracy"] - left["modal_accuracy"]),
        "mae_delta_c": right["mae_c"] - left["mae_c"],
        "improved": improved,
        "worsened": worsened,
        "unchanged": len(pairs) - improved - worsened,
        "modal_changed": sum(
            x["champion_modal_bucket"] != y["champion_modal_bucket"] for x, y in pairs
        ),
    }
