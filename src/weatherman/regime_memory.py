from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .actual_quality import settlement_grade_actuals

from .analytics import condition_probability_range, consensus
from .nowcast import merge_complete_metar_actuals


@dataclass(frozen=True)
class RegimeState:
    name: str
    status: str
    confidence: int
    source: str
    champion_effect: str
    supports: tuple[str, ...]
    contradictions: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class AnalogDay:
    target_date: str
    captured_at: str
    similarity: float
    forecast_c: float
    actual_c: float
    residual_c: float
    matched_on: tuple[str, ...]


@dataclass(frozen=True)
class PromotionGate:
    status: str
    eligible: bool
    oos_days: int
    minimum_oos_days: int
    champion_mae: float | None
    challenger_mae: float | None
    mae_gain_c: float | None
    brier_gain: float | None
    exact_hit_gain: float | None
    champion_bias: float | None
    challenger_bias: float | None
    explanation: str


@dataclass(frozen=True)
class AnchorTransferAssessment:
    status: str
    hours_bucket: str
    history_days: int
    prior_gain: float
    learned_gain: float
    current_residual_c: float | None
    current_adjustment_c: float
    suggested_adjustment_c: float
    forecast_delta_c: float
    ready: bool
    applied_to_champion: bool
    promotion: PromotionGate
    explanation: str


@dataclass(frozen=True)
class RegimeMemoryAssessment:
    status: str
    label: str
    confidence: int
    analog_count: int
    best_similarity: float | None
    center_adjustment_c: float
    suggested_forecast_c: float
    suggested_spread_c: float
    shadow_only: bool
    applied_to_champion: bool
    challenger_ready: bool
    regimes: tuple[RegimeState, ...]
    analogs: tuple[AnalogDay, ...]
    promotion: PromotionGate
    pro_signals: tuple[str, ...]
    contra_signals: tuple[str, ...]
    explanation: str
    feature_signature: dict[str, float]

    def as_storage_dict(self) -> dict[str, Any]:
        return asdict(self)


FEATURE_SPECS: dict[str, tuple[float, float, bool]] = {
    # name: (normalising scale, importance, circular direction)
    "temperature_anomaly_c": (1.5, 1.0, False),
    "effective_temperature_residual_c": (1.0, 1.3, False),
    "dryness_surprise_c": (3.0, 0.8, False),
    "observed_dewpoint_trend_cph": (1.2, 0.5, False),
    "cloud_surprise_pct": (30.0, 0.8, False),
    "observed_heating_rate_60m_cph": (1.5, 1.1, False),
    "heating_rate_surprise_cph": (1.2, 1.2, False),
    "model_radiation_wm2": (250.0, 0.5, False),
    "remaining_model_rise_c": (2.0, 0.9, False),
    "wind_speed_kph": (14.0, 0.8, False),
    "wind_direction_deg": (60.0, 0.8, True),
    "hours_to_peak": (2.0, 1.0, False),
    "taf_adjustment_c": (0.25, 0.4, False),
    "final_spread_c": (0.8, 0.5, False),
    "persistent_hot_strength": (0.40, 0.7, False),
    "rapid_heat_ramp_strength": (0.40, 0.6, False),
    "maritime_advection_strength": (0.40, 0.8, False),
    "maritime_low_range_strength": (0.40, 0.8, False),
    "phase_vs_amplitude_strength": (0.40, 0.6, False),
    "post_convective_uncertainty_strength": (0.40, 0.6, False),
}


FEATURE_LABELS = {
    "temperature_anomaly_c": "temperature anomaly",
    "effective_temperature_residual_c": "METAR/model temperature gap",
    "dryness_surprise_c": "dryness",
    "observed_dewpoint_trend_cph": "dew-point trend",
    "cloud_surprise_pct": "cloud surprise",
    "observed_heating_rate_60m_cph": "60-minute heating rate",
    "heating_rate_surprise_cph": "heating-rate surprise",
    "model_radiation_wm2": "radiation",
    "remaining_model_rise_c": "remaining model rise",
    "wind_speed_kph": "wind speed",
    "wind_direction_deg": "wind direction",
    "hours_to_peak": "time to peak",
    "taf_adjustment_c": "TAF influence",
    "final_spread_c": "forecast spread",
    "persistent_hot_strength": "persistent-hot evidence",
    "rapid_heat_ramp_strength": "heat-ramp evidence",
    "maritime_advection_strength": "maritime-advection evidence",
    "maritime_low_range_strength": "maritime low-range evidence",
    "phase_vs_amplitude_strength": "phase/amplitude evidence",
    "post_convective_uncertainty_strength": "convective evidence",
}


def _number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _json_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _direction_distance(left: float, right: float) -> float:
    difference = abs((left - right) % 360.0)
    return min(difference, 360.0 - difference)


def _direction_in_sectors(direction: float | None, sectors: object) -> bool:
    if direction is None or not isinstance(sectors, (list, tuple)):
        return False
    normalised = direction % 360.0
    for pair in sectors:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        start, end = float(pair[0]) % 360.0, float(pair[1]) % 360.0
        if start <= end and start <= normalised <= end:
            return True
        if start > end and (normalised >= start or normalised <= end):
            return True
    return False


def _signature_from_nowcast(nowcast: object) -> dict[str, float]:
    features = dict(getattr(nowcast, "live_features", {}) or {})
    additions = {
        "wind_speed_kph": getattr(nowcast, "wind_speed_kph", None),
        "wind_direction_deg": getattr(nowcast, "wind_direction_deg", None),
        "hours_to_peak": getattr(nowcast, "hours_to_peak", None),
        "taf_adjustment_c": getattr(nowcast, "taf_adjustment_c", None),
        "final_spread_c": getattr(nowcast, "final_forecast_spread", None),
    }
    features.update(additions)
    signature: dict[str, float] = {}
    for name in FEATURE_SPECS:
        value = _number(features.get(name))
        if value is not None:
            signature[name] = value
    return signature


def _signature_from_snapshot(row: object) -> dict[str, float]:
    features = _json_dict(getattr(row, "features_json", "{}"))
    additions = {
        "hours_to_peak": getattr(row, "hours_to_peak", None),
        "taf_adjustment_c": getattr(row, "taf_adjustment_c", None),
        "final_spread_c": getattr(row, "final_spread_c", None),
    }
    features.update(additions)
    signature: dict[str, float] = {}
    for name in FEATURE_SPECS:
        value = _number(features.get(name))
        if value is not None:
            signature[name] = value
    return signature


