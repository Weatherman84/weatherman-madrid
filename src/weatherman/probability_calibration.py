"""Causal probability calibration for the Madrid trading display.

The production Champion remains untouched.  This module learns an ordinal
mapping from stored, scheduled Champion distributions to final Stored-METAR
buckets and validates that mapping walk-forward before it can be called a
trade probability.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from .actual_quality import settlement_grade_actuals


CALIBRATION_VERSION = "ordinal_isotonic_shrinkage_v1"
MIN_TRAIN_DAYS = 30
MIN_EVIDENCE_DAYS = 60
MIN_VALIDATION_DAYS = 30
MIN_MATCHED_PHASE_DAYS = 30
MIN_BRIER_GAIN = 0.005
MIN_ECE_GAIN = 0.0
MAX_LOG_LOSS_DELTA = 0.02
MAX_MODAL_ACCURACY_LOSS = 0.03
MAX_RECENT_BRIER_DELTA = 0.03


@dataclass(frozen=True)
class CalibrationRecord:
    target_date: date
    probabilities: dict[int, float]
    actual_bucket: int
    hours_to_peak: float | None


@dataclass(frozen=True)
class ProbabilityCalibration:
    version: str
    checkpoint: str
    cohort: str
    probabilities: dict[int, float]
    available: bool
    status: str
    blockers: tuple[str, ...]
    evidence_days: int
    matched_phase_days: int
    validation_days: int
    raw_brier: float | None
    calibrated_brier: float | None
    brier_gain: float | None
    raw_log_loss: float | None
    calibrated_log_loss: float | None
    raw_ece: float | None
    calibrated_ece: float | None
    raw_modal_accuracy: float | None
    calibrated_modal_accuracy: float | None
    recent_raw_brier: float | None
    recent_calibrated_brier: float | None


def _bucket(value: object) -> int | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return math.floor(float(parsed) + 0.5) if pd.notna(parsed) else None


def _probabilities(value: object) -> dict[int, float]:
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    parsed: dict[int, float] = {}
    for bucket, probability in payload.items():
        try:
            parsed[int(float(bucket))] = max(0.0, float(probability))
        except (TypeError, ValueError):
            continue
    total = sum(parsed.values())
    return (
        {bucket: probability / total for bucket, probability in sorted(parsed.items())}
        if total > 0
        else {}
    )


def calibration_checkpoint(
    target: date,
    captured_at: datetime,
    timezone_name: str = "Europe/Madrid",
) -> str:
    """Map the current information set to a fixed, reproducible OOS cohort."""
    local = captured_at.astimezone(ZoneInfo(timezone_name))
    if target > local.date():
        return "D−1 Evening @20:00"
    if target < local.date() or local.hour >= 16:
        return "Late Live @16:00"
    if local.hour >= 12:
        return "First Live @12:00"
    return "Morning @09:00"


def calibration_records(
    snapshots: pd.DataFrame,
    variants: pd.DataFrame,
    actuals: pd.DataFrame,
    *,
    checkpoint: str,
    before_date: date,
) -> list[CalibrationRecord]:
    """Build immutable scheduled OOS pairs from already-loaded cockpit data."""
    if snapshots.empty or variants.empty or actuals.empty:
        return []
    required_snapshot = {
        "airport",
        "target_date",
        "captured_at",
        "checkpoint_label",
        "checkpoint_status",
        "checkpoint_reconstructed",
        "hours_to_peak",
    }
    required_variant = {
        "airport",
        "target_date",
        "captured_at",
        "variant",
        "probabilities_json",
    }
    if not required_snapshot.issubset(snapshots) or not required_variant.issubset(variants):
        return []

    selected = snapshots.copy()
    selected["target_date"] = pd.to_datetime(
        selected.target_date, errors="coerce"
    ).dt.date
    selected["captured_at"] = pd.to_datetime(
        selected.captured_at, utc=True, errors="coerce"
    )
    scheduled = selected.checkpoint_status.fillna("").astype(str).str.contains(
        "scheduled", case=False
    )
    reconstructed = selected.checkpoint_reconstructed.fillna(False).astype(bool)
    selected = selected[
        selected.checkpoint_label.astype(str).eq(checkpoint)
        & selected.target_date.lt(before_date)
        & scheduled
        & ~reconstructed
    ]
    selected = selected.sort_values("captured_at").drop_duplicates(
        ["airport", "target_date", "checkpoint_label"], keep="last"
    )

    champions = variants[variants.variant.astype(str).eq("Champion")].copy()
    champions["target_date"] = pd.to_datetime(
        champions.target_date, errors="coerce"
    ).dt.date
    champions["captured_at"] = pd.to_datetime(
        champions.captured_at, utc=True, errors="coerce"
    )
    champions = champions.sort_values("captured_at").drop_duplicates(
        ["airport", "target_date", "captured_at"], keep="last"
    )
    paired = selected.merge(
        champions[
            ["airport", "target_date", "captured_at", "probabilities_json"]
        ],
        on=["airport", "target_date", "captured_at"],
        how="inner",
    )

    final_actuals = settlement_grade_actuals(actuals).copy()
    if final_actuals.empty:
        return []
    final_actuals["target_date"] = pd.to_datetime(
        final_actuals.target_date, errors="coerce"
    ).dt.date
    final_actuals["actual_bucket"] = final_actuals.max_temp_c.map(_bucket)
    final_actuals = final_actuals.dropna(subset=["actual_bucket"]).sort_values(
        "target_date"
    ).drop_duplicates(["airport", "target_date"], keep="last")
    paired = paired.merge(
        final_actuals[["airport", "target_date", "actual_bucket"]],
        on=["airport", "target_date"],
        how="inner",
    ).sort_values("target_date")

    records: list[CalibrationRecord] = []
    for row in paired.itertuples():
        probabilities = _probabilities(row.probabilities_json)
        if not probabilities:
            continue
        hours_to_peak = pd.to_numeric(row.hours_to_peak, errors="coerce")
        records.append(
            CalibrationRecord(
                target_date=row.target_date,
                probabilities=probabilities,
                actual_bucket=int(row.actual_bucket),
                hours_to_peak=(
                    float(hours_to_peak) if pd.notna(hours_to_peak) else None
                ),
            )
        )
    return records


def _training_points(
    records: Sequence[CalibrationRecord],
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for record in records:
        cumulative = 0.0
        buckets = sorted(record.probabilities)
        for bucket in buckets[:-1]:
            cumulative += record.probabilities[bucket]
            if 1e-9 < cumulative < 1 - 1e-9:
                points.append((cumulative, float(record.actual_bucket <= bucket)))
    return points


def _pava(points: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """Return monotone cumulative-probability knots without external ML deps."""
    if not points:
        return ((0.0, 0.0), (1.0, 1.0))
    grouped: list[list[float]] = []
    for x, y in sorted(points):
        if grouped and abs(grouped[-1][0] - x) < 1e-12:
            grouped[-1][1] += y
            grouped[-1][2] += 1.0
        else:
            grouped.append([x, y, 1.0])
    blocks: list[list[float]] = []
    for x, y_sum, weight in grouped:
        blocks.append([x * weight, y_sum, weight])
        while len(blocks) >= 2:
            left = blocks[-2][1] / blocks[-2][2]
            right = blocks[-1][1] / blocks[-1][2]
            if left <= right:
                break
            newer = blocks.pop()
            older = blocks.pop()
            blocks.append(
                [older[0] + newer[0], older[1] + newer[1], older[2] + newer[2]]
            )
    knots = [(0.0, 0.0)]
    knots.extend((x_sum / weight, y_sum / weight) for x_sum, y_sum, weight in blocks)
    knots.append((1.0, 1.0))
    return tuple(knots)


def _interpolate(knots: Sequence[tuple[float, float]], value: float) -> float:
    value = min(1.0, max(0.0, float(value)))
    for index in range(1, len(knots)):
        left_x, left_y = knots[index - 1]
        right_x, right_y = knots[index]
        if value <= right_x:
            if right_x <= left_x:
                return right_y
            fraction = (value - left_x) / (right_x - left_x)
            return left_y + fraction * (right_y - left_y)
    return 1.0


def calibrate_distribution(
    probabilities: Mapping[int, float],
    training: Sequence[CalibrationRecord],
    *,
    observed_floor: float | None = None,
) -> dict[int, float]:
    """Apply shrunk ordinal isotonic calibration and preserve physical floors."""
    source = _probabilities(probabilities)
    if not source or not training:
        return source
    knots = _pava(_training_points(training))
    # Predetermined shrinkage protects small cohorts from step-function overfit.
    shrink = min(0.85, len(training) / (len(training) + 40.0))
    calibrated_cdf: dict[int, float] = {}
    cumulative = 0.0
    previous = 0.0
    for bucket in sorted(source):
        cumulative += source[bucket]
        learned = _interpolate(knots, cumulative)
        value = shrink * learned + (1.0 - shrink) * cumulative
        calibrated_cdf[bucket] = max(previous, min(1.0, value))
        previous = calibrated_cdf[bucket]
    calibrated_cdf[max(calibrated_cdf)] = 1.0

    result: dict[int, float] = {}
    previous = 0.0
    for bucket, cumulative in calibrated_cdf.items():
        result[bucket] = max(0.0, cumulative - previous)
        previous = cumulative

    floor_bucket = _bucket(observed_floor)
    if floor_bucket is not None:
        result.setdefault(floor_bucket, 0.0)
        impossible = sum(
            probability for bucket, probability in result.items() if bucket < floor_bucket
        )
        result = {
            bucket: probability
            for bucket, probability in result.items()
            if bucket >= floor_bucket
        }
        result[floor_bucket] = result.get(floor_bucket, 0.0) + impossible
    total = sum(result.values())
    return {
        bucket: probability / total
        for bucket, probability in sorted(result.items())
    } if total > 0 else source


def _brier(probabilities: Mapping[int, float], actual: int) -> float:
    buckets = set(probabilities) | {actual}
    return sum(
        (float(probabilities.get(bucket, 0.0)) - float(bucket == actual)) ** 2
        for bucket in buckets
    )


def _log_loss(probabilities: Mapping[int, float], actual: int) -> float:
    return -math.log(max(1e-9, float(probabilities.get(actual, 0.0))))


def _modal_hit(probabilities: Mapping[int, float], actual: int) -> float:
    modal = max(probabilities, key=lambda bucket: (probabilities[bucket], -bucket))
    return float(modal == actual)


def _ece(samples: Sequence[tuple[dict[int, float], int]], bins: int = 5) -> float:
    if not samples:
        return math.nan
    grouped: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for probabilities, actual in samples:
        modal = max(probabilities, key=lambda bucket: (probabilities[bucket], -bucket))
        confidence = float(probabilities[modal])
        index = min(bins - 1, int(confidence * bins))
        grouped[index].append((confidence, float(modal == actual)))
    return sum(
        len(group) / len(samples)
        * abs(
            sum(item[0] for item in group) / len(group)
            - sum(item[1] for item in group) / len(group)
        )
        for group in grouped
        if group
    )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_probability_calibration(
    snapshots: pd.DataFrame,
    variants: pd.DataFrame,
    actuals: pd.DataFrame,
    *,
    current_probabilities: Mapping[int, float],
    target: date,
    captured_at: datetime,
    observed_floor: float | None,
    hours_to_peak: float | None,
    timezone_name: str = "Europe/Madrid",
) -> ProbabilityCalibration:
    checkpoint = calibration_checkpoint(target, captured_at, timezone_name)
    records = calibration_records(
        snapshots,
        variants,
        actuals,
        checkpoint=checkpoint,
        before_date=target,
    )
    current_post_peak = hours_to_peak is not None and float(hours_to_peak) < 0
    matched = [
        record
        for record in records
        if record.hours_to_peak is not None
        and (record.hours_to_peak < 0) == current_post_peak
    ]
    cohort = "post-peak" if current_post_peak else "pre/at-peak"

    raw_scores: list[float] = []
    calibrated_scores: list[float] = []
    raw_logs: list[float] = []
    calibrated_logs: list[float] = []
    raw_hits: list[float] = []
    calibrated_hits: list[float] = []
    raw_samples: list[tuple[dict[int, float], int]] = []
    calibrated_samples: list[tuple[dict[int, float], int]] = []
    # Validate on the same pre-/post-peak information regime that is being
    # displayed. A large mixed cohort must not mask a weak post-peak sample.
    for index in range(MIN_TRAIN_DAYS, len(matched)):
        training = matched[:index]
        item = matched[index]
        calibrated = calibrate_distribution(item.probabilities, training)
        raw_scores.append(_brier(item.probabilities, item.actual_bucket))
        calibrated_scores.append(_brier(calibrated, item.actual_bucket))
        raw_logs.append(_log_loss(item.probabilities, item.actual_bucket))
        calibrated_logs.append(_log_loss(calibrated, item.actual_bucket))
        raw_hits.append(_modal_hit(item.probabilities, item.actual_bucket))
        calibrated_hits.append(_modal_hit(calibrated, item.actual_bucket))
        raw_samples.append((item.probabilities, item.actual_bucket))
        calibrated_samples.append((calibrated, item.actual_bucket))

    raw_brier = _mean(raw_scores)
    calibrated_brier = _mean(calibrated_scores)
    raw_log = _mean(raw_logs)
    calibrated_log = _mean(calibrated_logs)
    raw_modal = _mean(raw_hits)
    calibrated_modal = _mean(calibrated_hits)
    raw_ece = _ece(raw_samples) if raw_samples else None
    calibrated_ece = _ece(calibrated_samples) if calibrated_samples else None
    recent_raw = _mean(raw_scores[-10:])
    recent_calibrated = _mean(calibrated_scores[-10:])
    brier_gain = (
        raw_brier - calibrated_brier
        if raw_brier is not None and calibrated_brier is not None
        else None
    )

    blockers: list[str] = []
    if len(records) < MIN_EVIDENCE_DAYS:
        blockers.append(f"evidence days {len(records)}/{MIN_EVIDENCE_DAYS}")
    if len(raw_scores) < MIN_VALIDATION_DAYS:
        blockers.append(f"walk-forward validation days {len(raw_scores)}/{MIN_VALIDATION_DAYS}")
    if len(matched) < MIN_MATCHED_PHASE_DAYS:
        blockers.append(f"matched {cohort} days {len(matched)}/{MIN_MATCHED_PHASE_DAYS}")
    if brier_gain is None or brier_gain < MIN_BRIER_GAIN:
        blockers.append(
            f"Brier gain {brier_gain:.3f}/{MIN_BRIER_GAIN:.3f}"
            if brier_gain is not None
            else "Brier gain unavailable"
        )
    if (
        raw_ece is None
        or calibrated_ece is None
        or raw_ece - calibrated_ece < MIN_ECE_GAIN
    ):
        blockers.append("calibration error did not improve")
    if (
        raw_log is None
        or calibrated_log is None
        or calibrated_log - raw_log > MAX_LOG_LOSS_DELTA
    ):
        blockers.append("log loss non-inferiority failed")
    if (
        raw_modal is None
        or calibrated_modal is None
        or raw_modal - calibrated_modal > MAX_MODAL_ACCURACY_LOSS
    ):
        blockers.append("modal accuracy stability failed")
    if (
        recent_raw is None
        or recent_calibrated is None
        or recent_calibrated - recent_raw > MAX_RECENT_BRIER_DELTA
    ):
        blockers.append("recent 10-day Brier stability failed")

    calibration_training = matched if len(matched) >= MIN_TRAIN_DAYS else records
    probabilities = calibrate_distribution(
        current_probabilities,
        calibration_training,
        observed_floor=observed_floor,
    )
    available = not blockers
    return ProbabilityCalibration(
        version=CALIBRATION_VERSION,
        checkpoint=checkpoint,
        cohort=cohort,
        probabilities=probabilities,
        available=available,
        status="CALIBRATED" if available else "PROVISIONAL CALIBRATION",
        blockers=tuple(blockers),
        evidence_days=len(records),
        matched_phase_days=len(matched),
        validation_days=len(raw_scores),
        raw_brier=raw_brier,
        calibrated_brier=calibrated_brier,
        brier_gain=brier_gain,
        raw_log_loss=raw_log,
        calibrated_log_loss=calibrated_log,
        raw_ece=raw_ece,
        calibrated_ece=calibrated_ece,
        raw_modal_accuracy=raw_modal,
        calibrated_modal_accuracy=calibrated_modal,
        recent_raw_brier=recent_raw,
        recent_calibrated_brier=recent_calibrated,
    )
