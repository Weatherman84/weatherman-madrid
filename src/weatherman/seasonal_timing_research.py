"""Offline, research-only evaluation of Madrid timing and seasonal challengers.

The evaluator consumes published daily-analysis exports.  It deliberately has
no database dependency and is not imported by collection, forecasting, trading,
or promotion code.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence


EVALUATION_VERSION = "madrid_seasonal_timing_shadow_v0.1"
DEFAULT_HANDOFF_REGISTRY = Path(__file__).parent / "data" / "madrid_research_handoff_cases_v1.json"
LATE_CHECKPOINT = "Late Live @16:00"
MORNING_CHECKPOINT = "D0 Morning @09:00"
ANALOG_FACTOR = "regime_memory_analog"
POST_PEAK_FACTOR = "post_peak_upper_tail"

MINIMUM_OOS_DAYS = 30
MINIMUM_MAE_GAIN_C = 0.12
MINIMUM_BRIER_GAIN = 0.003
DYNAMIC_CAPTURE_TOLERANCE_MINUTES = 5.0


def _number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _bucket(value: object) -> int | None:
    parsed = _number(value)
    return math.floor(parsed + 0.5) if parsed is not None else None


def _probabilities(value: object) -> dict[int, float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if not isinstance(value, Mapping):
        return {}
    parsed: dict[int, float] = {}
    for key, probability in value.items():
        try:
            bucket = int(float(key))
            parsed[bucket] = max(0.0, float(probability))
        except (TypeError, ValueError):
            continue
    total = sum(parsed.values())
    return {key: probability / total for key, probability in parsed.items()} if total else {}


def _modal(probabilities: object) -> int | None:
    parsed = _probabilities(probabilities)
    return min(parsed, key=lambda key: (-parsed[key], key)) if parsed else None


def _top2(probabilities: object) -> list[int]:
    parsed = _probabilities(probabilities)
    return [key for key, _ in sorted(parsed.items(), key=lambda item: (-item[1], item[0]))[:2]]


def _brier(probabilities: object, actual_bucket: int) -> float | None:
    parsed = _probabilities(probabilities)
    if not parsed:
        return None
    return sum(
        (parsed.get(bucket, 0.0) - float(bucket == actual_bucket)) ** 2
        for bucket in set(parsed) | {actual_bucket}
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_export(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("airport") != "LEMD":
        raise ValueError(f"expected LEMD export: {source}")
    if payload.get("research_only") is not True:
        raise ValueError(f"export is not marked research_only: {source}")
    provenance = {
        "path": str(source.resolve()),
        "sha256": _sha256(source),
        "generated_at": payload.get("generated_at"),
        "schema_version": payload.get("schema_version"),
        "application_version": payload.get("application_version"),
        "export_engine_version": payload.get("export_engine_version"),
        "forecast_engine_baseline": payload.get("forecast_engine_baseline"),
        "protected_forecast_baseline": payload.get("protected_forecast_baseline"),
        "window": payload.get("window"),
    }
    return payload, provenance


def load_handoff_registry(path: str | Path = DEFAULT_HANDOFF_REGISTRY) -> tuple[dict[str, Any], dict[str, Any]]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("airport") != "LEMD" or payload.get("research_only") is not True:
        raise ValueError(f"invalid LEMD research handoff registry: {source}")
    dates = [row.get("date") for row in payload.get("cases", [])]
    if any(not value for value in dates) or len(dates) != len(set(dates)):
        raise ValueError("research handoff registry contains missing or duplicate dates")
    provenance = {
        "path": str(source.resolve()),
        "sha256": _sha256(source),
        "registry_version": payload.get("registry_version"),
        "date_range": payload.get("date_range"),
        "evidence_class": payload.get("evidence_class"),
    }
    return payload, provenance


def merge_exports(exports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge overlapping export windows, preferring the newest generated export."""
    ordered = sorted(exports, key=lambda item: str(item.get("generated_at") or ""))
    actuals: dict[str, dict[str, Any]] = {}
    checkpoints: dict[tuple[str, str], dict[str, Any]] = {}
    gate: dict[str, Any] | None = None
    metadata: dict[str, Any] = {}
    for payload in ordered:
        metadata = dict(payload)
        for row in payload.get("actuals", []):
            target = row.get("target_date")
            if target and row.get("is_final_station_actual") is True:
                actuals[str(target)] = dict(row)
        for row in payload.get("checkpoints", []):
            target = row.get("target_date")
            checkpoint = row.get("checkpoint")
            if target and checkpoint:
                checkpoints[(str(target), str(checkpoint))] = dict(row)
        candidate_gate = payload.get("analog_promotion_gate")
        if isinstance(candidate_gate, Mapping):
            gate = dict(candidate_gate)
    return {
        "metadata": metadata,
        "actuals": actuals,
        "checkpoints": checkpoints,
        "analog_promotion_gate": gate or {},
    }