def _similarity(
    current: dict[str, float],
    historic: dict[str, float],
) -> tuple[float, tuple[str, ...], int]:
    weighted_distance = 0.0
    total_weight = 0.0
    feature_distances: list[tuple[float, str]] = []
    for name, (scale, weight, circular) in FEATURE_SPECS.items():
        if name not in current or name not in historic:
            continue
        delta = (
            _direction_distance(current[name], historic[name])
            if circular
            else abs(current[name] - historic[name])
        )
        normalised = min(3.0, delta / max(1e-9, scale))
        weighted_distance += normalised * weight
        total_weight += weight
        feature_distances.append((normalised, name))
    if len(feature_distances) < 3 or total_weight <= 0:
        return 0.0, (), len(feature_distances)
    distance = weighted_distance / total_weight
    similarity = math.exp(-distance)
    matched = tuple(
        FEATURE_LABELS.get(name, name)
        for normalised, name in sorted(feature_distances)[:3]
        if normalised <= 1.0
    )
    return similarity, matched, len(feature_distances)


def _timing_group(value: object) -> str:
    text = str(value or "").casefold()
    if "d-1" in text or "earlier" in text:
        return "D-1"
    if "morning" in text:
        return "D0 morning"
    return "D0 live"


def _actual_map(actuals: pd.DataFrame, target: date) -> dict[date, float]:
    if actuals.empty or not {"target_date", "max_temp_c"}.issubset(actuals.columns):
        return {}
    frame = settlement_grade_actuals(actuals)
    frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
    frame["max_temp_c"] = pd.to_numeric(frame.max_temp_c, errors="coerce")
    frame = frame[(frame.target_date < target) & frame.max_temp_c.notna()]
    frame = frame.sort_values("target_date").drop_duplicates("target_date", keep="last")
    return {
        row.target_date: float(row.max_temp_c)
        for row in frame.itertuples()
        if isinstance(row.target_date, date)
    }


def find_analog_days(
    snapshots: pd.DataFrame,
    actuals: pd.DataFrame,
    *,
    current_signature: dict[str, float],
    target: date,
    current_phase: str,
    minimum_similarity: float = 0.45,
    maximum_analogs: int = 8,
) -> tuple[AnalogDay, ...]:
    """Return only earlier, settled airport-days with a comparable information set."""
    if snapshots.empty or not current_signature:
        return ()
    required = {"target_date", "captured_at", "final_forecast_c", "features_json"}
    if not required.issubset(snapshots.columns):
        return ()
    actual_by_day = _actual_map(actuals, target)
    if not actual_by_day:
        return ()
    frame = snapshots.copy()
    frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    frame = frame[
        (frame.target_date < target)
        & frame.target_date.isin(actual_by_day)
        & frame.captured_at.notna()
    ]
    if frame.empty:
        return ()
    current_hours = current_signature.get("hours_to_peak")
    candidates: list[tuple[float, AnalogDay]] = []
    for historic_day, day_frame in frame.groupby("target_date"):
        same_phase = day_frame[
            day_frame.get("day_phase", pd.Series(index=day_frame.index, dtype=object))
            .astype(str)
            .eq(str(current_phase))
        ]
        choices = same_phase if not same_phase.empty else day_frame
        if current_hours is not None and "hours_to_peak" in choices:
            hours = pd.to_numeric(choices.hours_to_peak, errors="coerce")
            if hours.notna().any():
                selected = choices.loc[(hours - current_hours).abs().idxmin()]
            else:
                selected = choices.sort_values("captured_at").iloc[-1]
        else:
            selected = choices.sort_values("captured_at").iloc[-1]
        historic_signature = _signature_from_snapshot(selected)
        similarity, matched_on, common = _similarity(current_signature, historic_signature)
        if common < 3 or similarity < minimum_similarity:
            continue
        forecast = _number(selected.final_forecast_c)
        actual = actual_by_day.get(historic_day)
        if forecast is None or actual is None:
            continue
        analog = AnalogDay(
            target_date=historic_day.isoformat(),
            captured_at=pd.Timestamp(selected.captured_at).isoformat(),
            similarity=round(similarity, 4),
            forecast_c=forecast,
            actual_c=actual,
            residual_c=actual - forecast,
            matched_on=matched_on,
        )
        candidates.append((similarity, analog))
    candidates.sort(key=lambda item: (item[0], item[1].target_date), reverse=True)
    return tuple(item[1] for item in candidates[:maximum_analogs])


def _normalised_probabilities(value: object) -> dict[int, float]:
    raw = _json_dict(value)
    parsed: dict[int, float] = {}
    for bucket, probability in raw.items():
        try:
            parsed[int(bucket)] = max(0.0, float(probability))
        except (TypeError, ValueError):
            continue
    total = sum(parsed.values())
    return {bucket: value / total for bucket, value in parsed.items()} if total else {}


def _brier(probabilities: dict[int, float], actual_bucket: int) -> float | None:
    if not probabilities:
        return None
    buckets = set(probabilities) | {actual_bucket}
    return sum(
        (probabilities.get(bucket, 0.0) - float(bucket == actual_bucket)) ** 2
        for bucket in buckets
    )


