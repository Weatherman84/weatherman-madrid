from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from .terminology import checkpoint_stage_label

from .actual_quality import settlement_grade_actuals


@dataclass(frozen=True)
class Consensus:
    mean: float
    median: float
    spread: float
    probability_by_bucket: dict[int, float]


@dataclass(frozen=True)
class HeatSpikeAssessment:
    score: int
    status: str
    adjustment_c: float
    signals: list[str]


@dataclass(frozen=True)
class DayStatus:
    phase: str
    label: str
    is_locked: bool
    minimum_bucket: int | None
    maximum_bucket: int | None
    remaining_heating_c: float | None
    explanation: str


@dataclass(frozen=True)
class MetarScheduleStatus:
    is_pending: bool
    due_at: datetime | None
    explanation: str


@dataclass(frozen=True)
class MarketModelConflict:
    is_conflict: bool
    bucket_label: str | None
    market_probability: float | None
    model_probability: float | None


def consensus(
    values: list[float],
    biases: list[float] | None = None,
    sigma_floor: float = 0.65,
    weights: list[float] | None = None,
) -> Consensus:
    if not values:
        raise ValueError("At least one forecast is required")
    biases = biases or [0.0] * len(values)
    weights = weights or [1.0] * len(values)
    if len(biases) != len(values) or len(weights) != len(values):
        raise ValueError("Values, biases and weights must have the same length")
    corrected = [float(value - bias) for value, bias in zip(values, biases)]
    usable_weights = [max(0.0, float(weight)) for weight in weights]
    if sum(usable_weights) <= 0:
        usable_weights = [1.0] * len(values)
    weight_total = sum(usable_weights)
    mean = sum(value * weight for value, weight in zip(corrected, usable_weights)) / weight_total
    ordered = sorted(zip(corrected, usable_weights), key=lambda item: item[0])
    cumulative = 0.0
    median = ordered[-1][0]
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= weight_total / 2:
            median = value
            break
    variance = (
        sum(weight * (value - mean) ** 2 for value, weight in zip(corrected, usable_weights))
        / weight_total
    )
    spread = max(math.sqrt(variance), sigma_floor)
    lo, hi = math.floor(mean - 4 * spread), math.ceil(mean + 4 * spread)
    probabilities = {}

    def cdf(x: float) -> float:
        return 0.5 * (1 + math.erf((x - mean) / (spread * math.sqrt(2))))

    for bucket in range(lo, hi + 1):
        lower, upper = bucket - 0.5, bucket + 0.5
        probabilities[bucket] = cdf(upper) - cdf(lower)
    total = sum(probabilities.values())
    probabilities = {k: v / total for k, v in probabilities.items()}
    return Consensus(mean, median, spread, probabilities)


def condition_probabilities(
    probabilities: dict[int, float], minimum_bucket: int | None
) -> dict[int, float]:
    return condition_probability_range(probabilities, minimum_bucket, None)


def condition_probability_range(
    probabilities: dict[int, float],
    minimum_bucket: int | None,
    maximum_bucket: int | None,
) -> dict[int, float]:
    if minimum_bucket is None and maximum_bucket is None:
        return probabilities
    possible = {
        bucket: probability
        for bucket, probability in probabilities.items()
        if (minimum_bucket is None or bucket >= minimum_bucket)
        and (maximum_bucket is None or bucket <= maximum_bucket)
    }
    total = sum(possible.values())
    if total <= 0:
        fallback = minimum_bucket if minimum_bucket is not None else maximum_bucket
        return {fallback: 1.0} if fallback is not None else probabilities
    return {bucket: probability / total for bucket, probability in possible.items()}


def metar_schedule_status(
    *,
    as_of: datetime,
    latest_observation_at: datetime | None,
    routine_minutes: list[int] | tuple[int, ...] | None,
    guard_minutes: int = 7,
) -> MetarScheduleStatus:
    """Block trading shortly before a routine METAR and until that report arrives."""
    minutes = sorted({int(value) % 60 for value in (routine_minutes or [])})
    if not minutes:
        return MetarScheduleStatus(False, None, "No routine METAR schedule is configured.")
    now = pd.Timestamp(as_of)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    candidates = []
    hour = now.floor("h")
    for hour_offset in (-1, 0, 1):
        base = hour + timedelta(hours=hour_offset)
        candidates.extend(base + timedelta(minutes=minute) for minute in minutes)
    # A forecast can change materially on the next routine observation. Start
    # protection before its nominal timestamp and keep it active until an
    # observation carrying that timestamp has reached the feed.
    lead = max(0, int(guard_minutes))
    eligible = [
        candidate
        for candidate in candidates
        if candidate <= now + timedelta(minutes=lead)
    ]
    due = max(eligible) if eligible else None
    if due is None:
        return MetarScheduleStatus(False, None, "No routine report is currently due.")
    latest = None
    if latest_observation_at is not None:
        latest = pd.Timestamp(latest_observation_at)
        latest = latest.tz_localize("UTC") if latest.tzinfo is None else latest.tz_convert("UTC")
    pending = latest is None or latest < due
    imminent = due > now
    return MetarScheduleStatus(
        pending,
        due.to_pydatetime(),
        (
            (
                f"The next routine METAR is due within {lead} minutes; "
                "trading is paused until it arrives."
                if imminent
                else "The routine METAR is due but has not reached the official feed yet."
            )
            if pending
            else "The latest scheduled METAR has arrived."
        ),
    )


def assess_day_status(
    *,
    target_date: date,
    local_now: datetime,
    observed_max: float | None,
    latest_observed_temp: float | None = None,
    observation_age_hours: float | None,
    heating_rate: float | None,
    remaining_model_rise: float | None,
    future_radiation_max: float | None,
    resolved_lower_c: float | None = None,
    resolved_upper_c: float | None = None,
) -> DayStatus:
    """Decide whether a daily maximum can still change."""
    minimum_bucket = math.floor(observed_max + 0.5) if observed_max is not None else None
    has_resolution = resolved_lower_c is not None or resolved_upper_c is not None
    if has_resolution:
        resolved_min = math.ceil(resolved_lower_c) if resolved_lower_c is not None else None
        resolved_max = math.floor(resolved_upper_c) if resolved_upper_c is not None else None
        return DayStatus(
            phase="resolved",
            label="Officially resolved",
            is_locked=True,
            minimum_bucket=resolved_min,
            maximum_bucket=resolved_max,
            remaining_heating_c=0.0,
            explanation="The market is closed and its official winning range is available.",
        )

    if target_date < local_now.date():
        if minimum_bucket is not None:
            return DayStatus(
                phase="final",
                label="Final from observations",
                is_locked=True,
                minimum_bucket=minimum_bucket,
                maximum_bucket=minimum_bucket,
                remaining_heating_c=0.0,
                explanation="The local calendar day has ended; the stored METAR maximum is final.",
            )
        return DayStatus(
            phase="incomplete",
            label="Past day · observations missing",
            is_locked=False,
            minimum_bucket=None,
            maximum_bucket=None,
            remaining_heating_c=None,
            explanation="The date has passed, but no METAR maximum is stored for it.",
        )

    if target_date > local_now.date():
        return DayStatus(
            phase="forecast",
            label="Pre-day forecast",
            is_locked=False,
            minimum_bucket=None,
            maximum_bucket=None,
            remaining_heating_c=remaining_model_rise,
            explanation="The target day has not started in the airport's local time.",
        )

    fresh_observation = observation_age_hours is not None and 0 <= observation_age_hours <= 2.0
    late_enough = local_now.hour >= 16
    not_heating = heating_rate is not None and heating_rate <= 0.2
    sunlight_gone = future_radiation_max is not None and future_radiation_max <= 50
    models_done = remaining_model_rise is not None and remaining_model_rise <= 0.4
    cooling_from_peak = (
        observed_max is not None
        and latest_observed_temp is not None
        and latest_observed_temp <= observed_max - 0.5
    )
    decisive_evening = local_now.hour >= 20 and cooling_from_peak
    heating_window_closed = sunlight_gone or decisive_evening
    if (
        minimum_bucket is not None
        and fresh_observation
        and late_enough
        and not_heating
        and models_done
        and heating_window_closed
    ):
        return DayStatus(
            phase="locked",
            label="Peak locked",
            is_locked=True,
            minimum_bucket=minimum_bucket,
            maximum_bucket=minimum_bucket,
            remaining_heating_c=max(0.0, remaining_model_rise),
            explanation=(
                "Fresh METAR observations are cooling and the METAR-anchored model paths "
                "cannot reach the next temperature bucket."
            ),
        )

    if minimum_bucket is None:
        label = "Waiting for METAR"
        explanation = "No observation for the local target day has been stored yet."
    elif not fresh_observation:
        label = "Live · METAR stale"
        explanation = "The last observation is too old to decide whether the daily peak is final."
    else:
        label = "Heating window open"
        blockers = []
        if not late_enough:
            blockers.append("it is still before 16:00 local")
        if not not_heating:
            blockers.append("the METAR trend is still rising or unavailable")
        if not models_done:
            blockers.append("an anchored model path can still reach a higher bucket")
        if not heating_window_closed:
            blockers.append("meaningful solar heating may remain")
        explanation = (
            "Further warming is still possible. Lock blockers: " + "; ".join(blockers) + "."
            if blockers
            else "Further warming is still possible."
        )
    return DayStatus(
        phase="active",
        label=label,
        is_locked=False,
        minimum_bucket=minimum_bucket,
        maximum_bucket=None,
        remaining_heating_c=remaining_model_rise,
        explanation=explanation,
    )


def resolved_market_range(
    markets: pd.DataFrame,
) -> tuple[float | None, float | None, str] | None:
    """Return the sole official winning range once every stored market is closed."""
    if markets.empty or "closed" not in markets or "yes_won" not in markets:
        return None
    latest = markets.copy()
    if "captured_at" in latest:
        latest = latest.sort_values("captured_at").drop_duplicates("market_id", keep="last")
    if not latest.closed.fillna(False).astype(bool).all():
        return None
    winners = latest[latest.yes_won.fillna(False).astype(bool)]
    if len(winners) != 1:
        return None
    winner = winners.iloc[0]

    def optional_number(value: object) -> float | None:
        return float(value) if pd.notna(value) else None

    return (
        optional_number(winner.bucket_low_c),
        optional_number(winner.bucket_high_c),
        str(winner.bucket_label),
    )


def probability_for_range(
    probabilities: dict[int, float],
    lower_c: float | None,
    upper_c: float | None,
) -> float:
    return sum(
        probability
        for bucket, probability in probabilities.items()
        if (lower_c is None or bucket >= lower_c) and (upper_c is None or bucket <= upper_c)
    )


def market_edges(probabilities: dict[int, float], markets: pd.DataFrame) -> pd.DataFrame:
    if markets.empty:
        return pd.DataFrame()
    result = markets.copy()

    def optional_number(value: object) -> float | None:
        return float(value) if pd.notna(value) else None

    result["model_probability"] = result.apply(
        lambda row: probability_for_range(
            probabilities,
            optional_number(row.bucket_low_c),
            optional_number(row.bucket_high_c),
        ),
        axis=1,
    )
    result["buy_price"] = result.best_ask.where(result.best_ask.notna(), result.yes_price).astype(
        float
    )
    result["edge"] = result.model_probability - result.buy_price
    result["signal"] = "No material disagreement"
    actionable = result.best_ask.notna()
    if "closed" in result:
        actionable &= ~result.closed.fillna(False).astype(bool)
    result.loc[actionable & (result.edge >= 0.04), "signal"] = "Watch only"
    result.loc[
        actionable & (result.edge >= 0.08),
        "signal",
    ] = "Uncalibrated disagreement"
    result.loc[
        actionable & (result.edge >= 0.15),
        "signal",
    ] = "Market-model conflict"
    return result.sort_values("edge", ascending=False)


def detect_market_model_conflict(
    probabilities: dict[int, float],
    markets: pd.DataFrame,
    *,
    market_threshold: float = 0.98,
    gap_threshold: float = 0.10,
) -> MarketModelConflict:
    """Use a near-certain market only as a safety brake, never as forecast input."""
    if markets.empty:
        return MarketModelConflict(False, None, None, None)
    latest = markets.copy()
    if "captured_at" in latest:
        latest["captured_at"] = pd.to_datetime(latest.captured_at, utc=True)
        latest = latest.sort_values("captured_at").drop_duplicates("market_id", keep="last")
    if "closed" in latest and latest.closed.fillna(False).astype(bool).all():
        return MarketModelConflict(False, None, None, None)
    leader = latest.sort_values("yes_price", ascending=False).iloc[0]
    market_probability = float(leader.yes_price)
    lower = float(leader.bucket_low_c) if pd.notna(leader.bucket_low_c) else None
    upper = float(leader.bucket_high_c) if pd.notna(leader.bucket_high_c) else None
    model_probability = probability_for_range(probabilities, lower, upper)
    is_conflict = (
        market_probability >= market_threshold
        and market_probability - model_probability >= gap_threshold
    )
    return MarketModelConflict(
        is_conflict,
        str(leader.bucket_label),
        market_probability,
        float(model_probability),
    )