def _scoped_merged(
    merged: Mapping[str, Any], start_date: str | None, end_date: str | None
) -> dict[str, Any]:
    def included(target: str) -> bool:
        return (start_date is None or target >= start_date) and (
            end_date is None or target <= end_date
        )

    return {
        **merged,
        "actuals": {
            target: value for target, value in merged["actuals"].items() if included(target)
        },
        "checkpoints": {
            key: value for key, value in merged["checkpoints"].items() if included(key[0])
        },
    }


def _variant(row: Mapping[str, Any], factor: str | None) -> Mapping[str, Any] | None:
    for item in row.get("champion_and_challengers", []):
        if not isinstance(item, Mapping):
            continue
        if factor is None and item.get("variant") == "Champion":
            return item
        if factor is not None and item.get("factor") == factor:
            return item
    return None


def _season(target_date: str) -> str:
    month = int(target_date[5:7])
    if month in {12, 1, 2}:
        return "winter"
    if month in {3, 4, 5}:
        return "spring"
    if month in {6, 7, 8}:
        return "summer"
    return "autumn"


def _regime_tags(row: Mapping[str, Any]) -> list[str]:
    drivers = row.get("forecast_drivers") or {}
    tags: list[str] = []
    cloud = _number(drivers.get("observed_cloud_cover_pct"))
    dryness = _number(drivers.get("observed_dryness_c"))
    heat30 = _number(drivers.get("observed_heating_rate_30m_cph"))
    heat60 = _number(drivers.get("observed_heating_rate_60m_cph"))
    heat120 = _number(drivers.get("observed_heating_rate_120m_cph"))
    if any(
        _number(drivers.get(key)) not in {None, 0.0}
        for key in (
            "post_rain_reheating_watch",
            "post_convective_reports_48h",
            "post_convective_uncertainty_active",
        )
    ):
        tags.append("rain_or_post_rain")
    if heat30 is not None and heat60 is not None and abs(heat30) <= 0.2 and abs(heat60) <= 0.2:
        tags.append("short_rate_plateau")
    if heat120 is not None and heat120 > 0.2:
        tags.append("positive_120m_heating")
    if cloud is not None and dryness is not None and cloud <= 25.0 and dryness >= 5.0:
        tags.append("clear_dry")
    if (cloud is not None and cloud >= 75.0) or (dryness is not None and dryness <= 3.0):
        tags.append("humid_or_cloudy")
    return tags or ["other"]


def _metric_rows(rows: Sequence[Mapping[str, Any]], candidate_key: str) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    champion_errors: list[float] = []
    candidate_errors: list[float] = []
    champion_briers: list[float] = []
    candidate_briers: list[float] = []
    champion_modal_hits = candidate_modal_hits = 0
    champion_top2_hits = candidate_top2_hits = 0
    champion_center_hits = candidate_center_hits = 0
    for row in rows:
        actual_c = float(row["actual_c"])
        actual_bucket = int(row["actual_bucket"])
        champion = row["champion"]
        candidate = row[candidate_key]
        champion_errors.append(float(champion["forecast_c"]) - actual_c)
        candidate_errors.append(float(candidate["forecast_c"]) - actual_c)
        champion_probs = champion.get("bucket_probabilities")
        candidate_probs = candidate.get("bucket_probabilities")
        champion_modal_hits += _modal(champion_probs) == actual_bucket
        candidate_modal_hits += _modal(candidate_probs) == actual_bucket
        champion_top2_hits += actual_bucket in _top2(champion_probs)
        candidate_top2_hits += actual_bucket in _top2(candidate_probs)
        champion_center_hits += _bucket(champion.get("forecast_c")) == actual_bucket
        candidate_center_hits += _bucket(candidate.get("forecast_c")) == actual_bucket
        champion_brier = _brier(champion_probs, actual_bucket)
        candidate_brier = _brier(candidate_probs, actual_bucket)
        if champion_brier is not None:
            champion_briers.append(champion_brier)
        if candidate_brier is not None:
            candidate_briers.append(candidate_brier)
    n = len(rows)
    champion_mae = mean(abs(value) for value in champion_errors)
    candidate_mae = mean(abs(value) for value in candidate_errors)
    return {
        "n": n,
        "champion": {
            "mae_c": champion_mae,
            "bias_c": mean(champion_errors),
            "modal_hits": champion_modal_hits,
            "modal_hit_rate": champion_modal_hits / n,
            "top2_hits": champion_top2_hits,
            "top2_rate": champion_top2_hits / n,
            "rounded_center_hits": champion_center_hits,
            "brier": mean(champion_briers) if champion_briers else None,
        },
        "candidate": {
            "mae_c": candidate_mae,
            "bias_c": mean(candidate_errors),
            "modal_hits": candidate_modal_hits,
            "modal_hit_rate": candidate_modal_hits / n,
            "top2_hits": candidate_top2_hits,
            "top2_rate": candidate_top2_hits / n,
            "rounded_center_hits": candidate_center_hits,
            "brier": mean(candidate_briers) if candidate_briers else None,
        },
        "gains": {
            "mae_c": champion_mae - candidate_mae,
            "modal_hits": candidate_modal_hits - champion_modal_hits,
            "top2_hits": candidate_top2_hits - champion_top2_hits,
            "rounded_center_hit_rate": (candidate_center_hits - champion_center_hits) / n,
            "brier": (
                mean(champion_briers) - mean(candidate_briers)
                if champion_briers and candidate_briers
                else None
            ),
        },
    }