def evaluate_promotion_gate(
    variants: pd.DataFrame,
    actuals: pd.DataFrame,
    *,
    timing_group: str,
    factor: str = "regime_memory_analog",
    challenger_label: str = "analog Challenger",
    minimum_oos_days: int = 30,
    minimum_mae_gain_c: float = 0.12,
    minimum_brier_gain: float = 0.003,
) -> PromotionGate:
    """Evaluate sequential challenger forecasts that were made before their outcomes."""
    empty = PromotionGate(
        status="SHADOW",
        eligible=False,
        oos_days=0,
        minimum_oos_days=minimum_oos_days,
        champion_mae=None,
        challenger_mae=None,
        mae_gain_c=None,
        brier_gain=None,
        exact_hit_gain=None,
        champion_bias=None,
        challenger_bias=None,
        explanation=(
            f"0/{minimum_oos_days} settled out-of-sample airport-days. "
            "The learned pattern cannot affect the Champion."
        ),
    )
    if variants.empty or actuals.empty:
        return empty
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
    if not required.issubset(variants.columns):
        return empty
    frame = variants.copy()
    frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    frame = frame[frame.timing.map(_timing_group) == timing_group]
    champion = frame[frame.variant == "Champion"]
    challenger = frame[frame.factor == factor]
    if champion.empty or challenger.empty:
        return empty
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
        return empty
    actual = actual[["airport", "target_date", "max_temp_c"]].copy()
    actual["target_date"] = pd.to_datetime(actual.target_date, errors="coerce").dt.date
    actual["max_temp_c"] = pd.to_numeric(actual.max_temp_c, errors="coerce")
    paired = paired.merge(actual, on=["airport", "target_date"], how="inner")
    paired = paired.dropna(subset=["max_temp_c"]).sort_values("captured_at")
    paired = paired.drop_duplicates(["airport", "target_date"], keep="last")
    if paired.empty:
        return empty
    actual_values = paired.max_temp_c.astype(float)
    champion_values = pd.to_numeric(paired.forecast_c_champion, errors="coerce")
    challenger_values = pd.to_numeric(paired.forecast_c_challenger, errors="coerce")
    usable = champion_values.notna() & challenger_values.notna()
    paired = paired[usable]
    actual_values = actual_values[usable]
    champion_values = champion_values[usable]
    challenger_values = challenger_values[usable]
    if paired.empty:
        return empty
    champion_error = champion_values - actual_values
    challenger_error = challenger_values - actual_values
    champion_mae = float(champion_error.abs().mean())
    challenger_mae = float(challenger_error.abs().mean())
    mae_gain = champion_mae - challenger_mae
    champion_briers: list[float] = []
    challenger_briers: list[float] = []
    for row in paired.itertuples():
        actual_bucket = math.floor(float(row.max_temp_c) + 0.5)
        champion_score = _brier(
            _normalised_probabilities(row.probabilities_json_champion),
            actual_bucket,
        )
        challenger_score = _brier(
            _normalised_probabilities(row.probabilities_json_challenger),
            actual_bucket,
        )
        if champion_score is not None and challenger_score is not None:
            champion_briers.append(champion_score)
            challenger_briers.append(challenger_score)
    brier_gain = (
        float(pd.Series(champion_briers).mean() - pd.Series(challenger_briers).mean())
        if champion_briers
        else None
    )
    actual_bucket = actual_values.map(lambda value: math.floor(float(value) + 0.5))
    champion_bucket = champion_values.map(lambda value: math.floor(float(value) + 0.5))
    challenger_bucket = challenger_values.map(lambda value: math.floor(float(value) + 0.5))
    exact_gain = float(
        (challenger_bucket == actual_bucket).mean()
        - (champion_bucket == actual_bucket).mean()
    )
    champion_bias = float(champion_error.mean())
    challenger_bias = float(challenger_error.mean())
    days = int(paired.target_date.nunique())
    recent_count = min(10, len(paired))
    recent_champion_error = champion_error.tail(recent_count)
    recent_challenger_error = challenger_error.tail(recent_count)
    recent_mae_gain = float(
        recent_champion_error.abs().mean() - recent_challenger_error.abs().mean()
    )
    recent_bias_guard = bool(
        abs(float(recent_challenger_error.mean()))
        <= abs(float(recent_champion_error.mean())) + 0.15
    )
    enough_days = days >= minimum_oos_days
    quality_pass = bool(
        mae_gain >= minimum_mae_gain_c
        and brier_gain is not None
        and brier_gain >= minimum_brier_gain
        and exact_gain >= -0.02
        and abs(challenger_bias) <= abs(champion_bias) + 0.15
        and recent_mae_gain >= 0.0
        and recent_bias_guard
    )
    eligible = enough_days and quality_pass
    if eligible:
        status = "AUTO-PROMOTION ELIGIBLE"
        explanation = (
            f"{days} settled OOS days; the {challenger_label} improves MAE by "
            f"{mae_gain:.2f} °C and Brier by {brier_gain:.3f}. Its last {recent_count} "
            f"days remain stable ({recent_mae_gain:+.2f} °C MAE gain), so guarded "
            "automatic promotion is permitted."
        )
    elif not enough_days:
        status = "SHADOW"
        explanation = (
            f"{days}/{minimum_oos_days} settled OOS days. The learned pattern remains "
            "Challenger-only regardless of its current score."
        )
    else:
        status = "NOT ELIGIBLE"
        explanation = (
            f"{days} settled OOS days, but the accuracy safeguards are not all met. "
            f"The last {recent_count} days show {recent_mae_gain:+.2f} °C MAE gain. "
            "The learned pattern remains Challenger-only or is rolled back automatically."
        )
    return PromotionGate(
        status=status,
        eligible=eligible,
        oos_days=days,
        minimum_oos_days=minimum_oos_days,
        champion_mae=champion_mae,
        challenger_mae=challenger_mae,
        mae_gain_c=mae_gain,
        brier_gain=brier_gain,
        exact_hit_gain=exact_gain,
        champion_bias=champion_bias,
        challenger_bias=challenger_bias,
        explanation=explanation,
    )


def _anchor_hours_bucket(hours_to_peak: float | None) -> str:
    if hours_to_peak is None or not math.isfinite(float(hours_to_peak)):
        return "peak unknown"
    hours = float(hours_to_peak)
    if hours > 6:
        return ">6 h to peak"
    if hours > 4:
        return "4–6 h to peak"
    if hours > 2:
        return "2–4 h to peak"
    if hours > 0:
        return "0–2 h to peak"
    return "after modelled peak"


