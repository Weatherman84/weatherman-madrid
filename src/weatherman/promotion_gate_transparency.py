"""Read-only transparency for the unchanged Analog Memory promotion gate.

The module evaluates already-loaded ForecastVariantSnapshot and DailyActual rows.
It never changes the Champion, thresholds, persistence, or betting logic.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any

import pandas as pd

from .actual_quality import settlement_grade_actuals
from .regime_memory import evaluate_promotion_gate

EVALUATION_VERSION = "analog_promotion_gate_transparency_v1"
ANALOG_FACTOR = "regime_memory_analog"
MINIMUM_OOS_DAYS = 30
MINIMUM_MAE_GAIN_C = 0.12
MINIMUM_BRIER_GAIN = 0.003


def _probabilities(value: object) -> dict[int, float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if not isinstance(value, dict):
        return {}
    parsed: dict[int, float] = {}
    for bucket, probability in value.items():
        try:
            parsed[int(float(bucket))] = max(0.0, float(probability))
        except (TypeError, ValueError):
            continue
    total = sum(parsed.values())
    return {bucket: probability / total for bucket, probability in parsed.items()} if total else {}


def _modal_bucket(value: object) -> int | None:
    parsed = _probabilities(value)
    return min(parsed, key=lambda bucket: (-parsed[bucket], bucket)) if parsed else None


def _top_buckets(value: object, limit: int = 2) -> list[int]:
    parsed = _probabilities(value)
    return [
        bucket
        for bucket, _ in sorted(parsed.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _brier(value: object, actual_bucket: int) -> float | None:
    parsed = _probabilities(value)
    if not parsed:
        return None
    return float(
        sum(
            (parsed.get(bucket, 0.0) - float(bucket == actual_bucket)) ** 2
            for bucket in set(parsed) | {actual_bucket}
        )
    )


def _bucket(value: object) -> int | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return math.floor(float(parsed) + 0.5) if pd.notna(parsed) else None


def _condition(
    name: str,
    actual: float | int | None,
    threshold: float | int,
    operator: str,
    passed: bool,
    basis: str,
) -> dict[str, Any]:
    return {
        "condition": name,
        "actual": actual,
        "threshold": threshold,
        "operator": operator,
        "status": "PASS" if passed else "FAIL",
        "passed": bool(passed),
        "basis": basis,
    }


def _prepared_variants(
    variants: pd.DataFrame,
    *,
    timezone_name: str,
    checkpoint_local_time: str | None,
    oos_start_date: date | str | None,
) -> pd.DataFrame:
    required = {
        "airport",
        "target_date",
        "captured_at",
        "timing",
        "variant",
        "factor",
        "forecast_c",
        "probabilities_json",
    }
    if variants.empty or not required.issubset(variants.columns):
        return pd.DataFrame(columns=sorted(required))
    frame = variants.copy()
    frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    if oos_start_date:
        start = pd.to_datetime(oos_start_date, errors="coerce")
        if not pd.isna(start):
            frame = frame[frame.target_date >= start.date()]
    # Match the protected gate's `_timing_group` behaviour without changing it:
    # everything except D-1/earlier and morning evidence belongs to D0 live.
    timing = frame.timing.fillna("").astype(str).str.casefold()
    frame = frame[~timing.str.contains("d-1|earlier", regex=True) & ~timing.str.contains("morning")]
    if checkpoint_local_time:
        hour, minute = (int(part) for part in checkpoint_local_time.split(":", 1))
        local = frame.captured_at.dt.tz_convert(timezone_name)
        frame = frame[
            (local.dt.date == frame.target_date)
            & (local.dt.hour == hour)
            & (local.dt.minute == minute)
        ]
    return frame


def _paired_evidence(frame: pd.DataFrame, actuals: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or actuals.empty:
        return pd.DataFrame()
    champion = frame[frame.variant == "Champion"]
    challenger = frame[frame.factor == ANALOG_FACTOR]
    if champion.empty or challenger.empty:
        return pd.DataFrame()
    paired = challenger.merge(
        champion[
            [
                "airport",
                "target_date",
                "captured_at",
                "forecast_c",
                "probabilities_json",
            ]
        ],
        on=["airport", "target_date", "captured_at"],
        suffixes=("_challenger", "_champion"),
    )
    actual = settlement_grade_actuals(actuals)
    if actual.empty:
        return pd.DataFrame()
    actual = actual[["airport", "target_date", "max_temp_c"]].copy()
    actual["target_date"] = pd.to_datetime(actual.target_date, errors="coerce").dt.date
    actual["max_temp_c"] = pd.to_numeric(actual.max_temp_c, errors="coerce")
    paired = paired.merge(actual, on=["airport", "target_date"], how="inner")
    paired["forecast_c_champion"] = pd.to_numeric(paired.forecast_c_champion, errors="coerce")
    paired["forecast_c_challenger"] = pd.to_numeric(paired.forecast_c_challenger, errors="coerce")
    return (
        paired.dropna(
            subset=[
                "max_temp_c",
                "forecast_c_champion",
                "forecast_c_challenger",
            ]
        )
        .sort_values("captured_at")
        .drop_duplicates(["airport", "target_date"], keep="last")
    )


def _gate_report(
    variants: pd.DataFrame,
    actuals: pd.DataFrame,
    *,
    scope: str,
    timezone_name: str,
    checkpoint_local_time: str | None,
    oos_start_date: date | str | None,
) -> dict[str, Any]:
    frame = _prepared_variants(
        variants,
        timezone_name=timezone_name,
        checkpoint_local_time=checkpoint_local_time,
        oos_start_date=oos_start_date,
    )
    gate = evaluate_promotion_gate(
        frame,
        actuals,
        timing_group="D0 live",
        minimum_oos_days=MINIMUM_OOS_DAYS,
        minimum_mae_gain_c=MINIMUM_MAE_GAIN_C,
        minimum_brier_gain=MINIMUM_BRIER_GAIN,
        oos_start_date=oos_start_date,
    )
    paired = _paired_evidence(frame, actuals)
    recent = paired.tail(min(10, len(paired))).copy()
    if paired.empty:
        recent_mae_gain = None
        recent_bias_delta = None
        modal_champion_hits = modal_challenger_hits = 0
        center_champion_hits = center_challenger_hits = 0
        top2_champion_hits = top2_challenger_hits = 0
    else:
        recent_champion_error = recent.forecast_c_champion - recent.max_temp_c
        recent_challenger_error = recent.forecast_c_challenger - recent.max_temp_c
        recent_mae_gain = float(
            recent_champion_error.abs().mean() - recent_challenger_error.abs().mean()
        )
        recent_bias_delta = float(
            abs(recent_challenger_error.mean()) - abs(recent_champion_error.mean())
        )
        actual_buckets = paired.max_temp_c.map(_bucket)
        champion_centers = paired.forecast_c_champion.map(_bucket)
        challenger_centers = paired.forecast_c_challenger.map(_bucket)
        champion_modals = paired.probabilities_json_champion.map(_modal_bucket)
        challenger_modals = paired.probabilities_json_challenger.map(_modal_bucket)
        center_champion_hits = int((champion_centers == actual_buckets).sum())
        center_challenger_hits = int((challenger_centers == actual_buckets).sum())
        modal_champion_hits = int((champion_modals == actual_buckets).sum())
        modal_challenger_hits = int((challenger_modals == actual_buckets).sum())
        top2_champion_hits = int(
            sum(
                actual in _top_buckets(probabilities)
                for actual, probabilities in zip(
                    actual_buckets, paired.probabilities_json_champion, strict=True
                )
            )
        )
        top2_challenger_hits = int(
            sum(
                actual in _top_buckets(probabilities)
                for actual, probabilities in zip(
                    actual_buckets, paired.probabilities_json_challenger, strict=True
                )
            )
        )

    bias_delta = (
        abs(float(gate.challenger_bias)) - abs(float(gate.champion_bias))
        if gate.challenger_bias is not None and gate.champion_bias is not None
        else None
    )
    conditions = [
        _condition(
            "oos_days",
            gate.oos_days,
            MINIMUM_OOS_DAYS,
            ">=",
            gate.oos_days >= MINIMUM_OOS_DAYS,
            "settled sequential OOS airport-days",
        ),
        _condition(
            "mae_gain_c",
            gate.mae_gain_c,
            MINIMUM_MAE_GAIN_C,
            ">=",
            gate.mae_gain_c is not None and gate.mae_gain_c >= MINIMUM_MAE_GAIN_C,
            "Champion MAE minus Analog MAE",
        ),
        _condition(
            "brier_gain",
            gate.brier_gain,
            MINIMUM_BRIER_GAIN,
            ">=",
            gate.brier_gain is not None and gate.brier_gain >= MINIMUM_BRIER_GAIN,
            "Champion multiclass Brier minus Analog multiclass Brier",
        ),
        _condition(
            "rounded_center_bucket_hit_gain",
            gate.exact_hit_gain,
            -0.02,
            ">=",
            gate.exact_hit_gain is not None and gate.exact_hit_gain >= -0.02,
            "unchanged gate uses rounded forecast center, not distribution modal",
        ),
        _condition(
            "absolute_bias_guard_delta_c",
            bias_delta,
            0.15,
            "<=",
            bias_delta is not None and bias_delta <= 0.15,
            "abs(Analog bias) minus abs(Champion bias)",
        ),
        _condition(
            "recent_10_mae_gain_c",
            recent_mae_gain,
            0.0,
            ">=",
            recent_mae_gain is not None and recent_mae_gain >= 0.0,
            "last 10 selected OOS days",
        ),
        _condition(
            "recent_10_absolute_bias_guard_delta_c",
            recent_bias_delta,
            0.15,
            "<=",
            recent_bias_delta is not None and recent_bias_delta <= 0.15,
            "last 10 selected OOS days",
        ),
    ]
    blockers = [item["condition"] for item in conditions if item["status"] == "FAIL"]

    used_days = []
    for row in paired.itertuples():
        actual_bucket = _bucket(row.max_temp_c)
        champion_center_bucket = _bucket(row.forecast_c_champion)
        challenger_center_bucket = _bucket(row.forecast_c_challenger)
        champion_modal = _modal_bucket(row.probabilities_json_champion)
        challenger_modal = _modal_bucket(row.probabilities_json_challenger)
        local = pd.Timestamp(row.captured_at).tz_convert(timezone_name)
        fingerprint = {
            "captured_at": pd.Timestamp(row.captured_at).isoformat(),
            "champion_center_c": float(row.forecast_c_champion),
            "analog_center_c": float(row.forecast_c_challenger),
            "champion_probabilities": _probabilities(row.probabilities_json_champion),
            "analog_probabilities": _probabilities(row.probabilities_json_challenger),
        }
        used_days.append(
            {
                "airport": str(row.airport),
                "target_date": row.target_date.isoformat(),
                "selected_checkpoint": f"{local:%H:%M} Europe/Madrid",
                "captured_at": pd.Timestamp(row.captured_at).isoformat(),
                "actual_bucket": actual_bucket,
                "actual_c": float(row.max_temp_c),
                "season": (
                    "winter"
                    if row.target_date.month in {12, 1, 2}
                    else "spring"
                    if row.target_date.month in {3, 4, 5}
                    else "summer"
                    if row.target_date.month in {6, 7, 8}
                    else "autumn"
                ),
                "champion_center_c": float(row.forecast_c_champion),
                "analog_center_c": float(row.forecast_c_challenger),
                "champion_center_error_c": float(row.forecast_c_champion - row.max_temp_c),
                "analog_center_error_c": float(row.forecast_c_challenger - row.max_temp_c),
                "champion_rounded_center_bucket": champion_center_bucket,
                "analog_rounded_center_bucket": challenger_center_bucket,
                "champion_modal_bucket": champion_modal,
                "analog_modal_bucket": challenger_modal,
                "champion_center_hit": champion_center_bucket == actual_bucket,
                "analog_center_hit": challenger_center_bucket == actual_bucket,
                "champion_modal_hit": champion_modal == actual_bucket,
                "analog_modal_hit": challenger_modal == actual_bucket,
                "champion_top2_buckets": _top_buckets(row.probabilities_json_champion),
                "analog_top2_buckets": _top_buckets(row.probabilities_json_challenger),
                "champion_top2_hit": actual_bucket
                in _top_buckets(row.probabilities_json_champion),
                "analog_top2_hit": actual_bucket
                in _top_buckets(row.probabilities_json_challenger),
                "champion_brier": _brier(row.probabilities_json_champion, actual_bucket),
                "analog_brier": _brier(row.probabilities_json_challenger, actual_bucket),
                "variant_pair_sha256": hashlib.sha256(
                    json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        )

    return {
        "scope": scope,
        "checkpoint_policy": (
            f"fixed local checkpoint {checkpoint_local_time}"
            if checkpoint_local_time
            else "unchanged combined live gate; latest paired D0-live snapshot per day"
        ),
        "status": gate.status,
        "eligible": gate.eligible,
        "oos_days": gate.oos_days,
        "conditions": conditions,
        "blockers": blockers,
        "recommendation": ("READY_FOR_RELEASE_REVIEW" if gate.eligible else "CONTINUE_SHADOW"),
        "metrics": {
            "champion_mae_c": gate.champion_mae,
            "analog_mae_c": gate.challenger_mae,
            "mae_gain_c": gate.mae_gain_c,
            "brier_gain": gate.brier_gain,
            "champion_bias_c": gate.champion_bias,
            "analog_bias_c": gate.challenger_bias,
            "champion_rounded_center_bucket_hits": center_champion_hits,
            "analog_rounded_center_bucket_hits": center_challenger_hits,
            "champion_modal_bucket_hits": modal_champion_hits,
            "analog_modal_bucket_hits": modal_challenger_hits,
            "champion_top2_bucket_hits": top2_champion_hits,
            "analog_top2_bucket_hits": top2_challenger_hits,
            "recent_10_mae_gain_c": recent_mae_gain,
            "recent_10_absolute_bias_guard_delta_c": recent_bias_delta,
        },
        "explanation": gate.explanation,
        "used_days": used_days,
    }


def build_analog_promotion_report(
    variants: pd.DataFrame,
    actuals: pd.DataFrame,
    *,
    timezone_name: str = "Europe/Madrid",
    oos_start_date: date | str | None = "2026-08-31",
) -> dict[str, Any]:
    """Return combined, First and Late gates from already-loaded evidence."""
    gates = {
        "combined_live": _gate_report(
            variants,
            actuals,
            scope="combined_live",
            timezone_name=timezone_name,
            checkpoint_local_time=None,
            oos_start_date=oos_start_date,
        ),
        "first_live_12_00": _gate_report(
            variants,
            actuals,
            scope="first_live_12_00",
            timezone_name=timezone_name,
            checkpoint_local_time="12:00",
            oos_start_date=oos_start_date,
        ),
        "late_live_16_00": _gate_report(
            variants,
            actuals,
            scope="late_live_16_00",
            timezone_name=timezone_name,
            checkpoint_local_time="16:00",
            oos_start_date=oos_start_date,
        ),
    }
    return {
        "evaluation_version": EVALUATION_VERSION,
        "generated_from": "already-loaded ForecastVariantSnapshot and DailyActual rows",
        "timezone": timezone_name,
        "oos_start_date": str(oos_start_date) if oos_start_date else None,
        "research_only": True,
        "automatic_promotion": False,
        "changes_champion": False,
        "changes_thresholds": False,
        "changes_betting_logic": False,
        "existing_combined_gate_unchanged": True,
        "gates": gates,
    }