def preferred_station_actuals(
    observations: pd.DataFrame,
    fallback_actuals: pd.DataFrame,
    timezone_by_airport: dict[str, str],
) -> pd.DataFrame:
    """Prefer the relevant airport METAR maximum; use archive actuals only as fallback."""
    frames: list[pd.DataFrame] = []
    if not fallback_actuals.empty:
        fallback = fallback_actuals[["airport", "target_date", "max_temp_c"]].copy()
        fallback["target_date"] = pd.to_datetime(fallback.target_date).dt.date
        fallback["actual_source"] = "archive fallback"
        fallback["source_rank"] = 0
        frames.append(fallback)
    if not observations.empty:
        metar = observations[["airport", "observed_at", "temp_c"]].copy()
        metar["observed_at"] = pd.to_datetime(metar.observed_at, utc=True)
        metar["target_date"] = metar.apply(
            lambda row: row.observed_at.tz_convert(
                timezone_by_airport.get(str(row.airport), "UTC")
            ).date(),
            axis=1,
        )
        metar = metar.groupby(["airport", "target_date"], as_index=False).agg(
            max_temp_c=("temp_c", "max")
        )
        metar["actual_source"] = "airport METAR"
        metar["source_rank"] = 1
        frames.append(metar)
    if not frames:
        return pd.DataFrame(columns=["airport", "target_date", "max_temp_c", "actual_source"])
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values("source_rank").drop_duplicates(
        ["airport", "target_date"], keep="last"
    )
    return combined.drop(columns="source_rank").reset_index(drop=True)


def _lead_bucket(timing: str, hours_to_peak: object) -> str:
    if timing == "D-1 Evening · 20:00":
        return "D-1 Evening · 20:00"
    if timing == "D0 Morning · 10:00":
        return "D0 Morning · 10:00"
    if timing == "D-2 or earlier":
        return "D-2+ collection"
    if timing in {"D-1", "D-1 or earlier"}:
        return "D-1 collection"
    if timing == "D0 morning":
        return "D0 collection before noon"
    if hours_to_peak is None or pd.isna(hours_to_peak):
        return "D0 live · peak unknown"
    hours = float(hours_to_peak)
    if hours > 6:
        return "D0 live · >6 h to peak"
    if hours > 3:
        return "D0 live · 3–6 h to peak"
    if hours > 1:
        return "D0 live · 1–3 h to peak"
    if hours >= 0:
        return "D0 live · <1 h to peak"
    return "D0 live · after median modelled peak"


def fixed_decision_snapshots(
    snapshots: pd.DataFrame,
    timezone_by_airport: dict[str, str],
    *,
    max_age_hours: float = 6.0,
) -> pd.DataFrame:
    """Select the latest snapshot known at two exact local decision cut-offs.

    A snapshot after the cut-off is never used. ``checkpoint_gap_minutes`` exposes
    how far before 20:00/10:00 the source snapshot was captured, so a delayed
    workflow cannot be mistaken for an exact historical information set.
    """
    if snapshots.empty:
        return pd.DataFrame()
    frame = snapshots.copy()
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True)
    frame["target_date"] = pd.to_datetime(frame.target_date).dt.date
    rows: list[pd.Series] = []
    checkpoints = (
        ("D-1 Evening · 20:00", -1, 20),
        ("D0 Morning · 10:00", 0, 10),
    )
    for (airport, target), day in frame.groupby(["airport", "target_date"]):
        timezone_name = timezone_by_airport.get(str(airport))
        if not timezone_name:
            continue
        zone = ZoneInfo(timezone_name)
        for label, day_offset, hour in checkpoints:
            local_day = target + timedelta(days=day_offset)
            decision_at = pd.Timestamp(
                datetime(
                    local_day.year,
                    local_day.month,
                    local_day.day,
                    hour,
                    tzinfo=zone,
                )
            ).tz_convert("UTC")
            minimum_at = pd.Timestamp(
                decision_at.to_pydatetime() - timedelta(hours=float(max_age_hours))
            )
            candidates = day[(day.captured_at <= decision_at) & (day.captured_at >= minimum_at)]
            if candidates.empty:
                continue
            selected = candidates.sort_values("captured_at").iloc[-1].copy()
            selected["timing"] = label
            selected["decision_at"] = decision_at
            selected["checkpoint_gap_minutes"] = (
                decision_at - selected["captured_at"]
            ).total_seconds() / 60
            rows.append(selected)
    return pd.DataFrame(rows).reset_index(drop=True) if rows else pd.DataFrame()


def checkpoint_completeness(
    snapshots: pd.DataFrame,
    timezone_by_airport: dict[str, str],
    *,
    as_of: datetime | None = None,
    lookback_days: int = 7,
    expected_models_by_airport: dict[str, list[str]] | None = None,
) -> pd.DataFrame:
    """Expose expected fixed checkpoints, including missing rows and provenance."""
    now = pd.Timestamp(as_of or datetime.now(timezone.utc))
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    frame = snapshots.copy()
    if not frame.empty:
        frame["target_date"] = pd.to_datetime(frame.target_date).dt.date
        frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
        if "checkpoint_at" in frame:
            frame["checkpoint_at"] = pd.to_datetime(
                frame.checkpoint_at, utc=True, errors="coerce"
            )
    rows: list[dict[str, object]] = []
    for airport, timezone_name in timezone_by_airport.items():
        zone = ZoneInfo(timezone_name)
        local_today = now.tz_convert(zone).date()
        for day_offset in range(max(1, int(lookback_days)) - 1, -1, -1):
            target = local_today - timedelta(days=day_offset)
            schedule = (
                ("D-1 @20", -1, 20),
                ("D0 @06", 0, 6),
                ("D0 @10", 0, 10),
            )
            for label, offset, hour in schedule:
                local_day = target + timedelta(days=offset)
                checkpoint_at = pd.Timestamp(
                    datetime(
                        local_day.year,
                        local_day.month,
                        local_day.day,
                        hour,
                        tzinfo=zone,
                    )
                ).tz_convert("UTC")
                if checkpoint_at > now:
                    continue
                candidates = pd.DataFrame()
                if not frame.empty:
                    candidates = frame[
                        (frame.airport.astype(str) == airport)
                        & (frame.target_date == target)
                    ].copy()
                    if "checkpoint_label" in candidates:
                        candidates = candidates[candidates.checkpoint_label == label]
                    else:
                        candidates = candidates.iloc[0:0]
                selected = (
                    candidates.sort_values("captured_at").iloc[-1]
                    if not candidates.empty
                    else None
                )
                lineage = _checkpoint_lineage_view(
                    selected,
                    (expected_models_by_airport or {}).get(airport),
                )
                rows.append(
                    {
                        "airport": airport,
                        "target_date": target,
                        "checkpoint": label,
                        "checkpoint_at": checkpoint_at,
                        "status": (
                            str(selected.get("checkpoint_status") or "captured")
                            if selected is not None
                            else "unavailable"
                        ),
                        "reconstructed": (
                            bool(selected.get("checkpoint_reconstructed", False))
                            if selected is not None
                            else False
                        ),
                        "source_age_minutes": (
                            lineage.get("source_age_minutes")
                        ),
                        "source_age_min_minutes": (
                            selected.get("source_age_min_minutes")
                            if selected is not None
                            else None
                        ),
                        "source_age_median_minutes": (
                            selected.get("source_age_median_minutes")
                            if selected is not None
                            else None
                        ),
                        "source_age_max_minutes": (
                            selected.get("source_age_max_minutes")
                            if selected is not None
                            else None
                        ),
                        "freshness_status": (
                            str(lineage.get("freshness_status") or "unavailable")
                        ),
                        "evidence_class": (
                            str(selected.get("evidence_class") or "unavailable")
                            if selected is not None
                            else "unavailable"
                        ),
                        "coverage_ratio": (
                            lineage.get("coverage_ratio")
                        ),
                        "expected_models": (
                            int(lineage.get("expected_model_count", 0) or 0)
                        ),
                        "available_models": (
                            int(lineage.get("available_model_count", 0) or 0)
                        ),
                        "used_models": (
                            int(lineage.get("used_model_count", 0) or 0)
                        ),
                        "forecast_run_at": (
                            selected.get("forecast_run_at")
                            if selected is not None
                            else None
                        ),
                        "forecast_available_at": (
                            selected.get("forecast_available_at")
                            if selected is not None
                            else None
                        ),
                        "forecast_fetched_at": (
                            selected.get("forecast_fetched_at")
                            if selected is not None
                            else None
                        ),
                        "models": int(selected.get("model_count", 0))
                        if selected is not None
                        else 0,
                    }
                )
    return pd.DataFrame(rows)


def _checkpoint_lineage_view(
    row: pd.Series | None,
    expected_models: list[str] | None = None,
) -> dict[str, object]:
    """Correct legacy display metadata without rewriting historical evidence."""
    if row is None:
        return {
            "source_age_minutes": None,
            "freshness_status": "unavailable",
            "coverage_ratio": None,
            "expected_model_count": len(expected_models or []),
            "available_model_count": 0,
            "fresh_model_count": 0,
            "used_model_count": 0,
        }
    expected = {str(model) for model in (expected_models or []) if str(model)}
    if not expected:
        try:
            expected = {
                str(model)
                for model in json.loads(str(row.get("expected_models_json") or "[]"))
            }
        except (TypeError, json.JSONDecodeError):
            expected = set()
    try:
        provenance = json.loads(str(row.get("source_provenance_json") or "[]"))
    except (TypeError, json.JSONDecodeError):
        provenance = []
    records = [item for item in provenance if isinstance(item, dict)]
    available = {str(item.get("model")) for item in records if item.get("model")}
    relevant = [item for item in records if str(item.get("model")) in expected]
    ages = sorted(
        float(item["age_at_cutoff_minutes"])
        for item in relevant
        if item.get("age_at_cutoff_minutes") is not None
    )
    if expected and records:
        maximum_age = ages[-1] if ages else None
        coverage = min(1.0, len({str(item.get("model")) for item in relevant}) / len(expected))
        freshness = (
            "unavailable"
            if maximum_age is None
            else "fresh"
            if maximum_age <= 30
            else "aging"
            if maximum_age <= 90
            else "stale"
        )
        return {
            "source_age_minutes": maximum_age,
            "freshness_status": freshness,
            "coverage_ratio": coverage,
            "expected_model_count": len(expected),
            "available_model_count": len(available),
            "fresh_model_count": len(
                {
                    str(item.get("model"))
                    for item in relevant
                    if item.get("age_at_cutoff_minutes") is not None
                    and float(item["age_at_cutoff_minutes"]) <= 90
                }
            ),
            "used_model_count": int(
                row.get("used_model_count")
                if pd.notna(row.get("used_model_count"))
                else row.get("model_count", 0)
                or 0
            ),
        }
    coverage_value = pd.to_numeric(row.get("source_coverage_ratio"), errors="coerce")
    return {
        "source_age_minutes": (
            row.get("source_age_at_checkpoint_minutes")
            if pd.notna(row.get("source_age_at_checkpoint_minutes"))
            else row.get("checkpoint_gap_minutes")
        ),
        "freshness_status": str(row.get("freshness_status") or "unavailable"),
        "coverage_ratio": min(1.0, float(coverage_value))
        if pd.notna(coverage_value)
        else None,
        "expected_model_count": int(row.get("expected_model_count", 0) or 0),
        "available_model_count": int(
            row.get("available_model_count")
            if pd.notna(row.get("available_model_count"))
            else row.get("source_model_count", 0)
            or 0
        ),
        "fresh_model_count": int(
            row.get("fresh_model_count")
            if pd.notna(row.get("fresh_model_count"))
            else 0
        ),
        "used_model_count": int(
            row.get("used_model_count")
            if pd.notna(row.get("used_model_count"))
            else row.get("model_count", 0)
            or 0
        ),
    }


FORECAST_LADDER_HISTORY_STAGES: dict[str, str] = {
    "d1_champion_c": checkpoint_stage_label("d1", "champion"),
    "d0_06_raw_c": checkpoint_stage_label("d0_06", "raw"),
    "d0_06_bias_c": checkpoint_stage_label("d0_06", "bias"),
    "d0_06_metar_c": checkpoint_stage_label("d0_06", "metar"),
    "d0_06_champion_c": checkpoint_stage_label("d0_06", "champion"),
    "d0_10_raw_c": checkpoint_stage_label("d0_10", "raw"),
    "d0_10_bias_c": checkpoint_stage_label("d0_10", "bias"),
    "d0_10_metar_c": checkpoint_stage_label("d0_10", "metar"),
    "d0_10_champion_c": checkpoint_stage_label("d0_10", "champion"),
    "live_raw_c": checkpoint_stage_label("live", "raw"),
    "live_bias_c": checkpoint_stage_label("live", "bias"),
    "live_metar_c": checkpoint_stage_label("live", "metar"),
    "live_champion_c": checkpoint_stage_label("live", "champion"),
}