def assess_anchor_transfer(
    nowcast: object,
    snapshots: pd.DataFrame,
    actuals: pd.DataFrame,
    observations: pd.DataFrame,
    variants: pd.DataFrame,
    *,
    airport_profile: dict[str, object],
    timezone_name: str,
    target: date,
    as_of: datetime,
    config: dict[str, object] | None = None,
) -> AnchorTransferAssessment:
    """Learn how much a same-time METAR residual survives until airport Tmax.

    Every training row predates ``target`` and one comparable checkpoint per settled
    airport-day is used. The learned coefficient is shrunk toward the conservative
    peak-aware Champion coefficient; it therefore starts as a no-op and earns influence
    only through independent airport-days.
    """
    configured = config or {}
    features = dict(getattr(nowcast, "live_features", {}) or {})
    residual = _number(features.get("effective_temperature_residual_c"))
    prior_gain = _number(features.get("temperature_anchor_gain")) or 0.0
    current_hours = _number(getattr(nowcast, "hours_to_peak", None))
    current_streak = _number(features.get("temperature_anchor_streak")) or 0.0
    current_adjustment = _number(
        dict(getattr(nowcast, "adjustment_contributions", {}) or {}).get(
            "temperature_anchor"
        )
    ) or 0.0
    timing = (
        "D-1"
        if as_of.astimezone(ZoneInfo(timezone_name)).date() < target
        else "D0 morning"
        if as_of.astimezone(ZoneInfo(timezone_name)).hour < 12
        else "D0 live"
    )
    promotion = evaluate_promotion_gate(
        variants,
        actuals,
        timing_group=timing,
        factor="airport_anchor_transfer",
        challenger_label="airport Anchor Transfer Challenger",
        minimum_oos_days=int(configured.get("anchor_minimum_oos_days", 30)),
        minimum_mae_gain_c=float(configured.get("anchor_minimum_mae_gain_c", 0.10)),
        minimum_brier_gain=float(configured.get("anchor_minimum_brier_gain", 0.002)),
    )
    bucket = _anchor_hours_bucket(current_hours)
    empty_reason = (
        "No current METAR/model path residual is available; the airport-specific "
        "Anchor Challenger is not created at this checkpoint."
    )
    if residual is None or abs(residual) < float(
        configured.get("anchor_minimum_current_residual_c", 0.25)
    ):
        return AnchorTransferAssessment(
            status="NO CURRENT SIGNAL",
            hours_bucket=bucket,
            history_days=0,
            prior_gain=prior_gain,
            learned_gain=prior_gain,
            current_residual_c=residual,
            current_adjustment_c=current_adjustment,
            suggested_adjustment_c=current_adjustment,
            forecast_delta_c=0.0,
            ready=False,
            applied_to_champion=False,
            promotion=promotion,
            explanation=empty_reason,
        )
    required = {
        "target_date",
        "captured_at",
        "final_forecast_c",
        "temp_anchor_adjustment_c",
        "features_json",
    }
    if snapshots.empty or not required.issubset(snapshots.columns):
        return AnchorTransferAssessment(
            status="BUILDING HISTORY",
            hours_bucket=bucket,
            history_days=0,
            prior_gain=prior_gain,
            learned_gain=prior_gain,
            current_residual_c=residual,
            current_adjustment_c=current_adjustment,
            suggested_adjustment_c=current_adjustment,
            forecast_delta_c=0.0,
            ready=False,
            applied_to_champion=False,
            promotion=promotion,
            explanation="No earlier settled Anchor checkpoints are stored yet.",
        )

    airport_code = "UNKN"
    current = getattr(nowcast, "current", pd.DataFrame())
    if isinstance(current, pd.DataFrame) and not current.empty and "airport" in current:
        airport_code = str(current.airport.iloc[0])
    effective_actuals = merge_complete_metar_actuals(
        actuals,
        observations,
        airport_code=airport_code,
        timezone_name=timezone_name,
        target=target,
        as_of=as_of,
        critical_window_local=airport_profile.get("critical_window_local"),
    )
    actual_by_day = _actual_map(effective_actuals, target)
    frame = snapshots.copy()
    frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    frame = frame[
        frame.target_date.notna()
        & (frame.target_date < target)
        & frame.target_date.isin(actual_by_day)
    ]
    candidates: list[dict[str, float | date]] = []
    minimum_history_residual = float(
        configured.get("anchor_minimum_history_residual_c", 0.30)
    )
    for row in frame.itertuples():
        historic_features = _json_dict(getattr(row, "features_json", "{}"))
        historic_residual = _number(
            historic_features.get("effective_temperature_residual_c")
        )
        historic_hours = _number(getattr(row, "hours_to_peak", None))
        if historic_hours is None:
            historic_hours = _number(historic_features.get("hours_to_peak"))
        historic_streak = _number(historic_features.get("temperature_anchor_streak")) or 0.0
        final_forecast = _number(getattr(row, "final_forecast_c", None))
        historic_anchor = _number(getattr(row, "temp_anchor_adjustment_c", None)) or 0.0
        actual = actual_by_day.get(row.target_date)
        if (
            historic_residual is None
            or abs(historic_residual) < minimum_history_residual
            or final_forecast is None
            or actual is None
        ):
            continue
        distance = (
            abs(historic_hours - current_hours)
            if historic_hours is not None and current_hours is not None
            else 3.0
        )
        time_weight = math.exp(-distance / float(configured.get("anchor_hour_scale", 3.0)))
        same_persistence = (historic_streak >= 2) == (current_streak >= 2)
        candidates.append(
            {
                "target_date": row.target_date,
                "distance": distance,
                "weight": time_weight * (1.0 if same_persistence else 0.70),
                "residual": historic_residual,
                "required": max(
                    -2.0,
                    min(2.0, float(actual) - (final_forecast - historic_anchor)),
                ),
            }
        )
    if candidates:
        candidate_frame = pd.DataFrame(candidates)
        candidate_frame = candidate_frame.sort_values(
            ["target_date", "distance", "weight"],
            ascending=[True, True, False],
        ).drop_duplicates("target_date", keep="first")
        lookback_days = int(configured.get("anchor_lookback_days", 90))
        candidate_frame = candidate_frame[
            candidate_frame.target_date >= target - timedelta(days=lookback_days)
        ]
    else:
        candidate_frame = pd.DataFrame()
    history_days = int(candidate_frame.target_date.nunique()) if not candidate_frame.empty else 0
    minimum_days = int(configured.get("anchor_minimum_training_days", 5))
    if history_days:
        numerator = float(
            (
                candidate_frame.weight
                * candidate_frame.residual
                * candidate_frame.required
            ).sum()
        )
        denominator = float(
            (candidate_frame.weight * candidate_frame.residual**2).sum()
        )
        raw_gain = max(0.0, min(1.0, numerator / denominator)) if denominator > 1e-9 else prior_gain
        shrinkage_days = float(configured.get("anchor_shrinkage_days", 12.0))
        reliability = history_days / (history_days + max(1.0, shrinkage_days))
        learned_gain = (1.0 - reliability) * prior_gain + reliability * raw_gain
    else:
        learned_gain = prior_gain
    suggested_adjustment = max(-1.40, min(1.40, residual * learned_gain))
    current_total = _number(
        dict(getattr(nowcast, "adjustment_contributions", {}) or {}).get("total")
    ) or 0.0
    suggested_total = max(
        -2.0,
        min(2.0, current_total - current_adjustment + suggested_adjustment),
    )
    delta = suggested_total - current_total
    ready = history_days >= minimum_days and abs(delta) >= float(
        configured.get("anchor_minimum_challenger_delta_c", 0.03)
    )
    allow_promoted = bool(configured.get("allow_promoted", False))
    applied = bool(ready and allow_promoted and promotion.eligible)
    status = "PROMOTED" if applied else "SHADOW" if ready else "BUILDING HISTORY"
    explanation = (
        f"{history_days}/{minimum_days} comparable settled airport-days in {bucket}. "
        f"The peak-aware prior gain is {prior_gain:.0%}; the shrunk airport estimate is "
        f"{learned_gain:.0%}, implying {delta:+.2f} °C versus the current Champion. "
        f"{promotion.explanation}"
    )
    return AnchorTransferAssessment(
        status=status,
        hours_bucket=bucket,
        history_days=history_days,
        prior_gain=prior_gain,
        learned_gain=learned_gain,
        current_residual_c=residual,
        current_adjustment_c=current_adjustment,
        suggested_adjustment_c=suggested_adjustment,
        forecast_delta_c=delta if ready else 0.0,
        ready=ready,
        applied_to_champion=applied,
        promotion=promotion,
        explanation=explanation,
    )


