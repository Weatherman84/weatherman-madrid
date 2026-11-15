"""Safety boundary between diagnostic forecast mass and trade probabilities."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


POST_PEAK_VARIANT = "Post-Peak Upper-Tail Challenger"
TRADE_PROBABILITY_CALIBRATION_GATE_PASSED = False
POST_PEAK_PROBABILITY_GATE_PASSED = False


def _utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _probabilities(value: object) -> dict[int, float]:
    if not isinstance(value, Mapping):
        return {}
    parsed: dict[int, float] = {}
    for bucket, probability in value.items():
        try:
            parsed[int(float(bucket))] = max(0.0, float(probability))
        except (TypeError, ValueError):
            continue
    total = sum(parsed.values())
    return {
        bucket: probability / total
        for bucket, probability in parsed.items()
    } if total > 0 else {}


def _upper_tail(probabilities: Mapping[int, float], observed_max: object) -> float | None:
    try:
        maximum = float(observed_max)
    except (TypeError, ValueError):
        return None
    return sum(
        float(probability)
        for bucket, probability in probabilities.items()
        if float(bucket) > maximum
    )


@dataclass(frozen=True)
class TradeProbabilityAssessment:
    available: bool
    status: str
    explanation: str
    post_peak: bool
    minutes_since_model_peak: float | None
    champion_probabilities: dict[int, float]
    shadow_probabilities: dict[int, float]
    champion_upper_tail_probability: float | None
    shadow_upper_tail_probability: float | None
    shadow_evidence_days: int
    shadow_required_days: int


def assess_trade_probability(
    nowcast: Any,
    captured_at: datetime,
    *,
    calibration_gate_passed: bool,
    post_peak_gate_passed: bool = POST_PEAK_PROBABILITY_GATE_PASSED,
    post_peak_evidence_days: int = 5,
    post_peak_required_days: int = 30,
) -> TradeProbabilityAssessment:
    """Classify whether forecast mass may be presented as trade probability.

    The Champion distribution remains available for forecast diagnostics.  It is
    not promoted to fair odds merely because impossible lower buckets were
    removed and the remaining mass was renormalized.
    """
    captured = _utc(captured_at)
    peak = _utc(getattr(nowcast, "expected_peak_at", None))
    post_peak = captured is not None and peak is not None and captured >= peak
    minutes_since_peak = (
        (captured - peak).total_seconds() / 60.0
        if post_peak and captured is not None and peak is not None
        else None
    )
    champion = _probabilities(getattr(nowcast, "probabilities", {}))
    variants = getattr(nowcast, "challenger_variants", {}) or {}
    post_peak_variant = variants.get(POST_PEAK_VARIANT, {})
    shadow = _probabilities(
        post_peak_variant.get("probabilities", {})
        if isinstance(post_peak_variant, Mapping)
        else {}
    )
    observed_max = getattr(nowcast, "observed_max", None)
    locked = bool(getattr(getattr(nowcast, "day_status", None), "is_locked", False))

    if locked:
        status = "DAY_LOCKED"
        explanation = (
            "The daily maximum is locked. The remaining distribution is an outcome "
            "diagnostic, not a new trade-probability estimate."
        )
        available = False
    elif post_peak and not post_peak_gate_passed:
        status = "POST_PEAK_UNVALIDATED"
        explanation = (
            "The expected model peak has passed, but the Champion still retains an "
            "uncalibrated upper tail after removing impossible lower buckets. The "
            f"Post-Peak Shadow has only {post_peak_evidence_days}/{post_peak_required_days} "
            "required sequential OOS days."
        )
        available = False
    elif not calibration_gate_passed:
        status = "UNCALIBRATED"
        explanation = (
            "No empirical probability-calibration gate has passed. Champion bucket "
            "values are diagnostic model mass, not fair trade odds."
        )
        available = False
    else:
        status = "AVAILABLE"
        explanation = "The configured probability-calibration gates have passed."
        available = True

    return TradeProbabilityAssessment(
        available=available,
        status=status,
        explanation=explanation,
        post_peak=post_peak,
        minutes_since_model_peak=minutes_since_peak,
        champion_probabilities=champion,
        shadow_probabilities=shadow,
        champion_upper_tail_probability=_upper_tail(champion, observed_max),
        shadow_upper_tail_probability=_upper_tail(shadow, observed_max) if shadow else None,
        shadow_evidence_days=post_peak_evidence_days,
        shadow_required_days=post_peak_required_days,
    )