_LADDER_EVIDENCE_PREFIX: dict[str, str] = {
    "d1_champion_c": "d1",
    "d0_06_raw_c": "d0_06",
    "d0_06_bias_c": "d0_06",
    "d0_06_metar_c": "d0_06",
    "d0_06_champion_c": "d0_06",
    "d0_10_raw_c": "d0_10",
    "d0_10_bias_c": "d0_10",
    "d0_10_metar_c": "d0_10",
    "d0_10_champion_c": "d0_10",
    "live_raw_c": "live",
    "live_bias_c": "live",
    "live_metar_c": "live",
    "live_champion_c": "live",
}


def _ladder_evidence(row: pd.Series | None, *, live: bool = False) -> str:
    if row is None:
        return "missing"
    if live:
        hours = pd.to_numeric(row.get("hours_to_peak"), errors="coerce")
        if pd.notna(hours) and float(hours) < 0:
            return "late/post-peak"
        captured = pd.to_datetime(row.get("captured_at"), utc=True, errors="coerce")
        peak = pd.to_datetime(row.get("expected_peak_at"), utc=True, errors="coerce")
        if pd.notna(captured) and pd.notna(peak) and captured > peak:
            return "late/post-peak"
        return "scheduled"
    status = str(row.get("checkpoint_status") or "").casefold()
    if "reconstructed" in status or bool(row.get("checkpoint_reconstructed", False)):
        return "reconstructed"
    if "scheduled" in status:
        return "scheduled"
    return "missing"


def first_stored_live_champion(
    snapshots: pd.DataFrame,
    *,
    target: date,
    timezone_name: str,
) -> dict[str, object] | None:
    """Return the first stored D0-live Champion after 10:00 airport local time."""
    if snapshots.empty:
        return None
    frame = snapshots.copy()
    frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    selected = frame[
        (frame.target_date == target)
        & frame.timing.fillna("").astype(str).str.startswith("D0 live")
    ].copy()
    if "checkpoint_label" in selected:
        selected = selected[selected.checkpoint_label.fillna("").astype(str).eq("")]
    if selected.empty:
        return None
    selected["captured_local"] = selected.captured_at.dt.tz_convert(timezone_name)
    selected = selected[
        (selected.captured_local.dt.date == target)
        & (selected.captured_local.dt.hour >= 10)
    ]
    if selected.empty:
        return None
    row = selected.sort_values("captured_at").iloc[0]
    value = pd.to_numeric(row.get("final_forecast_c"), errors="coerce")
    if pd.isna(value):
        return None
    latest_metar = pd.to_datetime(row.get("latest_metar_at"), utc=True, errors="coerce")
    return {
        "champion_c": float(value),
        "forecast_at": pd.Timestamp(row.captured_at).to_pydatetime(),
        "latest_metar_at": (
            pd.Timestamp(latest_metar).to_pydatetime() if pd.notna(latest_metar) else None
        ),
        "evidence": _ladder_evidence(row, live=True),
        "freshness": str(row.get("freshness_status") or "unavailable"),
        "source_age_minutes": row.get("source_age_at_checkpoint_minutes"),
    }


def forecast_ladder_history(
    snapshots: pd.DataFrame,
    actuals: pd.DataFrame,
    *,
    timezone_name: str,
    expected_checkpoint_models: list[str] | None = None,
) -> pd.DataFrame:
    """Return one compact chronological evaluation row per final station day.

    The function consumes only the already airport-filtered snapshot and Actual
    frames used by Trading Desk.  It never opens the raw archive itself and never
    mutates OOS counters or forecast configuration.
    """
    final_actuals = settlement_grade_actuals(actuals)
    if final_actuals.empty:
        return pd.DataFrame()
    actual = final_actuals.copy()
    actual["target_date"] = pd.to_datetime(actual.target_date, errors="coerce").dt.date
    actual["actual_c"] = pd.to_numeric(actual.max_temp_c, errors="coerce")
    actual["actual_source"] = (
        actual["source"].astype(str) if "source" in actual else "final station"
    )
    actual = actual.dropna(subset=["target_date", "actual_c"]).sort_values(
        "target_date"
    ).drop_duplicates(["airport", "target_date"], keep="last")

    frame = snapshots.copy()
    if not frame.empty:
        frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
        frame["captured_at"] = pd.to_datetime(
            frame.captured_at, utc=True, errors="coerce"
        )
    rows: list[dict[str, object]] = []
    stage_columns = {
        "raw": "raw_model_mean_c",
        "bias": "bias_corrected_c",
        "metar": "metar_conditioned_c",
        "champion": "final_forecast_c",
    }
    for actual_row in actual.itertuples():
        target = actual_row.target_date
        day = (
            frame[
                (frame.airport.astype(str) == str(actual_row.airport))
                & (frame.target_date == target)
            ].copy()
            if not frame.empty
            else pd.DataFrame()
        )

        def checkpoint(label: str) -> pd.Series | None:
            if day.empty or "checkpoint_label" not in day:
                return None
            normalised = day.checkpoint_label.fillna("").astype(str).str.replace(
                " ", "", regex=False
            ).str.casefold()
            selected = day[normalised == label.replace(" ", "").casefold()]
            return selected.sort_values("captured_at").iloc[-1] if not selected.empty else None

        d1 = checkpoint("D-1@20")
        d006 = checkpoint("D0@06")
        d010 = checkpoint("D0@10")
        live = None
        if not day.empty:
            live_candidates = day[
                day.timing.fillna("").astype(str).str.startswith("D0 live")
            ].copy()
            if "checkpoint_label" in live_candidates:
                live_candidates = live_candidates[
                    live_candidates.checkpoint_label.fillna("").astype(str).eq("")
                ]
            if not live_candidates.empty:
                live = live_candidates.sort_values("captured_at").iloc[0]

        result: dict[str, object] = {
            "airport": str(actual_row.airport),
            "target_date": target,
            "actual_c": float(actual_row.actual_c),
            "actual_status": "final",
            "actual_source": str(actual_row.actual_source),
        }
        selected_rows = {
            "d1": d1,
            "d0_06": d006,
            "d0_10": d010,
            "live": live,
        }
        for prefix, selected in selected_rows.items():
            evidence = _ladder_evidence(selected, live=prefix == "live")
            lineage = (
                _checkpoint_lineage_view(selected, expected_checkpoint_models)
                if prefix != "live"
                else {}
            )
            result[f"{prefix}_evidence"] = evidence
            result[f"{prefix}_freshness"] = (
                str(lineage.get("freshness_status") or "unavailable")
                if prefix != "live"
                else str(selected.get("freshness_status") or "unavailable")
                if selected is not None
                else "unavailable"
            )
            result[f"{prefix}_source_age_minutes"] = (
                lineage.get("source_age_minutes")
                if prefix != "live"
                else selected.get("source_age_at_checkpoint_minutes")
                if selected is not None
                else None
            )
            result[f"{prefix}_forecast_local_time"] = (
                pd.to_datetime(selected.get("captured_at"), utc=True)
                .tz_convert(timezone_name)
                .strftime("%H:%M")
                if selected is not None and pd.notna(selected.get("captured_at"))
                else None
            )
            latest_metar_at = selected.get("latest_metar_at") if selected is not None else None
            result[f"{prefix}_metar_local_time"] = (
                pd.to_datetime(latest_metar_at, utc=True)
                .tz_convert(timezone_name)
                .strftime("%H:%M")
                if latest_metar_at is not None and pd.notna(latest_metar_at)
                else None
            )
        result["live_local_time"] = (
            pd.Timestamp(live.captured_at).tz_convert(timezone_name).strftime("%H:%M")
            if live is not None and pd.notna(live.captured_at)
            else None
        )
        result["d1_champion_c"] = (
            d1.get("final_forecast_c") if d1 is not None else None
        )
        for prefix, selected in (("d0_06", d006), ("d0_10", d010), ("live", live)):
            for stage, source_column in stage_columns.items():
                result[f"{prefix}_{stage}_c"] = (
                    selected.get(source_column) if selected is not None else None
                )
        for forecast_column in FORECAST_LADDER_HISTORY_STAGES:
            value = pd.to_numeric(result.get(forecast_column), errors="coerce")
            result[forecast_column] = float(value) if pd.notna(value) else None
            result[f"{forecast_column.removesuffix('_c')}_error_c"] = (
                float(value) - float(actual_row.actual_c) if pd.notna(value) else None
            )
        evidence_values = [
            value for key, value in result.items() if key.endswith("_evidence")
        ]
        result["regular_oos"] = (
            "reconstructed" not in evidence_values
            and "late/post-peak" not in evidence_values
            and any(value == "scheduled" for value in evidence_values)
        )
        rows.append(result)
    return pd.DataFrame(rows).sort_values("target_date", ascending=False).reset_index(drop=True)


def forecast_ladder_history_metrics(history: pd.DataFrame) -> pd.DataFrame:
    """Summarise each ladder stage without allowing signed errors to cancel MAE."""
    if history.empty:
        return pd.DataFrame(
            columns=["stage", "bias", "mae", "exact_bucket", "within_1c", "n"]
        )
    rows: list[dict[str, object]] = []
    for forecast_column, label in FORECAST_LADDER_HISTORY_STAGES.items():
        error_column = f"{forecast_column.removesuffix('_c')}_error_c"
        errors = pd.to_numeric(history.get(error_column), errors="coerce").dropna()
        rows.append(
            {
                "stage": label,
                "bias": float(errors.mean()) if not errors.empty else None,
                "mae": float(errors.abs().mean()) if not errors.empty else None,
                "exact_bucket": float((errors.abs() < 0.5).mean()) if not errors.empty else None,
                "within_1c": float((errors.abs() <= 1.0).mean()) if not errors.empty else None,
                "n": int(len(errors)),
            }
        )
    return pd.DataFrame(rows)


def forecast_ladder_oos_reliability(history: pd.DataFrame) -> pd.DataFrame:
    """Score each stage only on its own real, pre-peak scheduled evidence."""
    if history.empty:
        return forecast_ladder_history_metrics(history)
    rows: list[dict[str, object]] = []
    for forecast_column, label in FORECAST_LADDER_HISTORY_STAGES.items():
        prefix = _LADDER_EVIDENCE_PREFIX[forecast_column]
        evidence = history.get(f"{prefix}_evidence", pd.Series(index=history.index, dtype=object))
        selected = history[evidence.astype(str).eq("scheduled")]
        error_column = f"{forecast_column.removesuffix('_c')}_error_c"
        errors = pd.to_numeric(selected.get(error_column), errors="coerce").dropna()
        rows.append(
            {
                "stage": label,
                "bias": float(errors.mean()) if not errors.empty else None,
                "mae": float(errors.abs().mean()) if not errors.empty else None,
                "exact_bucket": float((errors.abs() < 0.5).mean()) if not errors.empty else None,
                "within_1c": float((errors.abs() <= 1.0).mean()) if not errors.empty else None,
                "n": int(len(errors)),
                "evidence": "scheduled final-Actual OOS",
            }
        )
    return pd.DataFrame(rows)


MADRID_DECISION_CHECKPOINTS: tuple[str, ...] = (
    "D-1 Evening @20:00",
    "D0 Morning @09:00",
    "First Live @12:00",
    "Late Live @16:00",
)


