"""Research-only D-1 Evening challenger and evaluation helpers.

The challenger is an expanding-window historical replay.  Every adjustment is
estimated exclusively from earlier scheduled-causal D-1 cases.  This module is
not imported by the productive forecast engine or collector.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from .research_tracks import (
    AUTOMATIC_PROMOTION,
    RESEARCH_ONLY,
    SCHEDULED_CAUSAL_EVIDENCE,
    positive_temperature_bucket,
    probability_ranking,
)


D1_CHECKPOINT = "D-1 Evening @20:00"
D1_CHALLENGER_VERSION = "d1_evening_challenger_v0.1"
D1_MIN_PRIOR_CASES = 10
D1_MAX_ADJUSTMENT_C = 0.5
D1_TREND_THRESHOLD_C = 0.2
D1_SPLIT_THRESHOLD_C = 2.0


def _number(value: object) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(parsed) else float(parsed)


def _probabilities(value: object) -> dict[int, float]:
    return {bucket: probability for bucket, probability in probability_ranking(value)}


def d1_reason_codes(record: Mapping[str, Any]) -> list[str]:
    """Return explicit causal diagnostics for one D-1 checkpoint."""
    reasons: list[str] = []
    modal = positive_temperature_bucket(record.get("modal_bucket"))
    taf = positive_temperature_bucket(record.get("taf_bucket"))
    consensus_delta = _number(record.get("consensus_run_trend_c"))
    spread = _number(record.get("raw_spread_c"))
    center = _number(record.get("champion_center_c"))
    anchor = _number(record.get("temp_anchor_adjustment_c"))
    clear_sky = _number(record.get("clear_sky_override_adjustment_c"))

    if taf is not None and modal is not None:
        if taf > modal:
            reasons.append("taf_upside_signal")
        elif taf < modal:
            reasons.append("taf_downside_signal")
    if consensus_delta is not None:
        if consensus_delta >= D1_TREND_THRESHOLD_C:
            reasons.append("warming_run_trend")
        elif consensus_delta <= -D1_TREND_THRESHOLD_C:
            reasons.append("cooling_run_trend")
    if spread is not None and spread >= D1_SPLIT_THRESHOLD_C:
        reasons.append("model_split_high")
    if clear_sky is not None and clear_sky >= 0.05:
        reasons.append("clear_sky_warm_bias")
    if anchor is not None and anchor <= -0.05:
        reasons.append("negative_anchor_risk")
    if center is not None and abs((center - math.floor(center)) - 0.5) <= 0.15:
        reasons.append("bucket_boundary_risk")
    return reasons


def shift_bucket_distribution(value: object, adjustment_c: float) -> dict[int, float]:
    """Translate a discrete distribution fractionally while preserving mass."""
    source = _probabilities(value)
    if not source:
        return {}
    shift = max(-D1_MAX_ADJUSTMENT_C, min(D1_MAX_ADJUSTMENT_C, float(adjustment_c)))
    whole = math.floor(shift)
    fraction = shift - whole
    result: dict[int, float] = {}
    for bucket, probability in source.items():
        low_bucket = bucket + whole
        result[low_bucket] = result.get(low_bucket, 0.0) + probability * (1.0 - fraction)
        result[low_bucket + 1] = result.get(low_bucket + 1, 0.0) + probability * fraction
    total = sum(result.values())
    if total <= 0:
        return source
    return {bucket: probability / total for bucket, probability in sorted(result.items())}


def _calibration_adjustment(
    prior: Sequence[Mapping[str, Any]], reason_codes: Sequence[str]
) -> tuple[float, dict[str, Any]]:
    eligible = [
        item
        for item in prior
        if item.get("evidence_class") == SCHEDULED_CAUSAL_EVIDENCE
        and _number(item.get("signed_error_c")) is not None
    ]
    if len(eligible) < D1_MIN_PRIOR_CASES:
        return 0.0, {
            "status": "warmup_insufficient_prior_cases",
            "prior_n": len(eligible),
            "signal_n": 0,
        }
    global_residuals = [float(item["signed_error_c"]) for item in eligible]
    global_bias = sum(global_residuals) / len(global_residuals)
    signal_rows = [
        item
        for item in eligible
        if set(reason_codes).intersection(item.get("reason_codes", []))
    ]
    if len(signal_rows) >= D1_MIN_PRIOR_CASES and reason_codes:
        signal_bias = sum(float(item["signed_error_c"]) for item in signal_rows) / len(
            signal_rows
        )
        estimate = 0.5 * global_bias + 0.5 * signal_bias
        status = "expanding_window_global_and_signal_bias"
    else:
        signal_bias = None
        estimate = global_bias
        status = "expanding_window_global_bias"
    adjustment = max(-D1_MAX_ADJUSTMENT_C, min(D1_MAX_ADJUSTMENT_C, estimate))
    return adjustment, {
        "status": status,
        "prior_n": len(eligible),
        "signal_n": len(signal_rows),
        "global_actual_minus_champion_c": global_bias,
        "signal_actual_minus_champion_c": signal_bias,
    }


def build_d1_walk_forward_challenger(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Score D-1 records chronologically without using same-day or future Actuals."""
    ordered = sorted(records, key=lambda item: (str(item.get("target_date")), str(item.get("checkpoint_at"))))
    prior: list[dict[str, Any]] = []
    result: list[dict[str, Any]] = []
    for source in ordered:
        record = dict(source)
        reasons = d1_reason_codes(record)
        adjustment, calibration = _calibration_adjustment(prior, reasons)
        champion_center = _number(record.get("champion_center_c"))
        champion_probabilities = record.get("champion_probabilities")
        shifted = shift_bucket_distribution(champion_probabilities, adjustment)
        ranked = probability_ranking(shifted)
        challenger_center = (
            champion_center + adjustment if champion_center is not None else None
        )
        actual = _number(record.get("stored_metar_actual_c"))
        actual_bucket = positive_temperature_bucket(actual)
        champion_modal = positive_temperature_bucket(record.get("modal_bucket"))
        challenger_modal = ranked[0][0] if ranked else champion_modal
        champion_error = champion_center - actual if champion_center is not None and actual is not None else None
        challenger_error = (
            challenger_center - actual
            if challenger_center is not None and actual is not None
            else None
        )
        challenger = {
            "version": D1_CHALLENGER_VERSION,
            "status": calibration["status"],
            "center_c": challenger_center,
            "center_adjustment_c": adjustment,
            "modal_bucket": challenger_modal,
            "probabilities": shifted,
            "top1_probability": ranked[0][1] if ranked else None,
            "top2_bucket": ranked[1][0] if len(ranked) > 1 else None,
            "top2_probability": ranked[1][1] if len(ranked) > 1 else None,
            "top3_bucket": ranked[2][0] if len(ranked) > 2 else None,
            "top3_probability": ranked[2][1] if len(ranked) > 2 else None,
            "confidence": _challenger_confidence(record, calibration),
            "reason_codes": reasons or ["no_candidate_signal"],
            "calibration": calibration,
            "research_only": RESEARCH_ONLY,
            "automatic_promotion": AUTOMATIC_PROMOTION,
        }
        record.update(
            {
                "reason_codes": reasons,
                "actual_bucket": actual_bucket,
                "signed_error_c": (
                    actual - champion_center
                    if actual is not None and champion_center is not None
                    else None
                ),
                "champion_center_error_c": champion_error,
                "champion_bucket_error": (
                    champion_modal - actual_bucket
                    if champion_modal is not None and actual_bucket is not None
                    else None
                ),
                "challenger_center_error_c": challenger_error,
                "challenger_bucket_error": (
                    challenger_modal - actual_bucket
                    if challenger_modal is not None and actual_bucket is not None
                    else None
                ),
                "challenger": challenger,
            }
        )
        result.append(record)
        prior.append(record)
    return result