def _enrich_with_anchor_transfer(
    nowcast: object,
    snapshots: pd.DataFrame,
    actuals: pd.DataFrame,
    observations: pd.DataFrame,
    variants: pd.DataFrame,
    *,
    airport_profile: dict[str, object],
    timezone_name: str,
    target: date,
    as_of: datetime,
    config: dict[str, object] | None,
) -> object:
    assessment = assess_anchor_transfer(
        nowcast,
        snapshots,
        actuals,
        observations,
        variants,
        airport_profile=airport_profile,
        timezone_name=timezone_name,
        target=target,
        as_of=as_of,
        config=config,
    )
    live_features = dict(getattr(nowcast, "live_features", {}) or {})
    live_features.update(
        {
            "anchor_transfer_status": assessment.status,
            "anchor_transfer_hours_bucket": assessment.hours_bucket,
            "anchor_transfer_history_days": float(assessment.history_days),
            "anchor_transfer_prior_gain": assessment.prior_gain,
            "anchor_transfer_learned_gain": assessment.learned_gain,
            "anchor_transfer_forecast_delta_c": assessment.forecast_delta_c,
            "anchor_transfer_oos_days": float(assessment.promotion.oos_days),
            "anchor_transfer_promotion_eligible": float(
                assessment.promotion.eligible
            ),
            "anchor_transfer_explanation": assessment.explanation,
        }
    )
    if not assessment.ready:
        return replace(nowcast, live_features=live_features)
    champion_mean = float(getattr(nowcast, "final_forecast_mean"))
    champion_spread = float(getattr(nowcast, "final_forecast_spread"))
    alternative_mean = champion_mean + assessment.forecast_delta_c
    alternative_unconditioned = consensus(
        [alternative_mean],
        sigma_floor=max(0.65, champion_spread),
    )
    day_status = getattr(nowcast, "day_status")
    alternative_probabilities = condition_probability_range(
        alternative_unconditioned.probability_by_bucket,
        day_status.minimum_bucket,
        day_status.maximum_bucket,
    )
    original_variant = {
        "factor": "airport_anchor_transfer",
        "forecast_mean_c": champion_mean,
        "spread_c": champion_spread,
        "probabilities": dict(getattr(nowcast, "probabilities")),
        "forecast_confidence": int(getattr(nowcast, "forecast_confidence")),
    }
    alternative_variant = {
        "factor": "airport_anchor_transfer",
        "forecast_mean_c": alternative_mean,
        "spread_c": champion_spread,
        "probabilities": alternative_probabilities,
        "forecast_confidence": int(getattr(nowcast, "forecast_confidence")),
    }
    challengers = dict(getattr(nowcast, "challenger_variants", {}) or {})
    if not assessment.applied_to_champion:
        challengers["Airport Anchor Transfer Challenger"] = alternative_variant
        return replace(
            nowcast,
            challenger_variants=challengers,
            live_features=live_features,
        )
    challengers["Without Promoted Airport Anchor Transfer"] = original_variant
    contributions = dict(getattr(nowcast, "adjustment_contributions", {}) or {})
    contributions["temperature_anchor"] = assessment.suggested_adjustment_c
    contributions["total"] = float(contributions.get("total", 0.0)) + assessment.forecast_delta_c
    stages = dict(getattr(nowcast, "stage_probabilities", {}) or {})
    stages["Final incl. promoted Airport Anchor Transfer"] = alternative_probabilities
    return replace(
        nowcast,
        probabilities=alternative_probabilities,
        final_forecast_mean=alternative_mean,
        stage_probabilities=stages,
        adjustment_contributions=contributions,
        challenger_variants=challengers,
        live_features=live_features,
    )


def _recent_observations(
    observations: pd.DataFrame,
    *,
    timezone_name: str,
    target: date,
    as_of: datetime,
) -> pd.DataFrame:
    if observations.empty or "observed_at" not in observations:
        return pd.DataFrame()
    frame = observations.copy()
    frame["observed_at"] = pd.to_datetime(frame.observed_at, utc=True, errors="coerce")
    frame = frame[frame.observed_at.notna() & (frame.observed_at <= pd.Timestamp(as_of))]
    frame["local_at"] = frame.observed_at.dt.tz_convert(timezone_name)
    return frame[frame.local_at.dt.date == target].sort_values("observed_at")


