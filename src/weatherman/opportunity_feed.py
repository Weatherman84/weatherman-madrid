"""Causal live-feed producer for the separate six-airport Shadow scanner.

This module is deliberately independent of the Madrid database and Production
forecast service.  It reads public weather/market APIs and emits one compact,
outcome-free feed using the frozen common-model benchmark definition.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from importlib.resources import files
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from .opportunity_scanner import (
    FORBIDDEN_SELECTOR_FIELDS,
    checkpoint_schedule,
    expected_peak_time,
    load_airport_config,
)
from .providers import (
    polymarket_order_books,
    polymarket_prices,
    previous_run_hourly_d1,
    recent_metars,
)


FEED_SCHEMA = "opportunity_scanner_feed_v1"
CALIBRATION_SCHEMA = "opportunity_scanner_calibration_seed_v1"
PEAK_SEED_SCHEMA = "opportunity_scanner_peak_seed_v1"
FIXED_FORECAST_METHOD = "common_models_fixed_24h_lead_bias_corrected_live_anchor_v1"


def _resource(name: str) -> dict[str, Any]:
    with files("weatherman").joinpath("data", name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_calibration_seed() -> dict[str, Any]:
    payload = _resource("opportunity_scanner_calibration_seed_v1.json")
    airports = set(load_airport_config()["airports"])
    safe = (
        payload.get("schema_version") == CALIBRATION_SCHEMA
        and payload.get("research_only") is True
        and payload.get("automatic_promotion") is False
        and payload.get("automatic_execution") is False
        and set(payload.get("airports", {})) == airports
    )
    if not safe:
        raise RuntimeError("Unsafe or incomplete scanner calibration seed")
    return payload


def load_peak_seed() -> dict[str, Any]:
    payload = _resource("opportunity_scanner_peak_seed_v1.json")
    safe = (
        payload.get("schema_version") == PEAK_SEED_SCHEMA
        and payload.get("research_only") is True
        and payload.get("automatic_backfill") is False
        and set(payload.get("airports", {})) == set(load_airport_config()["airports"])
    )
    if not safe:
        raise RuntimeError("Unsafe or incomplete scanner peak-history seed")
    return payload


def _utc(value: datetime | str) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _weighted_mean(values: Mapping[str, float], weights: Mapping[str, float]) -> float:
    usable = {key: max(0.0, float(weights.get(key, 0.0))) for key in values}
    if sum(usable.values()) <= 0:
        usable = {key: 1.0 for key in values}
    return sum(values[key] * usable[key] for key in values) / sum(usable.values())


def _weighted_std(values: Mapping[str, float], weights: Mapping[str, float]) -> float:
    mean = _weighted_mean(values, weights)
    usable = {key: max(0.0, float(weights.get(key, 0.0))) for key in values}
    if sum(usable.values()) <= 0:
        usable = {key: 1.0 for key in values}
    variance = sum(usable[key] * (values[key] - mean) ** 2 for key in values) / sum(usable.values())
    return math.sqrt(max(0.0, variance))


def normal_bucket_probabilities(center: float, sigma: float) -> dict[int, float]:
    """Return normalized integer-C bucket probabilities."""
    spread = max(0.35, float(sigma))

    def cdf(value: float) -> float:
        return 0.5 * (1 + math.erf((value - center) / (spread * math.sqrt(2))))

    probabilities = {
        bucket: cdf(bucket + 0.5) - cdf(bucket - 0.5)
        for bucket in range(math.floor(center - 5 * spread), math.ceil(center + 5 * spread) + 1)
    }
    total = sum(probabilities.values())
    return {bucket: probability / total for bucket, probability in probabilities.items()}


def _nearest(rows: Iterable[Mapping[str, Any]], at: datetime) -> dict[str, Any] | None:
    usable = [dict(row) for row in rows if row.get("valid_at") is not None]
    return min(
        usable, key=lambda row: abs((_utc(row["valid_at"]) - at).total_seconds()), default=None
    )


def _local_day(
    rows: Iterable[Mapping[str, Any]], target: date, zone: ZoneInfo
) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if _utc(row["valid_at"]).astimezone(zone).date() == target]


def _observations_for_day(
    observations: Iterable[Mapping[str, Any]],
    target: date,
    zone: ZoneInfo,
    as_of: datetime,
) -> list[dict[str, Any]]:
    return sorted(
        [
            dict(row)
            for row in observations
            if row.get("observed_at") is not None
            and _utc(row["observed_at"]) <= as_of
            and _utc(row["observed_at"]).astimezone(zone).date() == target
        ],
        key=lambda row: _utc(row["observed_at"]),
    )


def _rate(rows: list[Mapping[str, Any]], minutes: int) -> float | None:
    if len(rows) < 2:
        return None
    latest_at = _utc(rows[-1]["observed_at"])
    candidates = [
        row
        for row in rows[:-1]
        if _utc(row["observed_at"]) <= latest_at - timedelta(minutes=minutes)
    ]
    if not candidates:
        return None
    earlier = max(candidates, key=lambda row: _utc(row["observed_at"]))
    elapsed = (latest_at - _utc(earlier["observed_at"])).total_seconds() / 3600
    return (float(rows[-1]["temp_c"]) - float(earlier["temp_c"])) / elapsed if elapsed > 0 else None


def _median(values: Iterable[object]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return float(statistics.median(usable)) if usable else None


def _regimes(
    *,
    observations: list[dict[str, Any]],
    model_now: list[dict[str, Any]],
    center: float,
    spread: float,
    expected_peak: datetime,
    recent_actuals: list[Mapping[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    regimes: set[str] = set()
    heating_30 = _rate(observations, 30)
    heating_60 = _rate(observations, 60)
    heating_120 = _rate(observations, 120)
    latest = observations[-1] if observations else {}
    latest_at = _utc(latest["observed_at"]) if latest else None
    recent = [
        row
        for row in observations
        if latest_at is not None and _utc(row["observed_at"]) >= latest_at - timedelta(hours=2)
    ]
    dewpoint_rate = None
    if (
        len(recent) >= 2
        and recent[0].get("dewpoint_c") is not None
        and recent[-1].get("dewpoint_c") is not None
    ):
        elapsed = (
            _utc(recent[-1]["observed_at"]) - _utc(recent[0]["observed_at"])
        ).total_seconds() / 3600
        if elapsed > 0:
            dewpoint_rate = (
                float(recent[-1]["dewpoint_c"]) - float(recent[0]["dewpoint_c"])
            ) / elapsed
    observed_cloud = _median(row.get("cloud_cover") for row in recent[-4:])
    model_cloud = _median(row.get("cloud_cover") for row in model_now)
    wind = _median(row.get("wind_kph") for row in recent[-3:])
    raw = " ".join(str(row.get("raw") or "").upper() for row in recent[-4:])
    station_clear = "CAVOK" in raw or (observed_cloud is not None and observed_cloud <= 20)
    if heating_60 is not None and abs(heating_60) <= 0.2:
        regimes.add("Temperature Plateau")
    if model_cloud is not None and model_cloud >= 35 and station_clear:
        regimes.add("Clear Sky Override")
    if (
        observed_cloud is not None
        and observed_cloud >= 65
        and (model_cloud is None or observed_cloud - model_cloud >= 20)
    ):
        regimes.add("Cloud / Radiation Suppression")
    hours_to_peak = (expected_peak - (latest_at or expected_peak)).total_seconds() / 3600
    observed_max = max((float(row["temp_c"]) for row in observations), default=None)
    late_dry = (
        latest_at is not None
        and 1.5 <= hours_to_peak <= 4.5
        and observed_max is not None
        and observed_max >= center - 0.5
        and dewpoint_rate is not None
        and dewpoint_rate <= -0.5
        and (heating_120 is None or heating_120 >= -0.1)
        and station_clear
        and wind is not None
        and wind <= 18
    )
    if late_dry:
        regimes.add("Late Dry Mixing")
    actual_values = sorted(
        [
            (str(row["target_date"])[:10], float(row["max_temp_c"]))
            for row in recent_actuals
            if row.get("max_temp_c") is not None
        ]
    )
    if actual_values:
        latest_actual = actual_values[-1][1]
        previous = [value for _, value in actual_values[:-1]][-14:]
        if center - latest_actual >= 1.5 or (
            len(actual_values) >= 2
            and latest_actual - actual_values[-2][1] >= 1.5
            and center - latest_actual >= 0.75
        ):
            regimes.add("Rapid Heat Ramp")
        if (
            previous
            and latest_actual >= statistics.median(previous) + 3
            and center >= latest_actual - 0.5
        ):
            regimes.add("Persistent Hot")
    if spread <= 1:
        regimes.add("Model Spread Low")
    else:
        regimes.add("Model Split / Spread High")
    if abs((center - math.floor(center)) - 0.5) <= 0.15:
        regimes.add("Bucket Boundary")
    return sorted(regimes), {
        "heating_rate_30m_cph": heating_30,
        "heating_rate_60m_cph": heating_60,
        "heating_rate_120m_cph": heating_120,
        "dewpoint_rate_120m_cph": dewpoint_rate,
        "observed_cloud_cover": observed_cloud,
        "model_cloud_cover": model_cloud,
        "wind_speed_kph": wind,
        "hours_to_expected_peak": hours_to_peak,
    }


def completed_peak_rows(
    observations: Iterable[Mapping[str, Any]],
    *,
    zone_name: str,
    before: date,
) -> list[dict[str, Any]]:
    """Extract first daily peak times from completed METAR days only."""
    zone = ZoneInfo(zone_name)
    grouped: dict[date, list[dict[str, Any]]] = {}
    for raw in observations:
        if raw.get("observed_at") is None or raw.get("temp_c") is None:
            continue
        row = dict(raw)
        local = _utc(row["observed_at"]).astimezone(zone)
        if local.date() < before:
            grouped.setdefault(local.date(), []).append(row)
    output = []
    for target, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: _utc(row["observed_at"]))
        span = (_utc(rows[-1]["observed_at"]) - _utc(rows[0]["observed_at"])).total_seconds() / 3600
        if len(rows) < 8 or span < 6:
            continue
        maximum = max(float(row["temp_c"]) for row in rows)
        first = next(row for row in rows if float(row["temp_c"]) == maximum)
        local = _utc(first["observed_at"]).astimezone(zone)
        output.append(
            {
                "target_date": target.isoformat(),
                "actual_first_peak_minute": float(
                    local.hour * 60 + local.minute + local.second / 60
                ),
                "eligible": True,
            }
        )
    return output


def merge_peak_history(
    seed: Iterable[Mapping[str, Any]],
    existing: Iterable[Mapping[str, Any]],
    completed: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in [*seed, *existing, *completed]:
        merged[str(row["target_date"])[:10]] = dict(row)
    return [merged[key] for key in sorted(merged)][-60:]


def build_forecast_snapshot(
    *,
    airport: str,
    airport_config: Mapping[str, Any],
    target: date,
    as_of: datetime,
    model_rows: Mapping[str, list[Mapping[str, Any]]],
    observations: list[Mapping[str, Any]],
    calibration: Mapping[str, Any],
    peak_history: list[Mapping[str, Any]],
) -> dict[str, Any]:
    zone = ZoneInfo(str(airport_config["timezone"]))
    peak = expected_peak_time(peak_history, target, str(airport_config["timezone"]))
    if not peak.get("available"):
        raise ValueError("insufficient peak history")
    schedule = checkpoint_schedule(peak["expected_peak_local"])
    selected = min(
        schedule, key=lambda row: abs((_utc(row["checkpoint_utc"]) - as_of).total_seconds())
    )
    expected_peak = _utc(peak["expected_peak_local"])
    corrected_maxima: dict[str, float] = {}
    model_now_rows: list[dict[str, Any]] = []
    missing_models: list[str] = []
    for model in load_airport_config()["common_models"]:
        daily = _local_day(model_rows.get(model, []), target, zone)
        if not daily:
            missing_models.append(model)
            continue
        corrected_maxima[model] = max(float(row["temp_c"]) for row in daily) - float(
            calibration["bias_c"][model]
        )
        current = _nearest(daily, as_of)
        if current:
            model_now_rows.append(current)
    if not corrected_maxima:
        raise ValueError("all common models missing")
    weights = calibration["model_weights"]
    pre_live_center = _weighted_mean(corrected_maxima, weights)
    day_observations = _observations_for_day(observations, target, zone, as_of)
    latest = day_observations[-1] if day_observations else None
    expected_now_values = {str(row["model"]): float(row["temp_c"]) for row in model_now_rows}
    live_adjustment = 0.0
    if latest and expected_now_values:
        expected_now = _weighted_mean(expected_now_values, weights)
        live_adjustment = max(-2.0, min(2.0, 0.8 * (float(latest["temp_c"]) - expected_now)))
    center = pre_live_center + live_adjustment
    observed_max = max((float(row["temp_c"]) for row in day_observations), default=None)
    if observed_max is not None:
        center = max(center, observed_max)
    model_spread = _weighted_std(corrected_maxima, weights)
    sigma = max(float(calibration["sigma_c"]), model_spread)
    probabilities = normal_bucket_probabilities(center, sigma)
    ranked = sorted(probabilities.items(), key=lambda item: (-item[1], item[0]))
    regimes, live_features = _regimes(
        observations=day_observations,
        model_now=model_now_rows,
        center=center,
        spread=model_spread,
        expected_peak=expected_peak,
        recent_actuals=calibration.get("recent_actual_maxima_c", []),
    )
    warnings = []
    if missing_models:
        warnings.append("missing_model")
    if latest is None:
        warnings.append("stale_weather_data")
        source_age = None
    else:
        source_age = max(0.0, (as_of - _utc(latest["observed_at"])).total_seconds() / 60)
        if source_age > 90:
            warnings.append("stale_weather_data")
    checkpoint_delta = abs((as_of - _utc(selected["checkpoint_utc"])).total_seconds()) / 60
    if checkpoint_delta > 15:
        warnings.append("checkpoint_missed")
    return {
        "airport": airport,
        "target_date": target.isoformat(),
        "checkpoint": selected["checkpoint"],
        "checkpoint_scheduled_at": selected["checkpoint_utc"],
        "checkpoint_delta_minutes": checkpoint_delta,
        "generated_at": as_of.isoformat(),
        "weather_observed_at": _utc(latest["observed_at"]).isoformat() if latest else None,
        "weather_source_age_minutes": source_age,
        "current_temperature_c": float(latest["temp_c"]) if latest else None,
        "observed_max_c": observed_max,
        "forecast_center_c": center,
        "pre_live_center_c": pre_live_center,
        "live_anchor_adjustment_c": live_adjustment,
        "modal_bucket": int(ranked[0][0]),
        "top1_bucket": int(ranked[0][0]),
        "top1_probability": float(ranked[0][1]),
        "top2_bucket": int(ranked[1][0]),
        "top2_probability": float(ranked[1][1]),
        "top1_top2_gap": float(ranked[0][1] - ranked[1][1]),
        "model_spread_c": model_spread,
        "distribution_sigma_c": sigma,
        "probability_by_bucket": {str(key): value for key, value in probabilities.items()},
        "regimes": regimes,
        "live_features": live_features,
        "models": sorted(corrected_maxima),
        "model_forecast_max_c": {
            key: max(float(row["temp_c"]) for row in _local_day(model_rows[key], target, zone))
            for key in corrected_maxima
        },
        "corrected_model_max_c": corrected_maxima,
        "model_weights": weights,
        "data_quality_warnings": sorted(set(warnings)),
        "expected_peak": peak,
        "forecast_method": FIXED_FORECAST_METHOD,
        "calibration_cutoff_date": calibration["calibration_cutoff_date"],
        "calibration_source_sha256": calibration["source_sha256"],
        "research_only": True,
    }


def _level_price(level: object) -> float | None:
    try:
        if isinstance(level, Mapping):
            return float(level.get("price")) if level.get("price") is not None else None
        if isinstance(level, (list, tuple)) and level:
            return float(level[0])
    except (TypeError, ValueError):
        pass
    return None


def normalize_markets(
    airport: str,
    rows: Iterable[Mapping[str, Any]],
    books: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    for raw in rows:
        low, high = raw.get("bucket_low_c"), raw.get("bucket_high_c")
        if low is None or high is None or float(low) != float(high) or not float(low).is_integer():
            continue
        row = dict(raw)
        token_id = str(row.get("token_id") or "")
        book = dict(books.get(token_id, {}))
        bids = [
            price
            for price in (_level_price(item) for item in book.get("bids", []))
            if price is not None
        ]
        asks = [
            price
            for price in (_level_price(item) for item in book.get("asks", []))
            if price is not None
        ]
        best_bid = max(bids) if bids else row.get("best_bid")
        best_ask = min(asks) if asks else row.get("best_ask")
        snapshot_at = book.get("observed_at") or row.get("captured_at")
        output.append(
            {
                "airport": airport,
                "target_date": str(row["target_date"])[:10],
                "bucket": int(float(low)),
                "event": row.get("event_slug"),
                "market_id": row.get("market_id"),
                "market_slug": row.get("market_slug"),
                "token_id": token_id or None,
                "bucket_label": row.get("bucket_label"),
                "best_bid": float(best_bid) if best_bid is not None else None,
                "best_ask": float(best_ask) if best_ask is not None else None,
                "midpoint": (
                    (float(best_bid) + float(best_ask)) / 2
                    if best_bid is not None and best_ask is not None
                    else None
                ),
                "spread": (
                    float(best_ask) - float(best_bid)
                    if best_bid is not None and best_ask is not None
                    else row.get("spread")
                ),
                "last_price": row.get("yes_price"),
                "series_price": row.get("yes_price"),
                "liquidity": row.get("liquidity"),
                "volume": row.get("volume"),
                "snapshot_at": _iso(snapshot_at),
                "market_status": "closed" if row.get("closed") else "open",
                "quote_source": "polymarket_clob" if book else "polymarket_gamma",
            }
        )
    return output


def validate_live_feed(payload: Mapping[str, Any]) -> None:
    safe = (
        payload.get("schema_version") == FEED_SCHEMA
        and payload.get("research_only") is True
        and payload.get("automatic_promotion") is False
        and payload.get("automatic_execution") is False
        and payload.get("writes_production_database") is False
        and payload.get("contains_outcomes") is False
        and payload.get("automatic_backfill") is False
        and payload.get("common_models") == load_airport_config()["common_models"]
    )
    if not safe:
        raise ValueError("Scanner feed safety validation failed")
    expected_airports = set(load_airport_config()["airports"])
    forecasts = payload.get("forecasts", [])
    if {row.get("airport") for row in forecasts} != expected_airports:
        raise ValueError("Scanner feed must contain exactly one forecast per frozen airport")
    for forecast in forecasts:
        leaked = FORBIDDEN_SELECTOR_FIELDS.intersection(forecast)
        if leaked:
            raise ValueError(f"Outcome leakage fields in scanner feed: {sorted(leaked)}")


def build_live_feed(
    *,
    as_of: datetime | None = None,
    existing_feed: Mapping[str, Any] | None = None,
    forecast_fetcher: Callable[..., list[dict]] = previous_run_hourly_d1,
    metar_fetcher: Callable[..., list[dict]] = recent_metars,
    market_fetcher: Callable[..., list[dict]] = polymarket_prices,
    book_fetcher: Callable[[list[str]], dict[str, dict]] = polymarket_order_books,
) -> dict[str, Any]:
    """Collect the six-airport public inputs and build a safe compact feed."""
    now = _utc(as_of or datetime.now(timezone.utc))
    config = load_airport_config()
    calibration = load_calibration_seed()["airports"]
    peak_seed = load_peak_seed()["airports"]
    previous_history = (existing_feed or {}).get("peak_history", {})
    forecasts: list[dict[str, Any]] = []
    peak_history: dict[str, list[dict[str, Any]]] = {}
    market_raw: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []
    for airport, airport_config in config["airports"].items():
        zone = ZoneInfo(airport_config["timezone"])
        target = now.astimezone(zone).date()
        try:
            observations = metar_fetcher(airport, hours=48)
        except Exception as exc:
            observations = []
            errors.append({"airport": airport, "component": "metar", "error": type(exc).__name__})
        peak_history[airport] = merge_peak_history(
            peak_seed[airport],
            previous_history.get(airport, []),
            completed_peak_rows(observations, zone_name=airport_config["timezone"], before=target),
        )
        model_rows: dict[str, list[dict[str, Any]]] = {}
        for model in config["common_models"]:
            try:
                model_rows[model] = forecast_fetcher(airport_config, model, target)
            except Exception as exc:
                model_rows[model] = []
                errors.append(
                    {
                        "airport": airport,
                        "component": f"forecast:{model}",
                        "error": type(exc).__name__,
                    }
                )
        try:
            forecasts.append(
                build_forecast_snapshot(
                    airport=airport,
                    airport_config=airport_config,
                    target=target,
                    as_of=now,
                    model_rows=model_rows,
                    observations=observations,
                    calibration=calibration[airport],
                    peak_history=peak_history[airport],
                )
            )
        except Exception as exc:
            errors.append(
                {"airport": airport, "component": "forecast_build", "error": type(exc).__name__}
            )
            raise RuntimeError(f"Could not build required scanner forecast for {airport}") from exc
        try:
            market_raw[airport] = market_fetcher(airport_config, target)
        except Exception as exc:
            market_raw[airport] = []
            errors.append({"airport": airport, "component": "market", "error": type(exc).__name__})
    tokens = [
        str(row["token_id"]) for rows in market_raw.values() for row in rows if row.get("token_id")
    ]
    try:
        books = book_fetcher(tokens)
    except Exception as exc:
        books = {}
        errors.append(
            {"airport": "PORTFOLIO", "component": "order_books", "error": type(exc).__name__}
        )
    markets = [
        row
        for airport, rows in market_raw.items()
        for row in normalize_markets(airport, rows, books)
    ]
    payload = {
        "schema_version": FEED_SCHEMA,
        "classification": "CROSS-AIRPORT CAUSAL RESEARCH-ONLY SHADOW FEED",
        "generated_at": now.isoformat(),
        "research_only": True,
        "automatic_promotion": False,
        "automatic_execution": False,
        "writes_production_database": False,
        "contains_outcomes": False,
        "automatic_backfill": False,
        "common_models": config["common_models"],
        "forecast_method": FIXED_FORECAST_METHOD,
        "forecasts": forecasts,
        "peak_history": peak_history,
        "markets": markets,
        "collection_errors": errors,
    }
    validate_live_feed(payload)
    return payload