def _challenger_confidence(record: Mapping[str, Any], calibration: Mapping[str, Any]) -> str:
    if int(calibration.get("prior_n") or 0) < D1_MIN_PRIOR_CASES:
        return "warmup"
    if "model_split_high" in d1_reason_codes(record):
        return "low"
    gap = _number(record.get("top1_top2_gap_pp"))
    return "high" if gap is not None and gap >= 15 else "moderate"


def _distribution_scores(value: object, actual_bucket: int | None) -> tuple[float | None, float | None]:
    probabilities = _probabilities(value)
    if not probabilities or actual_bucket is None:
        return None, None
    buckets = set(probabilities) | {actual_bucket}
    brier = sum(
        (probabilities.get(bucket, 0.0) - (1.0 if bucket == actual_bucket else 0.0)) ** 2
        for bucket in buckets
    )
    actual_probability = max(1e-12, probabilities.get(actual_bucket, 0.0))
    return brier, -math.log(actual_probability)


def _metrics(records: Sequence[Mapping[str, Any]], *, challenger: bool) -> dict[str, Any]:
    rows = [row for row in records if row.get("actual_bucket") is not None]
    if not rows:
        return {"n": 0}
    prefix = "challenger" if challenger else "champion"
    modal_key = "challenger.modal_bucket" if challenger else "modal_bucket"

    def nested(row: Mapping[str, Any], key: str) -> Any:
        if "." not in key:
            return row.get(key)
        left, right = key.split(".", 1)
        value = row.get(left, {})
        return value.get(right) if isinstance(value, Mapping) else None

    modal = [positive_temperature_bucket(nested(row, modal_key)) for row in rows]
    actual = [int(row["actual_bucket"]) for row in rows]
    centers = [_number(row.get(f"{prefix}_center_error_c")) for row in rows]
    probabilities = [
        row.get("challenger", {}).get("probabilities")
        if challenger
        else row.get("champion_probabilities")
        for row in rows
    ]
    rankings = [probability_ranking(value) for value in probabilities]
    valid_errors = [value for value in centers if value is not None]
    brier_log = [_distribution_scores(value, bucket) for value, bucket in zip(probabilities, actual, strict=True)]
    briers = [item[0] for item in brier_log if item[0] is not None]
    logs = [item[1] for item in brier_log if item[1] is not None]
    top1_pairs = [
        (ranking[0][1], int(ranking[0][0] == bucket))
        for ranking, bucket in zip(rankings, actual, strict=True)
        if ranking
    ]
    return {
        "n": len(rows),
        "modal_accuracy": sum(int(candidate == bucket) for candidate, bucket in zip(modal, actual, strict=True)) / len(rows),
        "top2_coverage": sum(int(bucket in {item[0] for item in ranking[:2]}) for ranking, bucket in zip(rankings, actual, strict=True)) / len(rows),
        "top3_coverage": sum(int(bucket in {item[0] for item in ranking[:3]}) for ranking, bucket in zip(rankings, actual, strict=True)) / len(rows),
        "center_mae_c": sum(abs(value) for value in valid_errors) / len(valid_errors) if valid_errors else None,
        "center_bias_c": sum(valid_errors) / len(valid_errors) if valid_errors else None,
        "brier_score": sum(briers) / len(briers) if briers else None,
        "log_loss": sum(logs) / len(logs) if logs else None,
        "top1_calibration_gap": (
            sum(probability - outcome for probability, outcome in top1_pairs) / len(top1_pairs)
            if top1_pairs else None
        ),
        "bucket_error_distribution": dict(sorted(Counter(
            str(candidate - bucket)
            for candidate, bucket in zip(modal, actual, strict=True)
            if candidate is not None
        ).items())),
    }


def compare_d1_challenger(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    scored = [row for row in records if row.get("actual_bucket") is not None]
    improved = 0
    worsened = 0
    unchanged = 0
    for row in scored:
        champion = abs(_number(row.get("champion_center_error_c")) or 0.0)
        challenger = abs(_number(row.get("challenger_center_error_c")) or 0.0)
        if challenger < champion - 1e-12:
            improved += 1
        elif challenger > champion + 1e-12:
            worsened += 1
        else:
            unchanged += 1
    return {
        "champion": _metrics(scored, challenger=False),
        "challenger": _metrics(scored, challenger=True),
        "cases_improved": improved,
        "cases_worsened": worsened,
        "cases_unchanged": unchanged,
        "method": "expanding_window_prior_scheduled_causal_cases_only",
        "historical_replay_not_oos": True,
    }


def json_mapping(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}