def _known_regime_states(
    nowcast: object,
    observations: pd.DataFrame,
    *,
    airport_profile: dict[str, object],
    timezone_name: str,
    target: date,
    as_of: datetime,
) -> tuple[RegimeState, ...]:
    features = dict(getattr(nowcast, "live_features", {}) or {})
    states: list[RegimeState] = []

    persistent_profile = (airport_profile.get("heat_regime") or {}).get(
        "persistent_hot", {}
    )
    if persistent_profile:
        active = bool(_number(features.get("persistent_hot_active")) or 0)
        evidence = max(
            0.0,
            min(1.0, _number(features.get("persistent_hot_evidence_score")) or 0.0),
        )
        intensity = max(
            0.0,
            min(1.0, _number(features.get("persistent_hot_intensity")) or 0.0),
        )
        anomaly = _number(features.get("persistent_hot_latest_anomaly_c"))
        taf_support = bool(_number(features.get("persistent_hot_taf_support")) or 0)
        clear_support = bool(_number(features.get("persistent_hot_clear_support")) or 0)
        supports = []
        if anomaly is not None and anomaly >= float(
            persistent_profile.get("minimum_latest_anomaly_c", 3.0)
        ):
            supports.append(f"yesterday was {anomaly:+.1f} °C above the recent baseline")
        if taf_support:
            supports.append("TAF maximum supports the hot continuation")
        if clear_support:
            supports.append("TAF keeps the peak window clear and dry")
        status = (
            "CONFIRMED"
            if intensity >= 0.67
            else "PREDICTED"
            if active
            else "WATCH"
            if supports
            else "REJECTED"
        )
        confidence = round(30 + 65 * evidence) if supports or active else 0
        states.append(
            RegimeState(
                name="Persistent Hot",
                status=status,
                confidence=confidence,
                source="configured",
                champion_effect=(
                    f"gradual factor at {intensity:.0%} strength"
                    if active
                    else "none until evidence clears the gradual gate"
                ),
                supports=tuple(supports),
                contradictions=() if supports else ("no hot-continuation evidence",),
                explanation=(
                    "Prior heat and independent forecast evidence are blended continuously; "
                    "the Champion effect grows with evidence instead of switching on at one "
                    "checkpoint."
                    if active
                    else "Hot-continuation evidence is being monitored before gradual activation."
                ),
            )
        )

    rapid_active = bool(_number(features.get("rapid_heat_ramp_active")) or 0)
    rapid_gap = _number(features.get("rapid_heat_ramp_forecast_vs_latest_c"))
    observed_support = False
    if rapid_active or (rapid_gap is not None and rapid_gap >= 2.0):
        observed_support = (
            (_number(features.get("heating_rate_surprise_cph")) or -99.0) >= 0.4
            or (_number(features.get("effective_temperature_residual_c")) or -99.0) >= 0.5
        )
        states.append(
            RegimeState(
                name="Rapid Heat Ramp",
                status="CONFIRMED" if rapid_active and observed_support else "PREDICTED",
                confidence=78 if rapid_active and observed_support else 58,
                source="configured",
                champion_effect="active factor" if rapid_active else "none until confirmed",
                supports=tuple(
                    item
                    for item in [
                        f"forecast is {rapid_gap:+.1f} °C above the latest day"
                        if rapid_gap is not None
                        else None,
                        "METAR heating supports the transition" if observed_support else None,
                    ]
                    if item
                ),
                contradictions=()
                if observed_support
                else ("METAR has not confirmed the ramp",),
                explanation="A fast transition into a warmer air mass is being tested.",
            )
        )

    post_convective_active = bool(
        _number(features.get("post_convective_uncertainty_active")) or 0
    )
    if post_convective_active:
        reports = int(
            _number(features.get("post_convective_reports_48h")) or 0
        )
        states.append(
            RegimeState(
                name="Post-Convective Uncertainty",
                status="CONFIRMED",
                confidence=min(82, 52 + reports * 8),
                source="configured",
                champion_effect="active spread factor",
                supports=(f"{reports} convective METAR report(s) in the last 48 hours",),
                contradictions=("temperature centre is deliberately not shifted",),
                explanation=(
                    "Recent convection makes the winning bucket less stable. The active "
                    "factor widens uncertainty without inventing a directional correction."
                ),
            )
        )

    phase_profile = airport_profile.get("phase_vs_amplitude") or {}
    if phase_profile:
        active = bool(_number(features.get("phase_vs_amplitude_active")) or 0)
        classification = str(features.get("phase_vs_amplitude_classification") or "")
        states.append(
            RegimeState(
                name="Phase vs Amplitude",
                status="CONFIRMED" if active else "WATCH" if classification else "PREDICTED",
                confidence=76 if active else 48,
                source="configured",
                champion_effect="active factor" if active else "none until confirmed",
                supports=(f"current classification: {classification}",) if classification else (),
                contradictions=() if active else ("phase fit is not yet decisive",),
                explanation="Tests whether the observed curve is early/late rather than too warm/cold.",
            )
        )

    taf = getattr(nowcast, "taf_guidance", None)
    taf_wind = _number(getattr(taf, "peak_wind_direction_deg", None))
    taf_wind_speed = _number(getattr(taf, "peak_wind_kph", None))
    current_wind = _number(getattr(nowcast, "wind_direction_deg", None))
    current_wind_speed = _number(getattr(nowcast, "wind_speed_kph", None))
    maritime_profile = airport_profile.get("maritime_advection") or {}
    if maritime_profile:
        active = bool(_number(features.get("maritime_advection_active")) or 0)
        sectors = maritime_profile.get("maritime_sectors")
        predicted = _direction_in_sectors(taf_wind, sectors) and (taf_wind_speed or 0) >= 12
        watch = _direction_in_sectors(current_wind, sectors) and (current_wind_speed or 0) >= 12
        status = "CONFIRMED" if active else "WATCH" if watch else "PREDICTED" if predicted else "REJECTED"
        states.append(
            RegimeState(
                name="Maritime Advection",
                status=status,
                confidence=84 if active else 62 if watch else 50 if predicted else 25,
                source="configured",
                champion_effect="active factor" if active else "none until confirmed",
                supports=tuple(
                    item
                    for item in [
                        "TAF predicts the maritime wind sector" if predicted else None,
                        "METAR has entered the maritime wind sector" if watch else None,
                        "temperature plateau/cooling confirms advection" if active else None,
                    ]
                    if item
                ),
                contradictions=() if active else ("sustained cooling/plateau is not yet confirmed",),
                explanation="Tracks arrival and persistence of a cooling maritime air mass.",
            )
        )

    low_range_profile = airport_profile.get("maritime_low_range") or {}
    if low_range_profile:
        active = bool(_number(features.get("maritime_low_range_active")) or 0)
        sectors = low_range_profile.get("sea_wind_sectors")
        predicted = _direction_in_sectors(taf_wind, sectors) and (taf_wind_speed or 0) >= 20
        watch = _direction_in_sectors(current_wind, sectors) and (current_wind_speed or 0) >= 20
        status = "CONFIRMED" if active else "WATCH" if watch else "PREDICTED" if predicted else "REJECTED"
        states.append(
            RegimeState(
                name="Maritime Low Range",
                status=status,
                confidence=88 if active else 66 if watch else 52 if predicted else 25,
                source="configured",
                champion_effect="active factor" if active else "none until confirmed",
                supports=tuple(
                    item
                    for item in [
                        "TAF predicts strong sea wind" if predicted else None,
                        "METAR shows strong sea wind" if watch else None,
                        "observed temperature range is compressed" if active else None,
                    ]
                    if item
                ),
                contradictions=() if active else ("low daily range is not yet confirmed",),
                explanation="Compresses uncertainty around a moving air-mass baseline, not a fixed bucket.",
            )
        )

    recent = _recent_observations(
        observations,
        timezone_name=timezone_name,
        target=target,
        as_of=as_of,
    )
    if len(recent) >= 3 and {"wind_direction", "temp_c"}.issubset(recent.columns):
        window = recent.tail(5)
        directions = pd.to_numeric(window.wind_direction, errors="coerce").dropna()
        temperatures = pd.to_numeric(window.temp_c, errors="coerce").dropna()
        if len(directions) >= 2 and len(temperatures) >= 2:
            shift = _direction_distance(float(directions.iloc[0]), float(directions.iloc[-1]))
            elapsed = (
                pd.Timestamp(window.observed_at.iloc[-1])
                - pd.Timestamp(window.observed_at.iloc[0])
            ).total_seconds() / 3600
            rate = (
                (float(temperatures.iloc[-1]) - float(temperatures.iloc[0])) / elapsed
                if elapsed > 0
                else 0.0
            )
            if shift >= 45:
                confirmed = shift >= 70 and rate <= 0.2
                states.append(
                    RegimeState(
                        name="Wind Shift / Air-mass Change",
                        status="CONFIRMED" if confirmed else "WATCH",
                        confidence=76 if confirmed else 55,
                        source="candidate",
                        champion_effect="Challenger only",
                        supports=(
                            f"wind changed by {shift:.0f}° across recent METARs",
                            f"temperature rate after the shift is {rate:+.1f} °C/h",
                        ),
                        contradictions=() if confirmed else ("temperature response is not decisive",),
                        explanation=(
                            "A continental air-mass transition may end the heating curve early. "
                            "This candidate is recorded but cannot alter the Champion."
                        ),
                    )
                )

    if (
        taf is not None
        and bool(getattr(taf, "cloud_clearance_reheating_predicted", False))
        and not bool(getattr(taf, "post_rain_reheating_predicted", False))
    ):
        states.append(
            RegimeState(
                name="Cloud-Clearance Reheating",
                status="PREDICTED",
                confidence=55,
                source="candidate",
                champion_effect="Challenger only",
                supports=(
                    "TAF predicts BKN/OVC giving way to clearer conditions",
                    "future model warming and radiation are checked separately",
                ),
                contradictions=("renewed heating is not yet confirmed by METAR",),
                explanation=(
                    "A second heating window is plausible after cloud clearance. The "
                    "candidate is stored without changing the Champion."
                ),
            )
        )

    if len(recent) >= 3:
        raw_series = recent.get("raw", pd.Series(index=recent.index, dtype=object)).fillna("")
        cloud_series = pd.to_numeric(
            recent.get("cloud_cover", pd.Series(index=recent.index, dtype=float)),
            errors="coerce",
        )
        rain_seen = raw_series.astype(str).str.contains("RA|SH", regex=True).iloc[:-1].any()
        cleared = (
            "CAVOK" in str(raw_series.iloc[-1])
            or (
                cloud_series.notna().sum() >= 2
                and float(cloud_series.dropna().iloc[0]) >= 60
                and float(cloud_series.dropna().iloc[-1]) <= 25
            )
        )
        heating_after_clearance = (
            _number(features.get("observed_heating_rate_60m_cph")) or 0.0
        ) >= 0.3
        if rain_seen and cleared:
            states.append(
                RegimeState(
                    name="Post-Rain Cloud Clearance",
                    status="CONFIRMED" if heating_after_clearance else "WATCH",
                    confidence=74 if heating_after_clearance else 56,
                    source="candidate",
                    champion_effect="Challenger only",
                    supports=tuple(
                        item
                        for item in [
                            "rain/showers occurred earlier in the METAR sequence",
                            "the latest METAR shows strong cloud clearance",
                            "heating resumed after clearance"
                            if heating_after_clearance
                            else None,
                        ]
                        if item
                    ),
                    contradictions=(
                        ()
                        if heating_after_clearance
                        else ("renewed heating is not yet confirmed",)
                    ),
                    explanation=(
                        "The heating window may reopen after rain and cloud clearance. "
                        "This candidate remains Challenger-only until repeated OOS evidence."
                    ),
                )
            )

    if taf is not None and bool(getattr(taf, "thunderstorm_risk", False)):
        raw_values = " ".join(str(value) for value in recent.get("raw", pd.Series(dtype=str)).tail(4))
        live_convective = "TS" in raw_values or "CB" in raw_values or "TCU" in raw_values
        states.append(
            RegimeState(
                name="Convective Peak Timing",
                status="WATCH" if live_convective else "PREDICTED",
                confidence=68 if live_convective else 52,
                source="candidate",
                champion_effect="Challenger only",
                supports=tuple(
                    item
                    for item in [
                        "TAF places thunderstorm risk near the trading day",
                        "recent METAR contains TS/CB/TCU" if live_convective else None,
                    ]
                    if item
                ),
                contradictions=("arrival time remains uncertain",),
                explanation=(
                    "A 30–60 minute outflow timing error can change the winning bucket. "
                    "The candidate raises caution but is not promoted from one case."
                ),
            )
        )
    return tuple(states)