def fixed_checkpoint_reliability(
    snapshots: pd.DataFrame,
    actuals: pd.DataFrame,
    *,
    checkpoint_labels: tuple[str, ...] = MADRID_DECISION_CHECKPOINTS,
) -> pd.DataFrame:
    """Explain both Champion reliability and every component of its sample size.

    Only final station Actuals paired with scheduled, pre-peak fixed checkpoints
    increment N. Reconstructed, late/post-peak and missing cases stay visible but
    cannot silently improve reliability.
    """
    columns = [
        "checkpoint",
        "exact_bucket",
        "within_1c",
        "mae",
        "bias",
        "n",
        "scheduled_days",
        "reconstructed_days",
        "late_post_peak_days",
        "missing_days",
        "provisional_days",
        "data_through",
    ]
    if snapshots.empty:
        return pd.DataFrame(
            [
                {
                    "checkpoint": label,
                    "exact_bucket": None,
                    "within_1c": None,
                    "mae": None,
                    "bias": None,
                    "n": 0,
                    "scheduled_days": 0,
                    "reconstructed_days": 0,
                    "late_post_peak_days": 0,
                    "missing_days": 0,
                    "provisional_days": 0,
                    "data_through": None,
                }
                for label in checkpoint_labels
            ],
            columns=columns,
        )

    frame = snapshots.copy()
    frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    frame = frame[frame.checkpoint_label.isin(checkpoint_labels)].copy()
    frame = frame.sort_values("captured_at").drop_duplicates(
        ["airport", "target_date", "checkpoint_label"], keep="last"
    )
    if frame.empty:
        return fixed_checkpoint_reliability(
            pd.DataFrame(), actuals, checkpoint_labels=checkpoint_labels
        )
    cohort_start = frame.target_date.dropna().min()

    final_actuals = settlement_grade_actuals(actuals).copy()
    if not final_actuals.empty:
        final_actuals["target_date"] = pd.to_datetime(
            final_actuals.target_date, errors="coerce"
        ).dt.date
        final_actuals["actual_c"] = pd.to_numeric(
            final_actuals.max_temp_c, errors="coerce"
        )
        final_actuals = final_actuals[
            final_actuals.target_date.ge(cohort_start)
        ].dropna(subset=["target_date", "actual_c"])
        final_actuals = final_actuals.sort_values("target_date").drop_duplicates(
            ["airport", "target_date"], keep="last"
        )

    provisional_days = 0
    if not actuals.empty and "source" in actuals:
        provisional = actuals.copy()
        provisional["target_date"] = pd.to_datetime(
            provisional.target_date, errors="coerce"
        ).dt.date
        provisional_days = int(
            (
                provisional.target_date.ge(cohort_start)
                & provisional.source.astype(str).str.contains(
                    "provisional", case=False, na=False
                )
            ).sum()
        )

    rows: list[dict[str, object]] = []
    for label in checkpoint_labels:
        selected = frame[frame.checkpoint_label.eq(label)].copy()
        selected["evidence"] = selected.apply(
            lambda row: (
                "reconstructed"
                if "reconstructed" in str(row.get("checkpoint_status") or "").casefold()
                or bool(row.get("checkpoint_reconstructed", False))
                else "late/post-peak"
                if (
                    label.startswith(("First Live", "Late Live"))
                    and (
                        pd.notna(pd.to_numeric(row.get("hours_to_peak"), errors="coerce"))
                        and float(row.get("hours_to_peak")) < 0
                    )
                )
                else "scheduled"
                if "scheduled" in str(row.get("checkpoint_status") or "").casefold()
                else "missing"
            ),
            axis=1,
        )
        merged = final_actuals.merge(
            selected[
                ["airport", "target_date", "final_forecast_c", "evidence"]
            ],
            on=["airport", "target_date"],
            how="left",
        ) if not final_actuals.empty else pd.DataFrame()
        if not merged.empty:
            merged["evidence"] = merged.evidence.fillna("missing")
            scored = merged[merged.evidence.eq("scheduled")].copy()
            errors = (
                pd.to_numeric(scored.final_forecast_c, errors="coerce")
                - pd.to_numeric(scored.actual_c, errors="coerce")
            ).dropna()
            evidence_counts = merged.evidence.value_counts()
        else:
            errors = pd.Series(dtype=float)
            evidence_counts = pd.Series(dtype=int)
        rows.append(
            {
                "checkpoint": label,
                "exact_bucket": float((errors.abs() < 0.5).mean()) if len(errors) else None,
                "within_1c": float((errors.abs() <= 1.0).mean()) if len(errors) else None,
                "mae": float(errors.abs().mean()) if len(errors) else None,
                "bias": float(errors.mean()) if len(errors) else None,
                "n": int(len(errors)),
                "scheduled_days": int(evidence_counts.get("scheduled", 0)),
                "reconstructed_days": int(evidence_counts.get("reconstructed", 0)),
                "late_post_peak_days": int(evidence_counts.get("late/post-peak", 0)),
                "missing_days": int(evidence_counts.get("missing", 0)),
                "provisional_days": provisional_days,
                "data_through": max(scored.target_date) if not merged.empty and not scored.empty else None,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def forecast_ladder_frame(
    snapshots: pd.DataFrame,
    actuals: pd.DataFrame,
) -> pd.DataFrame:
    """Score all forecast transformations without mixing their information sets."""
    if snapshots.empty or actuals.empty:
        return pd.DataFrame()
    frame = snapshots.copy()
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True)
    frame["target_date"] = pd.to_datetime(frame.target_date).dt.date
    actual = actuals[["airport", "target_date", "max_temp_c", "actual_source"]].copy()
    actual["target_date"] = pd.to_datetime(actual.target_date).dt.date
    merged = frame.merge(actual, on=["airport", "target_date"], how="inner")
    if merged.empty:
        return merged
    merged["lead_bucket"] = merged.apply(
        lambda row: _lead_bucket(str(row.timing), row.get("hours_to_peak")), axis=1
    )
    if {"final_forecast_c", "live_adjustment_c"}.issubset(merged.columns):
        merged["d0_before_live_c"] = (
            pd.to_numeric(merged.final_forecast_c, errors="coerce")
            - pd.to_numeric(merged.live_adjustment_c, errors="coerce").fillna(0.0)
        )
        anchor_values = (
            pd.to_numeric(merged.temp_anchor_adjustment_c, errors="coerce").fillna(0.0)
            if "temp_anchor_adjustment_c" in merged
            else pd.Series(0.0, index=merged.index)
        )
        merged["d0_anchor_only_c"] = merged.d0_before_live_c + anchor_values
    stage_columns = {
        "Raw model mean": "raw_model_mean_c",
        "Weighted raw ensemble": "weighted_raw_c",
        "Bias corrected · equal weight": "bias_corrected_equal_c",
        "Bias corrected · performance weighted": "bias_corrected_c",
        "D0 before live factors": "d0_before_live_c",
        "D0 with Anchor only": "d0_anchor_only_c",
        "METAR conditioned": "metar_conditioned_c",
        "Final incl. TAF": "final_forecast_c",
    }
    rows = []
    for stage, column in stage_columns.items():
        if column not in merged:
            continue
        selected = merged[merged[column].notna()].copy()
        if stage.startswith("D0 "):
            selected = selected[
                ~selected.timing.astype(str).str.startswith("D-1", na=False)
            ]
        if selected.empty:
            continue
        selected["stage"] = stage
        selected["forecast_c"] = selected[column].astype(float)
        rows.append(selected)
    if not rows:
        return pd.DataFrame()
    scored = pd.concat(rows, ignore_index=True)
    # One independent airport-day observation per comparable information bucket.
    scored = scored.sort_values("captured_at").drop_duplicates(
        ["airport", "target_date", "timing", "lead_bucket", "stage"], keep="last"
    )
    scored["error"] = scored.forecast_c - scored.max_temp_c
    scored["abs_error"] = scored.error.abs()
    return scored


def forecast_ladder_metrics(scored: pd.DataFrame) -> pd.DataFrame:
    if scored.empty:
        return pd.DataFrame()
    rows = []
    for keys, frame in scored.groupby(["airport", "timing", "lead_bucket", "stage"], dropna=False):
        airport, timing, lead_bucket, stage = keys
        rows.append(
            {
                "airport": airport,
                "timing": timing,
                "lead_bucket": lead_bucket,
                "stage": stage,
                "n_days": int(frame.target_date.nunique()),
                "bias": float(frame.error.mean()),
                "mae": float(frame.abs_error.mean()),
                "rmse": float(math.sqrt((frame.error**2).mean())),
                "exact_hit": float((frame.abs_error < 0.5).mean()),
                "within_1c": float((frame.abs_error <= 1).mean()),
            }
        )
    result = pd.DataFrame(rows)
    raw = result[result.stage == "Raw model mean"][
        ["airport", "timing", "lead_bucket", "mae"]
    ].rename(columns={"mae": "raw_mae"})
    result = result.merge(raw, on=["airport", "timing", "lead_bucket"], how="left")
    result["mae_gain_vs_raw"] = result.raw_mae - result.mae
    order = {
        "Raw model mean": 0,
        "Weighted raw ensemble": 1,
        "Bias corrected · equal weight": 2,
        "Bias corrected · performance weighted": 3,
        "D0 before live factors": 4,
        "D0 with Anchor only": 5,
        "METAR conditioned": 6,
        "Final incl. TAF": 7,
    }
    result["stage_order"] = result.stage.map(order)
    return result.sort_values(["airport", "timing", "lead_bucket", "stage_order"]).drop(
        columns="stage_order"
    )


def paired_d1_d0_reliability(
    snapshots: pd.DataFrame,
    actuals: pd.DataFrame,
    timezone_by_airport: dict[str, str],
) -> pd.DataFrame:
    """Compare D-1 and D0 transformations on exactly the same settled airport-days."""
    fixed = fixed_decision_snapshots(snapshots, timezone_by_airport)
    if fixed.empty or actuals.empty:
        return pd.DataFrame()
    frame = fixed.copy()
    frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
    actual = actuals[["airport", "target_date", "max_temp_c"]].copy()
    actual["target_date"] = pd.to_datetime(actual.target_date, errors="coerce").dt.date
    actual["max_temp_c"] = pd.to_numeric(actual.max_temp_c, errors="coerce")
    actual = actual.dropna(subset=["target_date", "max_temp_c"]).drop_duplicates(
        ["airport", "target_date"], keep="last"
    )
    rows: list[dict[str, object]] = []
    for keys, day in frame.groupby(["airport", "target_date"]):
        airport, target_day = keys
        d1 = day[day.timing == "D-1 Evening · 20:00"]
        d0 = day[day.timing == "D0 Morning · 10:00"]
        truth = actual[
            (actual.airport == airport) & (actual.target_date == target_day)
        ]
        if d1.empty or d0.empty or truth.empty:
            continue
        d1_row = d1.sort_values("captured_at").iloc[-1]
        d0_row = d0.sort_values("captured_at").iloc[-1]
        d1_forecast = pd.to_numeric(
            pd.Series([d1_row.get("final_forecast_c")]), errors="coerce"
        ).iloc[0]
        d0_final = pd.to_numeric(
            pd.Series([d0_row.get("final_forecast_c")]), errors="coerce"
        ).iloc[0]
        if pd.isna(d1_forecast) or pd.isna(d0_final):
            continue
        live_adjustment = float(d0_row.get("live_adjustment_c", 0.0) or 0.0)
        anchor_adjustment = float(d0_row.get("temp_anchor_adjustment_c", 0.0) or 0.0)
        d0_without_live = float(d0_final) - live_adjustment
        predictions = (
            ("D-1 @20 Champion", float(d1_forecast)),
            ("D0 @10 before live factors", d0_without_live),
            ("D0 @10 with Anchor only", d0_without_live + anchor_adjustment),
            ("D0 @10 complete Nowcast", float(d0_final)),
        )
        actual_value = float(truth.max_temp_c.iloc[-1])
        for stage, forecast in predictions:
            error = forecast - actual_value
            rows.append(
                {
                    "airport": airport,
                    "target_date": target_day,
                    "stage": stage,
                    "forecast_c": forecast,
                    "actual_c": actual_value,
                    "error": error,
                    "abs_error": abs(error),
                }
            )
    if not rows:
        return pd.DataFrame()
    scored = pd.DataFrame(rows)
    summary_rows = []
    for (airport, stage), group in scored.groupby(["airport", "stage"], sort=False):
        summary_rows.append(
            {
                "airport": airport,
                "stage": stage,
                "n_days": int(group.target_date.nunique()),
                "bias": float(group.error.mean()),
                "mae": float(group.abs_error.mean()),
                "rmse": float(math.sqrt((group.error**2).mean())),
                "exact_hit": float((group.abs_error < 0.5).mean()),
                "within_1c": float((group.abs_error <= 1.0).mean()),
            }
        )
    summary = pd.DataFrame(summary_rows)
    d1_mae = summary[summary.stage == "D-1 @20 Champion"][["airport", "mae"]].rename(
        columns={"mae": "d1_mae"}
    )
    no_live_mae = summary[
        summary.stage == "D0 @10 before live factors"
    ][["airport", "mae"]].rename(columns={"mae": "d0_no_live_mae"})
    summary = summary.merge(d1_mae, on="airport", how="left").merge(
        no_live_mae, on="airport", how="left"
    )
    summary["mae_change_vs_d1"] = summary.mae - summary.d1_mae
    summary["mae_change_vs_d0_no_live"] = summary.mae - summary.d0_no_live_mae
    order = {
        "D-1 @20 Champion": 0,
        "D0 @10 before live factors": 1,
        "D0 @10 with Anchor only": 2,
        "D0 @10 complete Nowcast": 3,
    }
    summary["stage_order"] = summary.stage.map(order)
    return summary.sort_values(["airport", "stage_order"]).drop(columns="stage_order")


def _rolling_biases_and_weights(
    history: list[tuple[date, str, float]],
    *,
    weight_lookback_days: int = 90,
    full_reliability_days: int = 30,
) -> tuple[dict[str, float], dict[str, float]]:
    """Compute the existing bias and weight rules without per-day DataFrame scans."""
    if not history:
        return {}, {}

    errors_by_model: dict[str, list[float]] = {}
    for _, model, error in history:
        errors_by_model.setdefault(model, []).append(float(error))
    biases = {
        model: sum(errors) / len(errors) for model, errors in errors_by_model.items() if errors
    }

    latest_history_date = max(item[0] for item in history)
    weight_cutoff = latest_history_date - timedelta(days=weight_lookback_days - 1)
    weight_history = [item for item in history if item[0] >= weight_cutoff]
    weight_errors: dict[str, list[float]] = {}
    weight_dates: dict[str, set[date]] = {}
    for target, model, error in weight_history:
        weight_errors.setdefault(model, []).append(float(error))
        weight_dates.setdefault(model, set()).add(target)
    residual_mae = {}
    for model, errors in weight_errors.items():
        center = sum(errors) / len(errors)
        residual_mae[model] = sum(abs(error - center) for error in errors) / len(errors)
    if not residual_mae:
        return biases, {}

    baseline_mae = max(0.25, float(statistics.median(residual_mae.values())))
    raw_weights: dict[str, float] = {}
    for model, mae in residual_mae.items():
        reliability = min(
            1.0,
            float(len(weight_dates.get(model, set()))) / full_reliability_days,
        )
        relative_precision = ((baseline_mae + 0.35) / (mae + 0.35)) ** 2
        raw_weights[model] = max(
            0.4,
            min(2.5, 1.0 + reliability * (relative_precision - 1.0)),
        )
    total = sum(raw_weights.values())
    weights = {model: value / total for model, value in raw_weights.items()}
    return biases, weights


def _weighted_average(values: list[float], weights: list[float]) -> float:
    usable = [max(0.0, float(weight)) for weight in weights]
    if sum(usable) <= 0:
        usable = [1.0] * len(values)
    total = sum(usable)
    return sum(value * weight for value, weight in zip(values, usable)) / total


def historical_d1_ladder(
    forecasts: pd.DataFrame,
    actuals: pd.DataFrame,
    lookback_days: int = 90,
) -> pd.DataFrame:
    """Leakage-safe reconstruction of four D-1 ensemble stages."""
    if forecasts.empty or actuals.empty:
        return pd.DataFrame()
    d1 = forecasts[forecasts.horizon == "D-1"].copy()
    if d1.empty:
        return pd.DataFrame()
    d1["target_date"] = pd.to_datetime(d1.target_date).dt.date
    d1 = d1.sort_values("run_at").drop_duplicates(["airport", "model", "target_date"], keep="last")
    actual = actuals[["airport", "target_date", "max_temp_c"]].copy()
    actual["target_date"] = pd.to_datetime(actual.target_date).dt.date
    paired = d1.merge(
        actual,
        on=["airport", "target_date"],
        how="inner",
        suffixes=("", "_actual"),
    )
    if paired.empty:
        return pd.DataFrame()
    paired["error"] = paired.max_temp_c - paired.max_temp_c_actual
    rows: list[dict] = []
    stages = (
        "Raw model mean",
        "Weighted raw ensemble",
        "Bias corrected · equal weight",
        "Bias corrected · performance weighted",
    )
    for airport, airport_frame in paired.groupby("airport"):
        daily: dict[date, list] = {}
        for item in airport_frame.sort_values("target_date").itertuples(index=False):
            daily.setdefault(item.target_date, []).append(item)
        history: list[tuple[date, str, float]] = []
        for target, today in daily.items():
            minimum_date = target - timedelta(days=lookback_days)
            history = [item for item in history if item[0] >= minimum_date]
            bias_map, weights = _rolling_biases_and_weights(history)
            fallback = float(statistics.median(weights.values())) if weights else 1.0
            values = [float(row.max_temp_c) for row in today]
            biases = [float(bias_map.get(str(row.model), 0.0)) for row in today]
            model_weights = [float(weights.get(str(row.model), fallback)) for row in today]
            corrected = [value - bias for value, bias in zip(values, biases)]
            predictions = (
                sum(values) / len(values),
                _weighted_average(values, model_weights),
                sum(corrected) / len(corrected),
                _weighted_average(corrected, model_weights),
            )
            actual_value = float(today[0].max_temp_c_actual)
            for stage, prediction in zip(stages, predictions):
                rows.append(
                    {
                        "airport": str(airport),
                        "target_date": target,
                        "timing": "D-1 · 24h lead",
                        "lead_bucket": "D-1 · 24h lead",
                        "stage": stage,
                        "forecast_c": float(prediction),
                        "max_temp_c": actual_value,
                        "error": float(prediction) - actual_value,
                        "abs_error": abs(float(prediction) - actual_value),
                    }
                )
            history.extend((target, str(row.model), float(row.error)) for row in today)
    return pd.DataFrame(rows)


def live_factor_diagnostics(
    snapshots: pd.DataFrame,
    actuals: pd.DataFrame,
) -> pd.DataFrame:
    """Measure each conservative live factor as a cumulative walk through the center."""
    if snapshots.empty or actuals.empty:
        return pd.DataFrame()
    required = [
        "temp_anchor_adjustment_c",
        "dryness_adjustment_c",
        "dewpoint_trend_adjustment_c",
        "cloud_adjustment_c",
        "heating_rate_adjustment_c",
        "recent_error_adjustment_c",
        "radiation_adjustment_c",
        "wind_adjustment_c",
        "run_trend_adjustment_c",
        "late_dry_mixing_adjustment_c",
        "failed_convection_adjustment_c",
        "clear_sky_override_adjustment_c",
    ]
    if any(column not in snapshots for column in required):
        return pd.DataFrame()
    frame = snapshots.copy()
    frame["target_date"] = pd.to_datetime(frame.target_date).dt.date
    actual = actuals[["airport", "target_date", "max_temp_c"]].copy()
    actual["target_date"] = pd.to_datetime(actual.target_date).dt.date
    merged = frame.merge(actual, on=["airport", "target_date"], how="inner")
    merged = merged[merged.metar_conditioned_c.notna()].copy()
    if merged.empty:
        return pd.DataFrame()
    merged["information_set"] = merged.apply(
        lambda row: _lead_bucket(str(row.timing), row.get("hours_to_peak")),
        axis=1,
    )
    labels = {
        "temp_anchor_adjustment_c": "Temperature anchor",
        "dryness_adjustment_c": "Dryness surprise",
        "dewpoint_trend_adjustment_c": "Observed dewpoint trend",
        "cloud_adjustment_c": "Cloud surprise",
        "heating_rate_adjustment_c": "Heating-rate surprise",
        "recent_error_adjustment_c": "Recent station error",
        "radiation_adjustment_c": "Radiation proxy",
        "wind_adjustment_c": "Observed wind sector",
        "run_trend_adjustment_c": "Model-run trend",
        "late_dry_mixing_adjustment_c": "Late dry mixing",
        "failed_convection_adjustment_c": "Failed convection",
        "clear_sky_override_adjustment_c": "Clear-sky override",
    }
    rows = []
    for (airport, information_set), airport_frame in merged.groupby(["airport", "information_set"]):
        running = airport_frame.bias_corrected_c.astype(float).copy()
        previous_mae = float((running - airport_frame.max_temp_c).abs().mean())
        rows.append(
            {
                "airport": airport,
                "information_set": information_set,
                "factor": "Bias-corrected baseline",
                "n_snapshots": len(airport_frame),
                "n_days": int(airport_frame.target_date.nunique()),
                "average_contribution_c": 0.0,
                "cumulative_mae": previous_mae,
                "marginal_mae_gain": 0.0,
            }
        )
        for column in required:
            contribution = pd.to_numeric(airport_frame[column], errors="coerce").fillna(0.0)
            running = running + contribution
            mae = float((running - airport_frame.max_temp_c).abs().mean())
            rows.append(
                {
                    "airport": airport,
                    "information_set": information_set,
                    "factor": labels[column],
                    "n_snapshots": len(airport_frame),
                    "n_days": int(airport_frame.target_date.nunique()),
                    "average_contribution_c": float(contribution.mean()),
                    "cumulative_mae": mae,
                    "marginal_mae_gain": previous_mae - mae,
                }
            )
            previous_mae = mae
    return pd.DataFrame(rows)


def _variant_probabilities(value: object) -> dict[int, float]:
    if isinstance(value, dict):
        parsed = value
    else:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if not isinstance(parsed, dict):
        return {}
    probabilities: dict[int, float] = {}
    for bucket, probability in parsed.items():
        try:
            probabilities[int(bucket)] = max(0.0, float(probability))
        except (TypeError, ValueError):
            continue
    total = sum(probabilities.values())
    return (
        {bucket: probability / total for bucket, probability in probabilities.items()}
        if total > 0
        else {}
    )


def _calibration_error(samples: list[tuple[float, int]], bins: int = 10) -> float | None:
    if not samples:
        return None
    total = len(samples)
    error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        selected = [
            (probability, outcome)
            for probability, outcome in samples
            if lower <= probability < upper or (index == bins - 1 and probability == 1.0)
        ]
        if not selected:
            continue
        mean_probability = sum(item[0] for item in selected) / len(selected)
        observed_rate = sum(item[1] for item in selected) / len(selected)
        error += len(selected) / total * abs(mean_probability - observed_rate)
    return float(error)


def champion_challenger_metrics(
    variants: pd.DataFrame,
    actuals: pd.DataFrame,
) -> pd.DataFrame:
    """Pair every active challenger with its same-snapshot champion and score both."""
    if variants.empty or actuals.empty:
        return pd.DataFrame()
    frame = variants.copy()
    required = {
        "airport",
        "target_date",
        "captured_at",
        "timing",
        "variant",
        "factor",
        "forecast_c",
        "probabilities_json",
        "forecast_confidence",
    }
    if not required.issubset(frame.columns):
        return pd.DataFrame()
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True)
    frame["target_date"] = pd.to_datetime(frame.target_date).dt.date
    frame["information_set"] = frame.apply(
        lambda row: _lead_bucket(str(row.timing), None),
        axis=1,
    )
    champions = frame[frame.variant == "Champion"].copy()
    challengers = frame[frame.variant != "Champion"].copy()
    if champions.empty or challengers.empty:
        return pd.DataFrame()
    champion_columns = [
        "airport",
        "target_date",
        "captured_at",
        "forecast_c",
        "probabilities_json",
        "forecast_confidence",
    ]
    paired = challengers.merge(
        champions[champion_columns],
        on=["airport", "target_date", "captured_at"],
        how="inner",
        suffixes=("_challenger", "_champion"),
    )
    actual = actuals[["airport", "target_date", "max_temp_c"]].copy()
    actual["target_date"] = pd.to_datetime(actual.target_date).dt.date
    paired = paired.merge(actual, on=["airport", "target_date"], how="inner")
    if paired.empty:
        return pd.DataFrame()
    paired = paired.sort_values("captured_at").drop_duplicates(
        ["airport", "target_date", "information_set", "factor"],
        keep="last",
    )

    def score_side(group: pd.DataFrame, side: str) -> dict[str, float | None]:
        forecast = pd.to_numeric(group[f"forecast_c_{side}"], errors="coerce")
        actual_values = pd.to_numeric(group.max_temp_c, errors="coerce")
        error = forecast - actual_values
        actual_buckets = actual_values.map(lambda value: math.floor(float(value) + 0.5))
        forecast_buckets = forecast.map(lambda value: math.floor(float(value) + 0.5))
        brier_values: list[float] = []
        log_losses: list[float] = []
        calibration_samples: list[tuple[float, int]] = []
        for probability_value, actual_bucket in zip(
            group[f"probabilities_json_{side}"],
            actual_buckets,
        ):
            probabilities = _variant_probabilities(probability_value)
            if not probabilities:
                continue
            buckets = set(probabilities) | {int(actual_bucket)}
            brier_values.append(
                sum(
                    (
                        probabilities.get(bucket, 0.0)
                        - (1.0 if bucket == int(actual_bucket) else 0.0)
                    )
                    ** 2
                    for bucket in buckets
                )
            )
            log_losses.append(
                -math.log(max(1e-12, probabilities.get(int(actual_bucket), 0.0)))
            )
            calibration_samples.extend(
                (
                    probability,
                    int(bucket == int(actual_bucket)),
                )
                for bucket, probability in probabilities.items()
            )
        confidence = pd.to_numeric(
            group[f"forecast_confidence_{side}"],
            errors="coerce",
        )
        exact = forecast_buckets == actual_buckets
        high = confidence >= 65
        low = confidence < 65
        return {
            "bias": float(error.mean()),
            "mae": float(error.abs().mean()),
            "rmse": float(math.sqrt((error**2).mean())),
            "exact_hit": float(exact.mean()),
            "within_1c": float((error.abs() <= 1.0).mean()),
            "brier_score": float(pd.Series(brier_values).mean())
            if brier_values
            else None,
            "log_loss": float(pd.Series(log_losses).mean()) if log_losses else None,
            "calibration_error": _calibration_error(calibration_samples),
            "high_confidence_hit": float(exact[high].mean()) if high.any() else None,
            "low_confidence_hit": float(exact[low].mean()) if low.any() else None,
        }

    rows = []
    for (airport, information_set, factor, variant), group in paired.groupby(
        ["airport", "information_set", "factor", "variant"],
        dropna=False,
    ):
        champion = score_side(group, "champion")
        challenger = score_side(group, "challenger")
        days = int(group.target_date.nunique())
        evidence = (
            "Stronger"
            if days >= 60
            else "Usable"
            if days >= 30
            else "Early tendency"
            if days >= 10
            else "Case studies only"
        )
        row: dict[str, object] = {
            "airport": airport,
            "information_set": information_set,
            "factor": factor,
            "challenger": variant,
            "n_days": days,
            "evidence": evidence,
        }
        for metric, value in champion.items():
            row[f"champion_{metric}"] = value
        for metric, value in challenger.items():
            row[f"challenger_{metric}"] = value
        row["mae_gain"] = (
            float(challenger["mae"]) - float(champion["mae"])
        )
        row["brier_gain"] = (
            float(challenger["brier_score"]) - float(champion["brier_score"])
            if challenger["brier_score"] is not None
            and champion["brier_score"] is not None
            else None
        )
        row["log_loss_gain"] = (
            float(challenger["log_loss"]) - float(champion["log_loss"])
            if challenger["log_loss"] is not None
            and champion["log_loss"] is not None
            else None
        )
        row["exact_hit_gain"] = (
            float(champion["exact_hit"]) - float(challenger["exact_hit"])
        )
        row["mae_winner"] = (
            "Champion"
            if row["mae_gain"] > 1e-9
            else "Challenger"
            if row["mae_gain"] < -1e-9
            else "Tie"
        )
        rows.append(row)
    return pd.DataFrame(rows)