def _paired_variant_rows(
    merged: Mapping[str, Any], *, checkpoint: str, factor: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for (target_date, label), checkpoint_row in sorted(merged["checkpoints"].items()):
        if label != checkpoint:
            continue
        actual = merged["actuals"].get(target_date)
        champion = _variant(checkpoint_row, None)
        candidate = _variant(checkpoint_row, factor)
        actual_c = _number((actual or {}).get("stored_metar_max_c"))
        if actual_c is None or champion is None or candidate is None:
            continue
        actual_bucket = _bucket(actual_c)
        if actual_bucket is None:
            continue
        observed_max = _number(checkpoint_row.get("observed_max_c"))
        rows.append(
            {
                "target_date": target_date,
                "captured_at": checkpoint_row.get("captured_at"),
                "checkpoint": label,
                "season": _season(target_date),
                "regime_tags": _regime_tags(checkpoint_row),
                "actual_c": actual_c,
                "actual_bucket": actual_bucket,
                "observed_max_c": observed_max,
                "outcome_only_higher_bucket_after_checkpoint": (
                    actual_c > observed_max if observed_max is not None else None
                ),
                "champion": dict(champion),
                "candidate": dict(candidate),
                "evidence_class": checkpoint_row.get("evidence_class"),
                "checkpoint_status": checkpoint_row.get("checkpoint_status"),
                "freshness_status": checkpoint_row.get("freshness_status"),
                "model_counts": checkpoint_row.get("model_counts"),
            }
        )
    return rows


def _conditions_from_metrics(
    metrics: Mapping[str, Any], recent_metrics: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    n = int(metrics.get("n") or 0)
    gains = metrics.get("gains") or {}
    champion = metrics.get("champion") or {}
    candidate = metrics.get("candidate") or {}
    mae_gain = _number(gains.get("mae_c"))
    brier_gain = _number(gains.get("brier"))
    center_gain = _number(gains.get("rounded_center_hit_rate"))
    bias_delta = (
        abs(float(candidate["bias_c"])) - abs(float(champion["bias_c"]))
        if candidate.get("bias_c") is not None and champion.get("bias_c") is not None
        else None
    )
    recent_metrics = recent_metrics or {}
    recent_gains = recent_metrics.get("gains") or {}
    recent_champion = recent_metrics.get("champion") or {}
    recent_candidate = recent_metrics.get("candidate") or {}
    recent_mae_gain = _number(recent_gains.get("mae_c"))
    recent_bias_delta = (
        abs(float(recent_candidate["bias_c"])) - abs(float(recent_champion["bias_c"]))
        if recent_candidate.get("bias_c") is not None
        and recent_champion.get("bias_c") is not None
        else None
    )
    definitions = [
        ("oos_days", n, MINIMUM_OOS_DAYS, ">=", n >= MINIMUM_OOS_DAYS),
        ("mae_gain_c", mae_gain, MINIMUM_MAE_GAIN_C, ">=", mae_gain is not None and mae_gain >= MINIMUM_MAE_GAIN_C),
        ("brier_gain", brier_gain, MINIMUM_BRIER_GAIN, ">=", brier_gain is not None and brier_gain >= MINIMUM_BRIER_GAIN),
        ("rounded_center_bucket_hit_gain", center_gain, -0.02, ">=", center_gain is not None and center_gain >= -0.02),
        ("absolute_bias_guard_delta_c", bias_delta, 0.15, "<=", bias_delta is not None and bias_delta <= 0.15),
        ("recent_10_mae_gain_c", recent_mae_gain, 0.0, ">=", recent_mae_gain is not None and recent_mae_gain >= 0.0),
        (
            "recent_10_absolute_bias_guard_delta_c",
            recent_bias_delta,
            0.15,
            "<=",
            recent_bias_delta is not None and recent_bias_delta <= 0.15,
        ),
    ]
    return [
        {
            "condition": name,
            "actual": actual,
            "threshold": threshold,
            "operator": operator,
            "status": "PASS" if passed else "FAIL",
            "passed": passed,
        }
        for name, actual, threshold, operator, passed in definitions
    ]


def _subgroup_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[f"season:{row['season']}"].append(row)
        for tag in row["regime_tags"]:
            groups[f"regime:{tag}"].append(row)
    return {name: _metric_rows(values, "candidate") for name, values in sorted(groups.items())}


def _authoritative_analog_gate(
    merged: Mapping[str, Any], diagnostic_window: Mapping[str, Any]
) -> dict[str, Any]:
    gate = (
        merged.get("analog_promotion_gate", {})
        .get("gates", {})
        .get("late_live_16_00", {})
    )
    if not gate:
        return {
            "status": "NOT_EVALUABLE",
            "blockers": ["authoritative_late_analog_gate_missing"],
        }
    result = json.loads(json.dumps(gate))
    metrics = result.get("metrics", {})
    mae_gain = _number(metrics.get("mae_gain_c"))
    result["candidate"] = "Analog Memory @ fixed Late Live 16:00"
    result["evidence_version"] = merged.get("analog_promotion_gate", {}).get(
        "evaluation_version"
    )
    result["valid_evidence_start"] = merged.get("analog_promotion_gate", {}).get(
        "oos_start_date"
    )
    result["mae_gain_shortfall_c"] = (
        max(0.0, MINIMUM_MAE_GAIN_C - mae_gain) if mae_gain is not None else None
    )
    result["top2"] = {
        "status": "NOT_AVAILABLE_FOR_FULL_GATE_LINEAGE",
        "reason": "historical used_days export does not include full probability vectors or top-2 flags",
    }
    window_rows = _paired_variant_rows(
        diagnostic_window, checkpoint=LATE_CHECKPOINT, factor=ANALOG_FACTOR
    )
    result["export_window_diagnostics"] = {
        "scope": "available checkpoint rows in the requested analysis window; not the 75-day gate",
        "metrics": _metric_rows(window_rows, "candidate"),
        "subgroups": _subgroup_metrics(window_rows),
        "used_days": window_rows,
    }
    result["release_recommendation"] = (
        "READY_FOR_SEPARATE_RELEASE_REVIEW"
        if result.get("eligible") is True
        else "CONTINUE_SHADOW"
    )
    return result


def _post_peak_report(merged: Mapping[str, Any]) -> dict[str, Any]:
    rows = _paired_variant_rows(merged, checkpoint=LATE_CHECKPOINT, factor=POST_PEAK_FACTOR)
    metrics = _metric_rows(rows, "candidate")
    recent_metrics = _metric_rows(rows[-10:], "candidate")
    conditions = _conditions_from_metrics(metrics, recent_metrics)
    blockers = [item["condition"] for item in conditions if not item["passed"]]
    return {
        "candidate": "Post-Peak Upper-Tail @ fixed Late Live 16:00",
        "factor": POST_PEAK_FACTOR,
        "variant_version": "stored post_peak_upper_tail factor under exported forecast engine",
        "valid_evidence_start": rows[0]["target_date"] if rows else None,
        "evidence_days": len(rows),
        "selected_checkpoints": [
            {
                "target_date": row["target_date"],
                "checkpoint": row["checkpoint"],
                "captured_at": row["captured_at"],
            }
            for row in rows
        ],
        "metrics": metrics,
        "recent_10_metrics": recent_metrics,
        "subgroups": _subgroup_metrics(rows),
        "conditions": conditions,
        "blockers": blockers,
        "status": "ELIGIBLE" if not blockers else "NOT_ELIGIBLE",
        "release_recommendation": "READY_FOR_SEPARATE_RELEASE_REVIEW" if not blockers else "CONTINUE_SHADOW",
        "used_days": rows,
    }


def _timing_variant(merged: Mapping[str, Any], offset_minutes: int) -> dict[str, Any]:
    """Evaluate a frozen morning peak estimate against stored causal snapshots.

    Policy v1 freezes the D0 09:00 expected peak and never revises it later.  A
    stored scheduled-causal snapshot is accepted only within five minutes of the
    planned time.  Missing snapshots are not reconstructed.
    """
    selected: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    dates = sorted(merged["actuals"])
    for target_date in dates:
        morning = merged["checkpoints"].get((target_date, MORNING_CHECKPOINT))
        expected_peak = _datetime((morning or {}).get("expected_peak_at"))
        if morning is None or expected_peak is None:
            missing.append({"target_date": target_date, "reason": "morning_peak_forecast_missing"})
            continue
        planned = expected_peak - timedelta(minutes=offset_minutes)
        candidates: list[tuple[float, Mapping[str, Any]]] = []
        for (row_date, _), row in merged["checkpoints"].items():
            if row_date != target_date or row.get("checkpoint_status") != "scheduled-causal":
                continue
            captured = _datetime(row.get("captured_at"))
            if captured is None or captured > expected_peak:
                continue
            candidates.append((abs((captured - planned).total_seconds()) / 60.0, row))
        if not candidates:
            missing.append({"target_date": target_date, "reason": "no_causal_snapshot"})
            continue
        gap, row = min(candidates, key=lambda item: item[0])
        if gap > DYNAMIC_CAPTURE_TOLERANCE_MINUTES:
            missing.append(
                {
                    "target_date": target_date,
                    "reason": "no_snapshot_within_5_minutes",
                    "planned_at": planned.isoformat(),
                    "nearest_gap_minutes": gap,
                }
            )
            continue
        actual_c = _number(merged["actuals"][target_date].get("stored_metar_max_c"))
        champion = _variant(row, None)
        if actual_c is None or champion is None:
            missing.append({"target_date": target_date, "reason": "actual_or_champion_missing"})
            continue
        actual_bucket = _bucket(actual_c)
        if actual_bucket is None:
            continue
        captured = _datetime(row.get("captured_at"))
        selected.append(
            {
                "target_date": target_date,
                "morning_expected_peak_at": expected_peak.isoformat(),
                "planned_at": planned.isoformat(),
                "selected_checkpoint": row.get("checkpoint"),
                "captured_at": row.get("captured_at"),
                "selection_gap_minutes": gap,
                "lead_to_frozen_peak_minutes": (
                    (expected_peak - captured).total_seconds() / 60.0 if captured else None
                ),
                "actual_c": actual_c,
                "actual_bucket": actual_bucket,
                "champion": dict(champion),
                "candidate": dict(champion),
                "season": _season(target_date),
                "regime_tags": _regime_tags(row),
            }
        )
    metrics = _metric_rows(selected, "candidate")
    return {
        "policy_version": "morning_freeze_v1",
        "definition": f"D0 09:00 expected_peak_at minus {offset_minutes} minutes",
        "peak_revision_policy": "freeze first scheduled D0 Morning 09:00 forecast; ignore later peak revisions",
        "matching_policy": "existing scheduled-causal snapshot within 5 minutes; no reconstruction",
        "offset_minutes": offset_minutes,
        "eligible_target_days": len(dates),
        "matched_days": len(selected),
        "coverage": len(selected) / len(dates) if dates else None,
        "metrics": metrics,
        "selected_days": selected,
        "missing_days": missing,
        "status": "EVALUABLE" if len(selected) >= MINIMUM_OOS_DAYS else "FORWARD_SHADOW_REQUIRED",
        "blockers": ([] if len(selected) >= MINIMUM_OOS_DAYS else ["fewer_than_30_matched_sequential_days"]),
    }


def _fixed_late_reference(merged: Mapping[str, Any]) -> dict[str, Any]:
    rows = _paired_variant_rows(merged, checkpoint=LATE_CHECKPOINT, factor=ANALOG_FACTOR)
    # Candidate is deliberately set to Champion so this is a pure timing reference.
    reference_rows = [{**row, "candidate": row["champion"]} for row in rows]
    return {
        "definition": "unchanged fixed Late Live checkpoint 16:00 Europe/Madrid",
        "matched_days": len(reference_rows),
        "metrics": _metric_rows(reference_rows, "candidate"),
        "selected_days": [
            {"target_date": row["target_date"], "captured_at": row["captured_at"]}
            for row in reference_rows
        ],
    }


def _component_associations(merged: Mapping[str, Any]) -> dict[str, Any]:
    components = {
        "temperature_anchor_c": [],
        "clear_sky_override_c": [],
        "cloud_c": [],
        "wind_c": [],
        "dewpoint_trend_c": [],
        "late_dry_mixing_c": [],
        "heating_rate_c": [],
    }
    days: list[dict[str, Any]] = []
    for (target_date, checkpoint), row in sorted(merged["checkpoints"].items()):
        if checkpoint != LATE_CHECKPOINT or target_date not in merged["actuals"]:
            continue
        champion = _variant(row, None)
        actual_c = _number(merged["actuals"][target_date].get("stored_metar_max_c"))
        if champion is None or actual_c is None:
            continue
        error = float(champion["forecast_c"]) - actual_c
        impacts = row.get("adjustment_impacts") or {}
        values: dict[str, float] = {}
        for name in components:
            value = _number(impacts.get(name)) or 0.0
            components[name].append((value, error))
            values[name] = value
        days.append({"target_date": target_date, "champion_error_c": error, "components": values})
    summaries: dict[str, Any] = {}
    for name, pairs in components.items():
        active = [(value, error) for value, error in pairs if abs(value) > 1e-12]
        summaries[name] = {
            "n": len(pairs),
            "active_n": len(active),
            "mean_contribution_c": mean(value for value, _ in pairs) if pairs else None,
            "mean_contribution_when_active_c": mean(value for value, _ in active) if active else None,
            "mean_champion_error_when_active_c": mean(error for _, error in active) if active else None,
            "interpretation": "association_only_not_counterfactual_ablation",
        }
    stored_variants: dict[str, Any] = {}
    for factor in ("clear_sky_override", "airport_anchor_transfer"):
        factor_rows = _paired_variant_rows(merged, checkpoint=LATE_CHECKPOINT, factor=factor)
        stored_variants[factor] = {
            "n": len(factor_rows),
            "metrics": _metric_rows(factor_rows, "candidate"),
            "interpretation": (
                "exact stored challenger comparison on selected days; not a complete component "
                "ablation across the whole window"
            ),
        }
    prioritized = sorted(
        (
            {
                "component": name,
                "mean_contribution_c": values["mean_contribution_c"],
                "active_n": values["active_n"],
                "priority_basis": "positive additive contribution during the analysis window",
                "causal_status": "ablation_required",
            }
            for name, values in summaries.items()
            if (values["mean_contribution_c"] or 0.0) > 0.0
        ),
        key=lambda item: -float(item["mean_contribution_c"]),
    )
    return {
        "window_days": days,
        "component_summary": summaries,
        "prioritized_positive_contributors": prioritized,
        "stored_variant_comparisons": stored_variants,
        "limitations": [
            "subtraction is not an exact ablation because downstream TAF and conditioning can be nonlinear",
            "model radiation is not measured radiation",
            "no vertical mixing profile is present in the export",
        ],
    }


def _data_quality(merged: Mapping[str, Any]) -> dict[str, Any]:
    rows = [
        row
        for (target, _), row in merged["checkpoints"].items()
        if target in merged["actuals"]
    ]
    return {
        "settled_target_days": len(merged["actuals"]),
        "checkpoint_rows": len(rows),
        "scheduled_causal_rows": sum(row.get("checkpoint_status") == "scheduled-causal" for row in rows),
        "complete_evidence_rows": sum(row.get("evidence_class") == "complete" for row in rows),
        "reconstructed_rows": sum(bool(row.get("checkpoint_reconstructed")) for row in rows),
        "freshness_status_counts": {
            status: sum(row.get("freshness_status") == status for row in rows)
            for status in sorted({str(row.get("freshness_status")) for row in rows})
        },
        "radiation_source": "modelled_only",
        "measured_radiation_available": False,
        "vertical_mixing_profiles_available": False,
        "production_database_queried": False,
        "historical_forecasts_reconstructed": False,
    }


def _handoff_summary(registry: Mapping[str, Any]) -> dict[str, Any]:
    cases = list(registry.get("cases", []))
    evaluated = [row for row in cases if row.get("status") == "evaluated"]
    scored = [
        row
        for row in evaluated
        if _number(row.get("actual_c")) is not None and row.get("late_modal") is not None
    ]
    centered = [row for row in scored if _number(row.get("late_center_c")) is not None]
    role_counts: dict[str, int] = defaultdict(int)
    role_dates: dict[str, list[str]] = defaultdict(list)
    for row in evaluated:
        for role in row.get("roles", []):
            role_counts[str(role)] += 1
            role_dates[str(role)].append(str(row["date"]))
    modal_hits = sum(int(row["late_modal"]) == _bucket(row["actual_c"]) for row in scored)
    center_errors = [float(row["late_center_c"]) - float(row["actual_c"]) for row in centered]
    protection_roles = {
        "protect_against_early_plateau_cap",
        "protect_against_plateau_cap",
        "protect_against_wind_plateau_cap",
        "protect_against_rest_rise_cap",
    }
    protection_dates = sorted(
        {
            str(row["date"])
            for row in evaluated
            if protection_roles.intersection(row.get("roles", []))
        }
    )
    warm_error_dates = role_dates.get("late_warm_error", [])
    cold_error_dates = role_dates.get("late_cold_error", [])
    return {
        "registry_version": registry.get("registry_version"),
        "date_range": registry.get("date_range"),
        "total_calendar_records": len(cases),
        "evaluated_days": len(evaluated),
        "unavailable_days": [row["date"] for row in cases if row.get("status") != "evaluated"],
        "late_modal_scored_days": len(scored),
        "late_modal_hits": modal_hits,
        "late_modal_hit_rate": modal_hits / len(scored) if scored else None,
        "late_center_scored_days": len(centered),
        "late_center_mae_c": mean(abs(value) for value in center_errors) if center_errors else None,
        "late_center_bias_c": mean(center_errors) if center_errors else None,
        "late_center_warm_days": sum(value > 0.0 for value in center_errors),
        "late_center_cold_days": sum(value < 0.0 for value in center_errors),
        "role_counts": dict(sorted(role_counts.items())),
        "late_warm_error_dates": warm_error_dates,
        "late_cold_error_dates": cold_error_dates,
        "plateau_cap_protection_dates": protection_dates,
        "development_constraints": [
            {
                "constraint": "no_blanket_plateau_or_remaining_rise_cap",
                "supporting_counterexample_dates": protection_dates,
            },
            {
                "constraint": "late_warm_error_is_recurrent_but_not_single_cause",
                "supporting_dates": warm_error_dates,
            },
            {
                "constraint": "retain_symmetric_cold_error_protection",
                "supporting_dates": cold_error_dates,
            },
            {
                "constraint": "night_maximum_and_weather_deterioration_need_separate_regimes",
                "supporting_dates": sorted(
                    set(role_dates.get("night_maximum", []))
                    | set(role_dates.get("weather_deterioration", []))
                ),
            },
        ],
        "cases": cases,
        "limitations": registry.get("limitations", []),
    }


def build_research_report(
    exports: Sequence[Mapping[str, Any]],
    *,
    provenance: Sequence[Mapping[str, Any]] | None = None,
    analysis_start_date: str | None = None,
    analysis_end_date: str | None = None,
    handoff_registry: Mapping[str, Any] | None = None,
    handoff_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    merged = merge_exports(exports)
    diagnostic_window = _scoped_merged(merged, analysis_start_date, analysis_end_date)
    analog = _authoritative_analog_gate(merged, diagnostic_window)
    post_peak = _post_peak_report(merged)
    peak60 = _timing_variant(diagnostic_window, 60)
    peak90 = _timing_variant(diagnostic_window, 90)
    handoff_evidence = _handoff_summary(handoff_registry or {"cases": []})
    release_blockers = [
        {
            "candidate": "Late Analog",
            "blocker": "mae_gain_c",
            "actual": (analog.get("metrics") or {}).get("mae_gain_c"),
            "threshold": MINIMUM_MAE_GAIN_C,
            "shortfall": analog.get("mae_gain_shortfall_c"),
        },
        {
            "candidate": "Post-Peak Upper-Tail",
            "blocker": "sequential_oos_days",
            "actual": post_peak.get("evidence_days"),
            "threshold": MINIMUM_OOS_DAYS,
        },
        {
            "candidate": "Peak-relative timing",
            "blocker": "stored_causal_checkpoint_coverage",
            "peak_minus_60_matched_days": peak60.get("matched_days"),
            "peak_minus_90_matched_days": peak90.get("matched_days"),
            "threshold": MINIMUM_OOS_DAYS,
        },
    ]
    metadata = merged["metadata"]
    return {
        "evaluation_version": EVALUATION_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "airport": "LEMD",
        "research_only": True,
        "automatic_promotion": False,
        "automatic_execution": False,
        "production_changed": False,
        "database_migration": False,
        "betting_logic_changed": False,
        "evaluation_target": "final stored METAR maximum",
        "modal_primary": True,
        "versions": {
            "application": metadata.get("application_version"),
            "export_engine": metadata.get("export_engine_version"),
            "forecast_engine": metadata.get("forecast_engine_baseline"),
            "protected_baseline": metadata.get("protected_forecast_baseline"),
        },
        "source_exports": list(provenance or []),
        "handoff_registry_provenance": dict(handoff_provenance or {}),
        "analysis_window": {
            "start_date": analysis_start_date,
            "end_date": analysis_end_date,
            "settled_days": sorted(diagnostic_window["actuals"]),
        },
        "gate_thresholds_unchanged": {
            "minimum_oos_days": MINIMUM_OOS_DAYS,
            "minimum_mae_gain_c": MINIMUM_MAE_GAIN_C,
            "minimum_brier_gain": MINIMUM_BRIER_GAIN,
        },
        "candidates": {
            "late_analog": analog,
            "post_peak_upper_tail": post_peak,
        },
        "timing": {
            "fixed_16_00_reference": _fixed_late_reference(diagnostic_window),
            "peak_minus_60": peak60,
            "peak_minus_90": peak90,
        },
        "seasonal_component_analysis": _component_associations(diagnostic_window),
        "full_handoff_case_evidence": handoff_evidence,
        "data_quality": _data_quality(diagnostic_window),
        "release_decision": {
            "status": "NO_RELEASE_CONTINUE_SHADOW",
            "release_candidate": None,
            "leading_shadow_candidate": "Analog Memory at fixed Late Live 16:00",
            "blockers": release_blockers,
            "next_engine_version": None,
            "reason": (
                "no candidate passes unchanged gates on sufficient sequential causal evidence; "
                "the full handoff series also contains counterexamples against a blanket late cap"
            ),
            "rollback_plan": "not applicable because no production release is proposed",
        },
    }


def report_markdown(report: Mapping[str, Any]) -> str:
    analog = report["candidates"]["late_analog"]
    post_peak = report["candidates"]["post_peak_upper_tail"]
    analog_metrics = analog.get("metrics") or {}
    post_metrics = post_peak.get("metrics") or {}
    peak60 = report["timing"]["peak_minus_60"]
    peak90 = report["timing"]["peak_minus_90"]
    handoffs = report.get("full_handoff_case_evidence") or {}
    lines = [
        "# LEMD saisonale Zeitsteuerung – Research-/Shadow-Entscheidung",
        "",
        f"Auswertung: `{report['evaluation_version']}`",
        "",
        "## Entscheidung",
        "",
        "**Keine Freigabe. Alle Kandidaten bleiben im Shadow.**",
        "",
        "| Kandidat | Evidenz | Ergebnis | Exakter Blocker |",
        "|---|---:|---|---|",
        (
            "| Late Analog 16:00 | "
            f"{analog.get('oos_days', 0)} Tage | CONTINUE_SHADOW | "
            f"MAE-Gewinn {analog_metrics.get('mae_gain_c', 0):.6f} °C < 0,120000 °C "
            f"(fehlen {analog.get('mae_gain_shortfall_c', 0):.6f} °C) |"
        ),
        (
            "| Post-Peak Upper-Tail 16:00 | "
            f"{post_peak.get('evidence_days', 0)} Tage | CONTINUE_SHADOW | "
            f"{post_peak.get('evidence_days', 0)} < 30 sequenzielle OOS-Tage |"
        ),
        (
            "| Peak−60 | "
            f"{peak60.get('matched_days', 0)} passende Captures | FORWARD_SHADOW_REQUIRED | "
            "zu wenig gespeicherte kausale Checkpoints |"
        ),
        (
            "| Peak−90 | "
            f"{peak90.get('matched_days', 0)} passende Captures | FORWARD_SHADOW_REQUIRED | "
            "zu wenig gespeicherte kausale Checkpoints |"
        ),
        "",
        "## Kandidatenmetriken",
        "",
        (
            f"Late Analog: Champion-MAE {analog_metrics.get('champion_mae_c', 0):.6f} °C, "
            f"Analog-MAE {analog_metrics.get('analog_mae_c', 0):.6f} °C, "
            f"Brier-Gewinn {analog_metrics.get('brier_gain', 0):.6f}, "
            f"Modal-Treffer {analog_metrics.get('champion_modal_bucket_hits', 0)} → "
            f"{analog_metrics.get('analog_modal_bucket_hits', 0)}."
        ),
        "",
        "## Gesamte Research-Handoff-Serie",
        "",
        (
            f"Berücksichtigt sind {handoffs.get('evaluated_days', 0)} auswertbare Tage von "
            f"{(handoffs.get('date_range') or {}).get('start', '?')} bis "
            f"{(handoffs.get('date_range') or {}).get('end', '?')}. "
            f"Late traf {handoffs.get('late_modal_hits', 0)}/"
            f"{handoffs.get('late_modal_scored_days', 0)} Modal-Buckets."
        ),
        "",
        (
            f"Der Late-Center war an {handoffs.get('late_center_warm_days', 0)} von "
            f"{handoffs.get('late_center_scored_days', 0)} numerisch verfügbaren Tagen warm. "
            f"Gleichzeitig enthalten die Handoffs "
            f"{len(handoffs.get('plateau_cap_protection_dates', []))} dokumentierte Schutzfälle "
            "gegen eine pauschale Plateau-/Restanstiegsbegrenzung."
        ),
        "",
        "Daraus folgt: Die Weiterentwicklung muss konditioniert nach Pfad, Regime und "
        "verbleibendem Heizfenster erfolgen; ein allgemeiner Late-Abschlag ist nicht zulässig.",
        "",
        (
            f"Post-Peak: Modal-Treffer "
            f"{(post_metrics.get('champion') or {}).get('modal_hits', 0)} → "
            f"{(post_metrics.get('candidate') or {}).get('modal_hits', 0)}; "
            f"MAE {(post_metrics.get('champion') or {}).get('mae_c', 0):.3f} → "
            f"{(post_metrics.get('candidate') or {}).get('mae_c', 0):.3f} °C. "
            "Die Fallzahl reicht nicht für eine Freigabe."
        ),
        "",
        "## Reproduzierbarkeit und Grenzen",
        "",
        "- Peak−60/−90 friert die um 09:00 bekannte Peak-Prognose ein; spätere Revisionen werden ignoriert.",
        "- Fehlende dynamische Captures werden nicht rekonstruiert.",
        "- Strahlung ist in den Exporten modelliert, nicht gemessen; vertikale Mischungsprofile fehlen.",
        "- Komponentenwerte sind Assoziationen, keine vollständigen kausalen Ablationen.",
        "- Champion, Gate-Schwellen, Baseline, Datenbank und Wettlogik wurden nicht verändert.",
        "",
        "## Nächster prüfbarer Schritt",
        "",
        "Peak−60 und Peak−90 als fest definierte Forward-Shadow-Captures sammeln. "
        "Late Analog unverändert weiterlaufen lassen; Gate nicht absenken. "
        "Post-Peak ab seinem eigenen Evidenzbeginn bis mindestens 30 sequenziellen Fällen sammeln.",
        "",
    ]
    return "\n".join(lines)


def load_exports(paths: Iterable[str | Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payloads: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for path in paths:
        payload, source = load_export(path)
        payloads.append(payload)
        provenance.append(source)
    return payloads, provenance