def _analog_adjustment(
    analogs: tuple[AnalogDay, ...],
    *,
    maximum_adjustment_c: float,
) -> tuple[float, float | None]:
    if not analogs:
        return 0.0, None
    weights = [max(0.01, analog.similarity**2) for analog in analogs]
    residuals = [analog.residual_c for analog in analogs]
    total = sum(weights)
    mean = sum(value * weight for value, weight in zip(residuals, weights)) / total
    variance = sum(
        weight * (value - mean) ** 2 for value, weight in zip(residuals, weights)
    ) / total
    # Strong shrinkage is deliberate: matching old days is a hypothesis until OOS tested.
    shrinkage = len(analogs) / (len(analogs) + 8.0)
    adjustment = max(
        -maximum_adjustment_c,
        min(maximum_adjustment_c, mean * shrinkage),
    )
    return adjustment, math.sqrt(max(0.0, variance))


def assess_regime_memory(
    nowcast: object,
    snapshots: pd.DataFrame,
    actuals: pd.DataFrame,
    observations: pd.DataFrame,
    variants: pd.DataFrame,
    *,
    airport_profile: dict[str, object],
    timezone_name: str,
    target: date,
    as_of: datetime,
    config: dict[str, object] | None = None,
) -> RegimeMemoryAssessment:
    configured = config or {}
    signature = _signature_from_nowcast(nowcast)
    analogs = find_analog_days(
        snapshots,
        actuals,
        current_signature=signature,
        target=target,
        current_phase=str(getattr(getattr(nowcast, "day_status", None), "phase", "")),
        minimum_similarity=float(configured.get("minimum_similarity", 0.45)),
        maximum_analogs=int(configured.get("maximum_analogs", 8)),
    )
    minimum_analogs = int(configured.get("minimum_analogs", 3))
    maximum_adjustment = float(configured.get("maximum_adjustment_c", 1.0))
    adjustment, analog_residual_spread = _analog_adjustment(
        analogs,
        maximum_adjustment_c=maximum_adjustment,
    )
    base_forecast = float(getattr(nowcast, "final_forecast_mean"))
    base_spread = float(getattr(nowcast, "final_forecast_spread"))
    suggested_spread = max(
        base_spread,
        min(2.0, analog_residual_spread or base_spread),
    )
    local_now = as_of.astimezone(ZoneInfo(timezone_name))
    timing = (
        "D-1"
        if local_now.date() < target
        else "D0 morning"
        if local_now.date() == target and local_now.hour < 12
        else "D0 live"
    )
    promotion = evaluate_promotion_gate(
        variants,
        actuals,
        timing_group=timing,
        minimum_oos_days=int(configured.get("minimum_oos_days", 30)),
        minimum_mae_gain_c=float(configured.get("minimum_mae_gain_c", 0.12)),
        minimum_brier_gain=float(configured.get("minimum_brier_gain", 0.003)),
    )
    states = list(
        _known_regime_states(
            nowcast,
            observations,
            airport_profile=airport_profile,
            timezone_name=timezone_name,
            target=target,
            as_of=as_of,
        )
    )
    challenger_ready = len(analogs) >= minimum_analogs
    if challenger_ready:
        direction = "warmer" if adjustment > 0 else "cooler" if adjustment < 0 else "unchanged"
        states.append(
            RegimeState(
                name="Learned Analog Pattern",
                status="WATCH",
                confidence=min(78, round(35 + 50 * analogs[0].similarity)),
                source="learned",
                champion_effect="Challenger only",
                supports=(
                    f"{len(analogs)} earlier days clear the similarity threshold",
                    f"best match similarity is {analogs[0].similarity:.0%}",
                    f"shadow forecast is {abs(adjustment):.2f} °C {direction}",
                ),
                contradictions=(promotion.explanation,),
                explanation=(
                    "The current METAR/model pattern resembles earlier settled days. "
                    "Its residual correction is stored as an OOS Challenger."
                ),
            )
        )
    priority = {"CONFIRMED": 3, "WATCH": 2, "PREDICTED": 1, "REJECTED": 0}
    actionable = [state for state in states if state.status != "REJECTED"]
    top = max(
        actionable,
        key=lambda state: (priority.get(state.status, 0), state.confidence),
        default=None,
    )
    if top is not None:
        status = top.status
        label = top.name
        confidence = top.confidence
    elif analogs:
        status = "MEMORY BUILDING"
        label = "No mature regime match"
        confidence = round(100 * analogs[0].similarity)
    else:
        status = "INSUFFICIENT HISTORY"
        label = "No mature regime match"
        confidence = 0
    pro_signals = tuple(
        signal
        for state in sorted(
            actionable,
            key=lambda item: (priority.get(item.status, 0), item.confidence),
            reverse=True,
        )[:3]
        for signal in state.supports[:2]
    )[:6]
    contra_signals = tuple(
        signal
        for state in actionable[:3]
        for signal in state.contradictions[:1]
    )[:4]
    allow_promoted = bool(configured.get("allow_promoted", False))
    applied = bool(allow_promoted and promotion.eligible and challenger_ready)
    shadow_only = not applied
    if challenger_ready:
        explanation = (
            f"{len(analogs)} prior days match the current information set; the best match is "
            f"{analogs[0].similarity:.0%} similar. The robust, shrunk analog correction is "
            f"{adjustment:+.2f} °C. {promotion.explanation}"
        )
    else:
        explanation = (
            f"Only {len(analogs)}/{minimum_analogs} comparable settled days are available. "
            "Regime states are explained, but no learned analog Challenger is created yet."
        )
    return RegimeMemoryAssessment(
        status=status,
        label=label,
        confidence=confidence,
        analog_count=len(analogs),
        best_similarity=analogs[0].similarity if analogs else None,
        center_adjustment_c=adjustment if challenger_ready else 0.0,
        suggested_forecast_c=base_forecast + (adjustment if challenger_ready else 0.0),
        suggested_spread_c=suggested_spread,
        shadow_only=shadow_only,
        applied_to_champion=applied,
        challenger_ready=challenger_ready,
        regimes=tuple(states),
        analogs=analogs,
        promotion=promotion,
        pro_signals=pro_signals,
        contra_signals=contra_signals,
        explanation=explanation,
        feature_signature=signature,
    )