def champion_challenger_trading_metrics(
    variants: pd.DataFrame,
    markets: pd.DataFrame,
) -> pd.DataFrame:
    """Compare guarded ask-based paper entries for champion and challengers."""
    if variants.empty or markets.empty:
        return pd.DataFrame()
    frame = variants.copy()
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True)
    frame["target_date"] = pd.to_datetime(frame.target_date).dt.date
    champions = frame[frame.variant == "Champion"].copy()
    challengers = frame[frame.variant != "Champion"].copy()
    if champions.empty or challengers.empty:
        return pd.DataFrame()
    pairs = challengers.merge(
        champions[
            [
                "airport",
                "target_date",
                "captured_at",
                "probabilities_json",
                "forecast_confidence",
            ]
        ],
        on=["airport", "target_date", "captured_at"],
        how="inner",
        suffixes=("_challenger", "_champion"),
    )
    market_frame = markets.copy()
    market_frame["captured_at"] = pd.to_datetime(market_frame.captured_at, utc=True)
    market_frame["target_date"] = pd.to_datetime(market_frame.target_date).dt.date
    outcomes = resolved_market_outcomes(market_frame)
    if pairs.empty or outcomes.empty:
        return pd.DataFrame()
    winner_map = dict(
        zip(outcomes.market_id.astype(str), outcomes.yes_won.astype(bool))
    )

    def paper_entry(
        probabilities_value: object,
        confidence_value: object,
        snapshot_markets: pd.DataFrame,
    ) -> dict[str, object] | None:
        probabilities = _variant_probabilities(probabilities_value)
        if not probabilities or int(confidence_value) < 65:
            return None
        comparison = market_edges(probabilities, snapshot_markets)
        if comparison.empty:
            return None
        actionable = comparison[comparison.best_ask.notna()].copy()
        if "closed" in actionable:
            actionable = actionable[~actionable.closed.fillna(False).astype(bool)]
        if actionable.empty:
            return None
        actionable["fee_per_share"] = (
            actionable.buy_price * 0.05 * (1.0 - actionable.buy_price)
        )
        actionable["all_in_price"] = actionable.buy_price + actionable.fee_per_share
        actionable["net_edge"] = (
            actionable.model_probability - actionable.all_in_price - 0.02
        )
        actionable = actionable[
            (actionable.net_edge >= 0.05)
            & (
                actionable.spread.isna()
                | (pd.to_numeric(actionable.spread, errors="coerce") <= 0.12)
            )
        ]
        if actionable.empty:
            return None
        best = actionable.sort_values("net_edge", ascending=False).iloc[0]
        market_id = str(best.market_id)
        if market_id not in winner_map:
            return None
        won = bool(winner_map[market_id])
        all_in = float(best.all_in_price)
        return {
            "market_id": market_id,
            "net_edge": float(best.net_edge),
            "won": won,
            "pnl": 1.0 / all_in - 1.0 if won else -1.0,
        }

    entry_rows: list[dict[str, object]] = []
    for pair in pairs.itertuples():
        snapshot_markets = market_frame[
            (market_frame.airport == pair.airport)
            & (market_frame.target_date == pair.target_date)
            & (market_frame.captured_at == pair.captured_at)
        ]
        if snapshot_markets.empty:
            continue
        information_set = _lead_bucket(str(pair.timing), None)
        for side in ("champion", "challenger"):
            entry = paper_entry(
                getattr(pair, f"probabilities_json_{side}"),
                getattr(pair, f"forecast_confidence_{side}"),
                snapshot_markets,
            )
            if entry is None:
                continue
            entry_rows.append(
                {
                    "airport": pair.airport,
                    "target_date": pair.target_date,
                    "captured_at": pair.captured_at,
                    "information_set": information_set,
                    "factor": pair.factor,
                    "challenger": pair.variant,
                    "side": side,
                    **entry,
                }
            )
    if not entry_rows:
        return pd.DataFrame()
    entries = pd.DataFrame(entry_rows)
    entries = entries.sort_values("captured_at").drop_duplicates(
        [
            "airport",
            "target_date",
            "information_set",
            "factor",
            "challenger",
            "side",
        ],
        keep="first",
    )
    summaries = []
    for keys, group in entries.groupby(
        ["airport", "information_set", "factor", "challenger", "side"],
        dropna=False,
    ):
        airport, information_set, factor, challenger, side = keys
        summaries.append(
            {
                "airport": airport,
                "information_set": information_set,
                "factor": factor,
                "challenger": challenger,
                "side": side,
                "entries": len(group),
                "independent_days": int(group.target_date.nunique()),
                "hit_rate": float(group.won.mean()),
                "average_net_edge": float(group.net_edge.mean()),
                "net_pnl": float(group.pnl.sum()),
                "roi": float(group.pnl.sum() / len(group)),
            }
        )
    summary = pd.DataFrame(summaries)
    if summary.empty:
        return summary
    wide = summary.pivot(
        index=["airport", "information_set", "factor", "challenger"],
        columns="side",
        values=[
            "entries",
            "independent_days",
            "hit_rate",
            "average_net_edge",
            "net_pnl",
            "roi",
        ],
    )
    wide.columns = [f"{side}_{metric}" for metric, side in wide.columns]
    result = wide.reset_index()
    for side in ("champion", "challenger"):
        for metric in (
            "entries",
            "independent_days",
            "hit_rate",
            "average_net_edge",
            "net_pnl",
            "roi",
        ):
            column = f"{side}_{metric}"
            if column not in result:
                result[column] = pd.NA
    return result