def enrich_nowcast_with_regime_memory(
    nowcast: object | None,
    snapshots: pd.DataFrame,
    actuals: pd.DataFrame,
    observations: pd.DataFrame,
    variants: pd.DataFrame,
    *,
    airport_profile: dict[str, object],
    timezone_name: str,
    target: date,
    as_of: datetime,
    config: dict[str, object] | None = None,
) -> object | None:
    if nowcast is None:
        return None
    nowcast = _enrich_with_anchor_transfer(
        nowcast,
        snapshots,
        actuals,
        observations,
        variants,
        airport_profile=airport_profile,
        timezone_name=timezone_name,
        target=target,
        as_of=as_of,
        config=config,
    )
    assessment = assess_regime_memory(
        nowcast,
        snapshots,
        actuals,
        observations,
        variants,
        airport_profile=airport_profile,
        timezone_name=timezone_name,
        target=target,
        as_of=as_of,
        config=config,
    )
    live_features = dict(getattr(nowcast, "live_features", {}) or {})
    live_features.update(
        {
            "regime_memory_status": assessment.status,
            "regime_memory_label": assessment.label,
            "regime_memory_confidence": float(assessment.confidence),
            "regime_memory_analog_count": float(assessment.analog_count),
            "regime_memory_best_similarity": assessment.best_similarity,
            "regime_memory_adjustment_c": assessment.center_adjustment_c,
            "regime_memory_shadow_only": float(assessment.shadow_only),
            "regime_memory_oos_days": float(assessment.promotion.oos_days),
            "regime_memory_promotion_eligible": float(assessment.promotion.eligible),
        }
    )
    challengers = dict(getattr(nowcast, "challenger_variants", {}) or {})
    if not assessment.challenger_ready:
        return replace(
            nowcast,
            live_features=live_features,
            regime_memory=assessment,
        )
    original_variant = {
        "factor": "regime_memory_analog",
        "forecast_mean_c": float(getattr(nowcast, "final_forecast_mean")),
        "spread_c": float(getattr(nowcast, "final_forecast_spread")),
        "probabilities": dict(getattr(nowcast, "probabilities")),
        "forecast_confidence": int(getattr(nowcast, "forecast_confidence")),
    }
    alternative_unconditioned = consensus(
        [assessment.suggested_forecast_c],
        sigma_floor=max(0.65, assessment.suggested_spread_c),
    )
    day_status = getattr(nowcast, "day_status")
    alternative_probabilities = condition_probability_range(
        alternative_unconditioned.probability_by_bucket,
        day_status.minimum_bucket,
        day_status.maximum_bucket,
    )
    alternative_variant = {
        "factor": "regime_memory_analog",
        "forecast_mean_c": assessment.suggested_forecast_c,
        "spread_c": assessment.suggested_spread_c,
        "probabilities": alternative_probabilities,
        "forecast_confidence": int(getattr(nowcast, "forecast_confidence")),
    }
    if not assessment.applied_to_champion:
        challengers["Analog Memory Challenger"] = alternative_variant
        return replace(
            nowcast,
            challenger_variants=challengers,
            live_features=live_features,
            regime_memory=assessment,
        )

    challengers["Without Promoted Regime Memory"] = original_variant
    contributions = dict(getattr(nowcast, "adjustment_contributions"))
    contributions["regime_memory"] = assessment.center_adjustment_c
    contributions["total"] = float(contributions.get("total", 0.0)) + assessment.center_adjustment_c
    stages = dict(getattr(nowcast, "stage_probabilities"))
    stages["Final incl. promoted Regime Memory"] = alternative_probabilities
    return replace(
        nowcast,
        probabilities=alternative_probabilities,
        final_forecast_mean=assessment.suggested_forecast_c,
        final_forecast_spread=assessment.suggested_spread_c,
        stage_probabilities=stages,
        adjustment_contributions=contributions,
        challenger_variants=challengers,
        live_features=live_features,
        regime_memory=assessment,
    )