def settled_basket_performance(
    baskets: pd.DataFrame,
    markets: pd.DataFrame,
) -> pd.DataFrame:
    """Settle one first simultaneous SHADOW BASKET per independent airport-day."""
    columns = [
        "airport",
        "target_date",
        "event_slug",
        "captured_at",
        "timing",
        "strategy",
        "market_count",
        "fair_probability",
        "total_cost",
        "net_edge",
        "won",
        "pnl",
        "roi",
        "cumulative_pnl",
    ]
    if baskets.empty or markets.empty:
        return pd.DataFrame(columns=columns)
    entries = baskets[baskets.status == "SHADOW BASKET"].copy()
    if entries.empty:
        return pd.DataFrame(columns=columns)
    entries["captured_at"] = pd.to_datetime(entries.captured_at, utc=True)
    entries["target_date"] = pd.to_datetime(entries.target_date).dt.date
    entries = entries.sort_values("captured_at").drop_duplicates(
        ["airport", "target_date", "strategy"],
        keep="first",
    )
    outcomes = resolved_market_outcomes(markets)
    if outcomes.empty:
        return pd.DataFrame(columns=columns)
    won_ids = set(outcomes.loc[outcomes.yes_won.astype(bool), "market_id"].astype(str))

    def selected_ids(value: object) -> set[str]:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return set()
        return {str(item) for item in parsed} if isinstance(parsed, list) else set()

    resolved_ids = set(outcomes.market_id.astype(str))
    entries["_selected_ids"] = entries.market_ids_json.map(selected_ids)
    entries = entries[
        entries._selected_ids.map(
            lambda values: bool(values) and values.issubset(resolved_ids)
        )
    ].copy()
    if entries.empty:
        return pd.DataFrame(columns=columns)
    entries["won"] = entries._selected_ids.map(
        lambda values: bool(values & won_ids)
    )
    entries["pnl"] = entries.apply(
        lambda row: 1.0 - float(row.total_cost)
        if row.won
        else -float(row.total_cost),
        axis=1,
    )
    entries["roi"] = entries.pnl / entries.total_cost
    entries = entries.sort_values(["target_date", "captured_at"])
    entries["cumulative_pnl"] = entries.groupby("strategy").pnl.cumsum()
    return entries[columns].reset_index(drop=True)


def settled_strategy_performance(
    strategies: pd.DataFrame,
    markets: pd.DataFrame,
    stake: float = 1.0,
) -> pd.DataFrame:
    """Settle one consensus-bucket entry per strategy, timing and airport-day."""
    columns = [
        "airport",
        "target_date",
        "market_id",
        "bucket_label",
        "captured_at",
        "timing",
        "strategy",
        "buy_price",
        "won",
        "pnl",
        "cumulative_pnl",
    ]
    if strategies.empty or markets.empty:
        return pd.DataFrame(columns=columns)
    candidates = strategies.copy()
    candidates["captured_at"] = pd.to_datetime(candidates.captured_at, utc=True)
    candidates["buy_price"] = pd.to_numeric(candidates.buy_price, errors="coerce")
    candidates = candidates[(candidates.buy_price > 0) & (candidates.buy_price < 1)]
    if candidates.empty:
        return pd.DataFrame(columns=columns)
    entries = candidates.sort_values("captured_at").drop_duplicates(
        ["airport", "target_date", "timing", "strategy"], keep="first"
    )
    outcomes = resolved_market_outcomes(markets)
    settled = entries.merge(outcomes, on="market_id", how="inner")
    if settled.empty:
        return pd.DataFrame(columns=columns)
    settled["won"] = settled.yes_won.astype(bool)
    settled["pnl"] = settled.apply(
        lambda row: stake / row.buy_price - stake if row.won else -stake,
        axis=1,
    )
    settled = settled.sort_values(["target_date", "captured_at"])
    settled["cumulative_pnl"] = settled.groupby(["strategy", "timing"]).pnl.cumsum()
    return settled


def historical_price_strategy_simulation(
    reconstructed: pd.DataFrame,
    markets: pd.DataFrame,
    stake: float = 1.0,
) -> pd.DataFrame:
    """Combine reconstructed D-1 forecasts with sampled historical trade prices."""
    columns = [
        "airport",
        "target_date",
        "timing",
        "strategy",
        "bucket_label",
        "model_bucket_c",
        "buy_price",
        "won",
        "pnl",
        "price_basis",
        "cumulative_pnl",
    ]
    if reconstructed.empty or markets.empty:
        return pd.DataFrame(columns=columns)
    history = markets.copy()
    history["captured_at"] = pd.to_datetime(history.captured_at, utc=True)
    history["target_date"] = pd.to_datetime(history.target_date).dt.date
    if "price_kind" in history:
        history = history[history.price_kind == "historical trade-price sample"].copy()
    else:
        history = history[history.best_ask.isna()].copy()
    history = history[history.yes_won.notna()].copy()
    if history.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for forecast in reconstructed.itertuples():
        target_markets = history[
            (history.airport == forecast.airport) & (history.target_date == forecast.target_date)
        ]
        if target_markets.empty:
            continue
        # The market-history backfill stores D-1 samples before D0 samples.
        sampled_at = target_markets.captured_at.min()
        sample = target_markets[target_markets.captured_at == sampled_at]
        bucket = math.floor(float(forecast.forecast_c) + 0.5)
        match = sample[
            sample.apply(
                lambda row, selected_bucket=bucket: (
                    (pd.isna(row.bucket_low_c) or selected_bucket >= float(row.bucket_low_c))
                    and (pd.isna(row.bucket_high_c) or selected_bucket <= float(row.bucket_high_c))
                ),
                axis=1,
            )
        ]
        if match.empty:
            continue
        market = match.iloc[0]
        price = float(market.yes_price)
        if not 0 < price < 1:
            continue
        won = bool(market.yes_won)
        rows.append(
            {
                "airport": forecast.airport,
                "target_date": forecast.target_date,
                "timing": "D-1 historical price",
                "strategy": forecast.stage,
                "bucket_label": market.bucket_label,
                "model_bucket_c": bucket,
                "buy_price": price,
                "won": won,
                "pnl": stake / price - stake if won else -stake,
                "price_basis": "historical trade-price sample",
            }
        )
    result = pd.DataFrame(rows, columns=columns[:-1])
    if not result.empty:
        result = result.sort_values("target_date")
        result["cumulative_pnl"] = result.groupby("strategy").pnl.cumsum()
    return result


def resolved_market_outcomes(markets: pd.DataFrame) -> pd.DataFrame:
    """Return outcomes only for events with exactly one confirmed winner."""
    if markets.empty:
        return pd.DataFrame(columns=["market_id", "yes_won"])
    outcomes = markets.copy()
    outcomes["captured_at"] = pd.to_datetime(outcomes.captured_at, utc=True)
    outcomes = outcomes.sort_values("captured_at").drop_duplicates("market_id", keep="last")
    if "event_slug" in outcomes:
        outcomes["event_key"] = outcomes.event_slug.astype(str)
    else:
        outcomes["event_key"] = "single-event"
    resolved_groups = []
    for _, event in outcomes.groupby("event_key"):
        event_closed = event.closed.fillna(False).astype(bool).all()
        winner_count = int(event.yes_won.fillna(False).astype(bool).sum())
        if event_closed and winner_count == 1:
            resolved_groups.append(event[["market_id", "yes_won"]])
    return (
        pd.concat(resolved_groups, ignore_index=True)
        if resolved_groups
        else pd.DataFrame(columns=["market_id", "yes_won"])
    )


def settled_signal_performance(
    signals: pd.DataFrame,
    markets: pd.DataFrame,
    stake: float = 1.0,
) -> pd.DataFrame:
    """Settle the first recorded legacy edge or research-disagreement entry."""
    columns = [
        "airport",
        "target_date",
        "market_id",
        "bucket_label",
        "captured_at",
        "timing",
        "model_probability",
        "buy_price",
        "edge",
        "won",
        "pnl",
        "cumulative_pnl",
    ]
    if signals.empty or markets.empty:
        return pd.DataFrame(columns=columns)
    candidates = signals[
        signals.signal.isin(
            [
                "Possible edge",
                "Uncalibrated disagreement",
                "Market-model conflict",
            ]
        )
    ].copy()
    candidates["buy_price"] = pd.to_numeric(candidates.buy_price, errors="coerce")
    candidates = candidates[(candidates.buy_price > 0) & (candidates.buy_price < 1)]
    if candidates.empty:
        return pd.DataFrame(columns=columns)
    candidates["captured_at"] = pd.to_datetime(candidates.captured_at, utc=True)
    entries = candidates.sort_values("captured_at").drop_duplicates("market_id", keep="first")

    outcomes = resolved_market_outcomes(markets)
    if outcomes.empty:
        return pd.DataFrame(columns=columns)

    settled = entries.merge(outcomes, on="market_id", how="inner")
    if settled.empty:
        return pd.DataFrame(columns=columns)
    settled["won"] = settled.yes_won.astype(bool)
    settled["pnl"] = settled.apply(
        lambda row: stake / row.buy_price - stake if row.won else -stake,
        axis=1,
    )
    settled = settled.sort_values("captured_at")
    settled["cumulative_pnl"] = settled.pnl.cumsum()
    return settled[columns].reset_index(drop=True)


def settled_shadow_performance(
    evaluations: pd.DataFrame,
    markets: pd.DataFrame,
) -> pd.DataFrame:
    """Settle the first depth- and fee-aware SHADOW BET for each market bucket."""
    columns = [
        "airport",
        "target_date",
        "market_id",
        "bucket_label",
        "captured_at",
        "timing",
        "fair_probability",
        "average_fill_price",
        "fee_per_share",
        "all_in_price",
        "slippage",
        "net_edge",
        "stake_usdc",
        "shares",
        "total_cost_usdc",
        "won",
        "pnl",
        "roi",
        "cumulative_pnl",
    ]
    if evaluations.empty or markets.empty:
        return pd.DataFrame(columns=columns)
    candidates = evaluations[evaluations.status == "SHADOW BET"].copy()
    for column in ("shares", "total_cost_usdc", "stake_usdc"):
        candidates[column] = pd.to_numeric(candidates[column], errors="coerce")
    candidates = candidates[
        (candidates.shares > 0)
        & (candidates.total_cost_usdc > 0)
        & candidates.fully_fillable.fillna(False).astype(bool)
    ]
    if candidates.empty:
        return pd.DataFrame(columns=columns)
    candidates["captured_at"] = pd.to_datetime(candidates.captured_at, utc=True)
    entries = candidates.sort_values("captured_at").drop_duplicates(
        "market_id",
        keep="first",
    )
    outcomes = resolved_market_outcomes(markets)
    if outcomes.empty:
        return pd.DataFrame(columns=columns)
    settled = entries.merge(outcomes, on="market_id", how="inner")
    if settled.empty:
        return pd.DataFrame(columns=columns)
    settled["won"] = settled.yes_won.astype(bool)
    settled["pnl"] = settled.apply(
        lambda row: (
            float(row.shares) - float(row.total_cost_usdc)
            if row.won
            else -float(row.total_cost_usdc)
        ),
        axis=1,
    )
    settled["roi"] = settled.pnl / settled.total_cost_usdc
    settled = settled.sort_values("captured_at")
    settled["cumulative_pnl"] = settled.pnl.cumsum()
    return settled[columns].reset_index(drop=True)


def settled_probability_comparison(
    signals: pd.DataFrame,
    markets: pd.DataFrame,
) -> pd.DataFrame:
    """Compare journaled model and market probabilities after official resolution."""
    if signals.empty or markets.empty:
        return pd.DataFrame()
    snapshots = signals.copy()
    snapshots["captured_at"] = pd.to_datetime(snapshots.captured_at, utc=True)
    snapshots = snapshots.sort_values("captured_at").drop_duplicates(
        ["market_id", "timing"], keep="first"
    )
    outcomes = resolved_market_outcomes(markets)
    if outcomes.empty:
        return pd.DataFrame()
    result = snapshots.merge(outcomes, on="market_id", how="inner")
    if result.empty:
        return result
    result["outcome"] = result.yes_won.astype(bool).astype(float)
    result["model_brier"] = (result.model_probability - result.outcome) ** 2
    result["market_brier"] = (result.market_probability - result.outcome) ** 2
    result["model_market_gap"] = (result.model_probability - result.market_probability).abs()
    return result


def _expected_calibration_error(frame: pd.DataFrame, bins: int = 5) -> float | None:
    if frame.empty:
        return None
    working = frame.copy()
    working["probability_bin"] = pd.cut(
        working.model_probability,
        bins=[index / bins for index in range(bins + 1)],
        include_lowest=True,
    )
    total = len(working)
    error = 0.0
    for _, group in working.groupby("probability_bin", observed=True):
        error += (
            len(group)
            / total
            * abs(float(group.model_probability.mean()) - float(group.outcome.mean()))
        )
    return error


def trading_airport_scorecards(
    performance: pd.DataFrame,
    probability_records: pd.DataFrame,
) -> pd.DataFrame:
    """Build gated airport trading statistics from independent target-day results."""
    airports = set()
    if not performance.empty:
        airports.update(performance.airport.astype(str).unique())
    if not probability_records.empty:
        airports.update(probability_records.airport.astype(str).unique())
    rows = []
    for airport in sorted(airports):
        trades = performance[performance.airport == airport].copy()
        probabilities = probability_records[probability_records.airport == airport].copy()
        if not trades.empty:
            trades["target_date"] = pd.to_datetime(trades.target_date).dt.date
            daily = trades.groupby("target_date", as_index=False).agg(
                pnl=("pnl", "sum"),
                entries=("market_id", "count"),
            )
            daily = daily.sort_values("target_date")
            cumulative = daily.pnl.cumsum()
            drawdown = cumulative - cumulative.cummax().clip(lower=0)
            max_drawdown = abs(float(drawdown.min()))
            resolved_days = int(daily.target_date.nunique())
            entries = len(trades)
            total_pnl = float(trades.pnl.sum())
            roi = total_pnl / entries
            hit_rate = float(trades.won.mean())
            average_edge = float(trades.edge.mean())
            daily_mean = float(daily.pnl.mean())
            daily_std = float(daily.pnl.std(ddof=1)) if resolved_days >= 2 else 0.0
            risk_ratio = daily_mean / daily_std if daily_std > 0 else 0.0
            sharpe = risk_ratio if resolved_days >= 30 and daily_std > 0 else None
        else:
            resolved_days = 0
            entries = 0
            total_pnl = 0.0
            roi = None
            hit_rate = None
            average_edge = None
            max_drawdown = 0.0
            risk_ratio = 0.0
            sharpe = None

        probability_samples = len(probabilities)
        model_brier = float(probabilities.model_brier.mean()) if probability_samples else None
        market_brier = float(probabilities.market_brier.mean()) if probability_samples else None
        brier_advantage = (
            market_brier - model_brier
            if model_brier is not None and market_brier is not None
            else None
        )
        average_market_gap = (
            float(probabilities.model_market_gap.mean()) if probability_samples else None
        )
        calibration_error = (
            _expected_calibration_error(probabilities)
            if probability_samples >= 100 and resolved_days >= 30
            else None
        )

        if resolved_days < 10:
            confidence = "Not enough data"
            trade_score = None
        else:
            confidence = (
                "Provisional"
                if resolved_days < 30
                else "Developing"
                if resolved_days < 100
                else "More robust"
            )
            roi_score = 50 + 50 * math.tanh(float(roi or 0.0) * 2)
            risk_score = 50 + 50 * math.tanh(risk_ratio)
            brier_score = 50 + 50 * math.tanh(float(brier_advantage or 0.0) * 10)
            raw_score = 0.50 * roi_score + 0.25 * risk_score + 0.25 * brier_score
            drawdown_penalty = min(15.0, max_drawdown / max(1, entries) * 15)
            reliability = min(1.0, resolved_days / 30)
            trade_score = 50 + reliability * (raw_score - drawdown_penalty - 50)
            trade_score = max(0.0, min(100.0, trade_score))

        rows.append(
            {
                "airport": airport,
                "resolved_days": resolved_days,
                "entries": entries,
                "hit_rate": hit_rate,
                "pnl": total_pnl,
                "roi": roi,
                "max_drawdown": max_drawdown,
                "sharpe": sharpe,
                "average_edge": average_edge,
                "probability_samples": probability_samples,
                "model_brier": model_brier,
                "market_brier": market_brier,
                "brier_advantage": brier_advantage,
                "average_market_gap": average_market_gap,
                "calibration_error": calibration_error,
                "trade_score": trade_score,
                "confidence": confidence,
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "airport",
            "resolved_days",
            "entries",
            "hit_rate",
            "pnl",
            "roi",
            "max_drawdown",
            "sharpe",
            "average_edge",
            "probability_samples",
            "model_brier",
            "market_brier",
            "brier_advantage",
            "average_market_gap",
            "calibration_error",
            "trade_score",
            "confidence",
        ],
    )


def _direction_in_sectors(
    direction: float,
    sectors: list[list[float]] | tuple[tuple[float, float], ...] | None,
) -> bool:
    """Return whether a meteorological FROM direction falls inside any sector."""
    normalized = direction % 360
    for start, end in sectors or []:
        start = float(start) % 360
        end = float(end) % 360
        if start <= end and start <= normalized <= end:
            return True
        if start > end and (normalized >= start or normalized <= end):
            return True
    return False


def _wind_heat_effect(
    *,
    speed_kph: float | None,
    direction_deg: float | None,
    warm_sectors: list[list[float]] | tuple[tuple[float, float], ...] | None,
    cool_sectors: list[list[float]] | tuple[tuple[float, float], ...] | None,
    source: str,
) -> tuple[int, float, str | None]:
    """Conservative wind contribution pending airport-specific calibration."""
    if speed_kph is None:
        return 0, 0.0, None
    speed = max(0.0, float(speed_kph))
    direction = None if direction_deg is None else float(direction_deg) % 360
    source_label = "METAR" if source == "METAR" else "model"
    wind_label = f"{speed:.0f} km/h"
    if direction is not None:
        wind_label += f" from {direction:.0f}°"

    # With almost calm wind, the direction is not meteorologically robust. Light
    # winds do, however, reduce ventilation and can support local solar heating.
    if speed < 6:
        return 3, 0.08, f"Light {source_label} wind ({wind_label}) limits ventilation"

    if direction is not None and _direction_in_sectors(direction, warm_sectors):
        points = 4 if speed < 15 else 7 if speed < 30 else 5
        return (
            points,
            min(0.25, points / 30),
            f"{source_label} wind ({wind_label}) is in this airport's warm sector",
        )

    if direction is not None and _direction_in_sectors(direction, cool_sectors):
        points = -4 if speed < 15 else -8 if speed < 30 else -12
        return (
            points,
            max(-0.40, points / 30),
            f"{source_label} wind ({wind_label}) is in this airport's cooling sector",
        )

    if speed >= 35:
        return (
            -4,
            -0.12,
            f"Strong {source_label} wind ({wind_label}) makes local spike heating less reliable",
        )
    return 0, 0.0, f"{source_label} wind is neutral ({wind_label})"


def wind_heat_adjustment(
    *,
    speed_kph: float | None,
    direction_deg: float | None,
    warm_sectors=None,
    cool_sectors=None,
    source: str = "model",
) -> float:
    """Expose the numerical part of the airport wind-sector correction."""
    return _wind_heat_effect(
        speed_kph=speed_kph,
        direction_deg=direction_deg,
        warm_sectors=warm_sectors,
        cool_sectors=cool_sectors,
        source=source,
    )[1]


def heat_spike_assessment(
    *,
    forecast_mean: float,
    recent_baseline: float | None,
    run_trend: float | None,
    model_spread: float,
    observed_temp: float | None,
    observed_dewpoint: float | None,
    expected_temp_now: float | None,
    heating_rate: float | None,
    cloud_cover: float | None,
    wind_speed_kph: float | None = None,
    wind_direction_deg: float | None = None,
    warm_wind_sectors: list[list[float]] | tuple[tuple[float, float], ...] | None = None,
    cool_wind_sectors: list[list[float]] | tuple[tuple[float, float], ...] | None = None,
    wind_source: str = "model",
    guidance_score_points: int = 0,
    guidance_adjustment_c: float = 0.0,
    guidance_signals: list[str] | tuple[str, ...] | None = None,
) -> HeatSpikeAssessment:
    score = 35
    signals: list[str] = []

    if recent_baseline is not None:
        anomaly = forecast_mean - recent_baseline
        if anomaly >= 4:
            score += 20
            signals.append(f"Forecast is {anomaly:.1f} °C above the recent baseline")
        elif anomaly >= 2:
            score += 10
            signals.append(f"Moderate heat anomaly of {anomaly:.1f} °C")

    if run_trend is not None:
        if run_trend >= 1:
            score += 15
            signals.append(f"Model runs moved {run_trend:+.1f} °C hotter")
        elif run_trend >= 0.3:
            score += 7
            signals.append(f"Model runs trend slightly hotter ({run_trend:+.1f} °C)")
        elif run_trend <= -0.5:
            score -= 8
            signals.append(f"Latest runs cooled by {run_trend:.1f} °C")

    if model_spread <= 1:
        score += 8
        signals.append("Strong model agreement")
    elif model_spread >= 2.5:
        score -= 10
        signals.append("Large model disagreement")

    if observed_temp is not None and observed_dewpoint is not None:
        depression = observed_temp - observed_dewpoint
        if depression >= 15:
            score += 12
            signals.append(f"Very dry mixed air (T−Td {depression:.0f} °C)")
        elif depression >= 10:
            score += 6
            signals.append(f"Dry air supports heating (T−Td {depression:.0f} °C)")

    observed_anomaly = None
    if observed_temp is not None and expected_temp_now is not None:
        observed_anomaly = observed_temp - expected_temp_now
        if observed_anomaly >= 1:
            score += 15
            signals.append(f"METAR is {observed_anomaly:+.1f} °C above the model path")
        elif observed_anomaly <= -1:
            score -= 15
            signals.append(f"METAR is {observed_anomaly:.1f} °C below the model path")

    if heating_rate is not None:
        if heating_rate >= 1.5:
            score += 12
            signals.append(f"Rapid heating of {heating_rate:.1f} °C/hour")
        elif heating_rate >= 0.6:
            score += 6
            signals.append(f"Heating continues at {heating_rate:.1f} °C/hour")
        elif heating_rate < 0:
            score -= 5
            signals.append("Temperature is no longer rising")

    if cloud_cover is not None:
        if cloud_cover <= 20:
            score += 8
            signals.append("Mostly clear at the current forecast hour")
        elif cloud_cover >= 70:
            score -= 12
            signals.append("Cloud cover suppresses heating")

    wind_points, wind_adjustment, wind_signal = _wind_heat_effect(
        speed_kph=wind_speed_kph,
        direction_deg=wind_direction_deg,
        warm_sectors=warm_wind_sectors,
        cool_sectors=cool_wind_sectors,
        source=wind_source,
    )
    score += wind_points
    if wind_signal is not None:
        signals.append(wind_signal)

    # Airport TAF guidance is intentionally capped and remains visibly separate
    # from the numerical-model consensus. It can confirm or flag the heat setup,
    # but it is not counted as another independent model vote.
    score += max(-12, min(6, int(guidance_score_points)))
    signals.extend(guidance_signals or [])

    score = int(max(0, min(100, score)))
    adjustment = 0.0
    if observed_anomaly is not None:
        adjustment += 0.45 * observed_anomaly
    if run_trend is not None:
        adjustment += 0.2 * run_trend
    adjustment += wind_adjustment
    adjustment += max(-0.35, min(0.20, float(guidance_adjustment_c)))
    adjustment = max(-1.5, min(1.5, adjustment))

    if observed_temp is None:
        status = "Elevated" if score >= 65 else "Normal"
    elif score >= 70 and (observed_anomaly or 0) >= 0:
        status = "Confirmed"
    elif score >= 50:
        status = "On track"
    elif score >= 30:
        status = "At risk"
    else:
        status = "Failed"
    return HeatSpikeAssessment(score, status, adjustment, signals or ["No strong signal"])


def score_frame(forecasts: pd.DataFrame, actuals: pd.DataFrame) -> pd.DataFrame:
    if forecasts.empty or actuals.empty:
        return pd.DataFrame()
    latest = forecasts.sort_values("run_at").drop_duplicates(
        ["airport", "model", "target_date"], keep="last"
    )
    merged = latest.merge(actuals, on=["airport", "target_date"], suffixes=("_forecast", "_actual"))
    merged["error"] = merged["max_temp_c_forecast"] - merged["max_temp_c_actual"]
    merged["abs_error"] = merged["error"].abs()
    return merged


def model_metrics(scored: pd.DataFrame) -> pd.DataFrame:
    if scored.empty:
        return pd.DataFrame(columns=["model", "n", "bias", "mae", "rmse", "hit_rate"])
    rows = []
    for model, frame in scored.groupby("model"):
        rows.append(
            {
                "model": model,
                "n": len(frame),
                "bias": frame.error.mean(),
                "mae": frame.abs_error.mean(),
                "rmse": math.sqrt((frame.error**2).mean()),
                "hit_rate": (frame.abs_error < 0.5).mean(),
            }
        )
    return pd.DataFrame(rows).sort_values("mae")


def model_weight_map(
    scored: pd.DataFrame,
    lookback_days: int = 90,
    full_reliability_days: int = 30,
) -> dict[str, float]:
    """Create conservative recent-error weights, shrunk toward equal weighting."""
    if scored.empty:
        return {}
    recent = scored.copy()
    recent["target_date"] = pd.to_datetime(recent.target_date).dt.date
    cutoff = max(recent.target_date) - timedelta(days=lookback_days - 1)
    recent = recent[recent.target_date >= cutoff]
    recent["residual_error"] = recent.error - recent.groupby("model").error.transform("mean")
    recent["residual_abs_error"] = recent.residual_error.abs()
    grouped = recent.groupby("model", as_index=False).agg(
        n=("target_date", "nunique"),
        mae=("residual_abs_error", "mean"),
    )
    if grouped.empty:
        return {}
    baseline_mae = max(0.25, float(grouped.mae.median()))
    raw: dict[str, float] = {}
    for row in grouped.itertuples():
        reliability = min(1.0, float(row.n) / full_reliability_days)
        relative_precision = ((baseline_mae + 0.35) / (float(row.mae) + 0.35)) ** 2
        raw[str(row.model)] = max(
            0.4,
            min(2.5, 1.0 + reliability * (relative_precision - 1.0)),
        )
    total = sum(raw.values())
    return {model: value / total for model, value in raw.items()}


def walk_forward_ensemble(
    forecasts: pd.DataFrame,
    actuals: pd.DataFrame,
    min_history_days: int = 20,
) -> pd.DataFrame:
    """Validate dynamic weights using only information available before each target day."""
    if forecasts.empty or actuals.empty:
        return pd.DataFrame()
    d1 = forecasts[forecasts.horizon == "D-1"].copy()
    scored = score_frame(d1, actuals)
    if scored.empty:
        return pd.DataFrame()
    scored["target_date"] = pd.to_datetime(scored.target_date).dt.date
    rows = []
    for airport, airport_frame in scored.groupby("airport"):
        daily: dict[date, list] = {}
        for item in airport_frame.sort_values("target_date").itertuples(index=False):
            daily.setdefault(item.target_date, []).append(item)
        recent_history: list[tuple[date, str, float]] = []
        seen_dates: set[date] = set()
        for target, today in daily.items():
            history_cutoff = target - timedelta(days=90)
            recent_history = [item for item in recent_history if item[0] >= history_cutoff]
            if len(seen_dates) >= min_history_days:
                biases, weights = _rolling_biases_and_weights(recent_history)
                fallback_weight = min(weights.values()) * 0.5 if weights else 1.0
                corrected_values = [
                    float(row.max_temp_c_forecast) - float(biases.get(str(row.model), 0.0))
                    for row in today
                ]
                current_weights = [
                    float(weights.get(str(row.model), fallback_weight)) for row in today
                ]
                if corrected_values:
                    prediction = _weighted_average(
                        corrected_values,
                        current_weights,
                    )
                    actual = float(today[0].max_temp_c_actual)
                    rows.append(
                        {
                            "airport": airport,
                            "model": "Weighted ensemble",
                            "target_date": target,
                            "max_temp_c_forecast": prediction,
                            "max_temp_c_actual": actual,
                            "error": prediction - actual,
                            "abs_error": abs(prediction - actual),
                        }
                    )
            recent_history.extend((target, str(row.model), float(row.error)) for row in today)
            seen_dates.add(target)
    return pd.DataFrame(rows)


def forecast_scorecards(
    forecasts: pd.DataFrame,
    actuals: pd.DataFrame,
    windows: tuple[int, ...] = (30, 90, 365),
) -> pd.DataFrame:
    """Build per-airport and per-model accuracy scorecards for fixed D-1 forecasts."""
    if forecasts.empty or actuals.empty:
        return pd.DataFrame()
    d1 = forecasts[forecasts.horizon == "D-1"].copy()
    scored = score_frame(d1, actuals)
    ensemble = walk_forward_ensemble(forecasts, actuals)
    metric_columns = [
        "airport",
        "model",
        "target_date",
        "max_temp_c_forecast",
        "max_temp_c_actual",
        "error",
        "abs_error",
    ]
    if not ensemble.empty:
        scored = pd.concat(
            [scored[metric_columns], ensemble[metric_columns]],
            ignore_index=True,
        )
    if scored.empty:
        return pd.DataFrame()
    scored["target_date"] = pd.to_datetime(scored.target_date).dt.date
    scored["bucket_hit"] = scored.apply(
        lambda row: (
            math.floor(row.max_temp_c_forecast + 0.5) == math.floor(row.max_temp_c_actual + 0.5)
        ),
        axis=1,
    )
    scored["within_1c"] = scored.abs_error <= 1.0
    latest = max(scored.target_date)
    rows = []
    for window in windows:
        period = scored[scored.target_date >= latest - timedelta(days=window - 1)]
        for (airport, model), frame in period.groupby(["airport", "model"]):
            n = int(frame.target_date.nunique())
            bias = float(frame.error.mean())
            mae = float(frame.abs_error.mean())
            rmse = math.sqrt(float((frame.error**2).mean()))
            exact_hit = float(frame.bucket_hit.mean())
            within_1c = float(frame.within_1c.mean())
            mae_score = 100 / (1 + (mae / 1.0) ** 2)
            rmse_score = 100 / (1 + (rmse / 1.25) ** 2)
            raw_score = (
                0.35 * mae_score
                + 0.20 * rmse_score
                + 0.25 * exact_hit * 100
                + 0.20 * within_1c * 100
            )
            reliability = min(1.0, n / 30)
            forecast_score = 50 + reliability * (raw_score - 50)
            rows.append(
                {
                    "airport": str(airport),
                    "model": str(model),
                    "window_days": window,
                    "n": n,
                    "bias": bias,
                    "mae": mae,
                    "rmse": rmse,
                    "exact_hit": exact_hit,
                    "within_1c": within_1c,
                    "forecast_score": max(0.0, min(100.0, forecast_score)),
                    "data_quality": ("Strong" if n >= 90 else "Moderate" if n >= 30 else "Limited"),
                }
            )
    return pd.DataFrame(rows)


def flat_bet_simulation(
    scored: pd.DataFrame, stake: float = 1.0, decimal_odds: float = 2.0
) -> pd.DataFrame:
    """Synthetic $1 strategy: bet rounded corrected consensus, fixed odds unless market history exists."""
    if scored.empty:
        return pd.DataFrame()
    working = scored.sort_values(["airport", "model", "target_date"]).copy()
    if "error" not in working:
        working["error"] = working.max_temp_c_forecast - working.max_temp_c_actual
    working["past_bias"] = working.groupby(["airport", "model"])["error"].transform(
        lambda values: values.expanding().mean().shift(1)
    )
    working["corrected"] = working.max_temp_c_forecast - working.past_bias.fillna(0)
    daily = working.groupby(["airport", "target_date"], as_index=False).agg(
        predicted=("corrected", "median"), actual=("max_temp_c_actual", "first")
    )
    daily["bucket"] = (daily.predicted + 0.5).apply(math.floor).astype(int)
    daily["actual_bucket"] = (daily.actual + 0.5).apply(math.floor).astype(int)
    daily["won"] = daily.actual_bucket.eq(daily.bucket)
    daily["pnl"] = daily.won.map({True: stake * (decimal_odds - 1), False: -stake})
    daily["cumulative_pnl"] = daily.pnl.cumsum()
    return daily
