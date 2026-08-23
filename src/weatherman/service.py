from __future__ import annotations

import json
import hashlib
import math
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from .analytics import (
    DayStatus,
    detect_market_model_conflict,
    forecast_ladder_history,
    forecast_ladder_oos_reliability,
    market_edges,
)
from .catalog import market_city_index, research_airports, trading_airports
from .db import (
    AirportMarketUniverse,
    BasketSnapshot,
    CollectionCoverage,
    DailyActual,
    Forecast,
    ForecastSnapshot,
    ForecastVariantSnapshot,
    HourlyForecast,
    MarketSnapshot,
    Observation,
    ProviderCall,
    RegimeMemorySnapshot,
    Session,
    ShadowEvaluation,
    SignalSnapshot,
    StrategySnapshot,
    TafReport,
    init_db,
)
from .history import read_archive_live
from .nowcast import build_live_nowcast, complete_metar_actuals
from .post_peak_diagnostics import post_peak_diagnostic
from .regime_memory import enrich_nowcast_with_regime_memory
from .regime_profiles import continuous_regime_profiles
from .providers import (
    discover_polymarket_temperature_events,
    historical_actuals,
    historical_tafs_at,
    meteoblue_forecast,
    open_meteo_forecast,
    open_meteo_hourly,
    polymarket_prices,
    polymarket_order_books,
    polymarket_historical_prices,
    previous_run_d1,
    recent_metars,
    recent_tafs,
)
from .settings import airports, settings
from .shadow import build_shadow_basket, evaluate_shadow_markets


def _upsert(session, model, keys: dict, values: dict) -> None:
    row = session.scalar(select(model).filter_by(**keys))
    if row is None:
        session.add(model(**keys, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)


def _upsert_batch(
    session,
    model,
    rows: Iterable[dict],
    keys: Callable[[dict], dict],
    values: Callable[[dict], dict],
    label: str,
) -> int:
    """Store one source atomically so a bad row cannot poison the whole collection."""
    items = list(rows)
    if not items:
        return 0
    try:
        with session.begin_nested():
            for item in items:
                _upsert(session, model, keys(item), values(item))
            session.flush()
    except Exception as exc:
        print(f"WARN {label} storage rolled back: {type(exc).__name__}: {exc}")
        return 0
    return len(items)


def _actual_source_quality(source: object) -> int:
    """Rank Actual provenance so a later low-quality write cannot win."""
    normalized = str(source or "").strip().casefold()
    if normalized == "stored-metar-station":
        return 300
    if normalized == "metar-provisional":
        return 200
    if "official" in normalized or "manual" in normalized:
        return 400
    if "metar" in normalized or "station" in normalized:
        return 300
    if "archive" in normalized or "reanalysis" in normalized:
        return 100
    return 0


def _store_actual_rows(
    session,
    airport_code: str,
    rows: Iterable[dict],
    *,
    source: str,
    label: str,
) -> int:
    """Store Actuals with monotone provenance and provisional-maximum guards."""
    items = list(rows)
    if not items:
        return 0
    stored = 0
    incoming_quality = _actual_source_quality(source)
    try:
        with session.begin_nested():
            for item in items:
                keys = {
                    "airport": airport_code,
                    "target_date": item["target_date"],
                }
                existing = session.scalar(select(DailyActual).filter_by(**keys))
                incoming_max = float(item["max_temp_c"])
                if existing is None:
                    session.add(
                        DailyActual(
                            **keys,
                            max_temp_c=incoming_max,
                            source=source,
                        )
                    )
                    stored += 1
                    continue
                existing_quality = _actual_source_quality(existing.source)
                if incoming_quality < existing_quality:
                    continue
                if (
                    incoming_quality == existing_quality == 200
                    and incoming_max < float(existing.max_temp_c)
                ):
                    # A rolling METAR window may lose the earlier daily peak. A
                    # provisional value may rise as new reports arrive, never fall.
                    continue
                if (
                    float(existing.max_temp_c) != incoming_max
                    or str(existing.source) != source
                ):
                    existing.max_temp_c = incoming_max
                    existing.source = source
                    stored += 1
            session.flush()
    except Exception as exc:
        print(f"WARN {label} storage rolled back: {type(exc).__name__}: {exc}")
        return 0
    return stored


def _store_taf_rows(
    session,
    rows: Iterable[dict],
    catalog: dict[str, dict],
    label: str,
) -> int:
    """Append immutable TAF revisions while preserving first-seen causality."""
    inserted = 0
    try:
        with session.begin_nested():
            for source in rows:
                item = dict(source)
                code = str(item["airport"])
                content_hash = str(item["content_hash"])
                existing = session.scalar(
                    select(TafReport).where(
                        TafReport.airport == code,
                        TafReport.content_hash == content_hash,
                    )
                )
                if existing is not None:
                    existing.fetched_at = item["fetched_at"]
                    continue
                previous_hash = session.scalar(
                    select(TafReport.content_hash)
                    .where(
                        TafReport.airport == code,
                        TafReport.content_hash != content_hash,
                    )
                    .order_by(TafReport.issue_time.desc(), TafReport.first_seen_at.desc())
                    .limit(1)
                )
                item["revision_of_hash"] = previous_hash
                timezone_name = catalog.get(code, {}).get("timezone", "UTC")
                target_at = item.get("max_temp_at") or item.get("valid_from")
                if target_at is not None:
                    item["target_local_date"] = _as_utc(target_at).astimezone(
                        ZoneInfo(timezone_name)
                    ).date()
                session.add(TafReport(**item))
                inserted += 1
            session.flush()
    except Exception as exc:
        print(f"WARN {label} storage rolled back: {type(exc).__name__}: {exc}")
        return 0
    return inserted


def _store_reanalysis_actuals(
    session,
    airport_code: str,
    rows: Iterable[dict],
) -> int:
    """Store gridded fallback actuals without overwriting station truth."""
    return _store_actual_rows(
        session,
        airport_code,
        rows,
        source="open-meteo-archive",
        label=f"{airport_code}/reanalysis actuals",
    )


def _restore_stored_station_actuals(
    session,
    airport_code: str,
    airport: dict,
    *,
    as_of: datetime,
    lookback_days: int = 7,
) -> int:
    """Rebuild every complete stored METAR day before lower-quality data can win."""
    session.flush()
    frame = read_archive_live(
        Observation,
        session.connection(),
        filters={"airport": airport_code},
        minimums={
            "observed_at": datetime.combine(
                as_of.astimezone(ZoneInfo(airport["timezone"])).date()
                - timedelta(days=max(1, lookback_days)),
                datetime.min.time(),
                ZoneInfo(airport["timezone"]),
            ).astimezone(timezone.utc)
        },
    )
    if frame.empty:
        return 0
    local_target = as_of.astimezone(ZoneInfo(airport["timezone"])).date() + timedelta(days=1)
    rows = complete_metar_actuals(
        frame,
        airport_code=airport_code,
        timezone_name=airport["timezone"],
        target=local_target,
        as_of=as_of,
        critical_window_local=airport.get("critical_window_local"),
    ).to_dict("records")
    return _store_actual_rows(
        session,
        airport_code,
        rows,
        source="stored-metar-station",
        label=f"{airport_code}/stored station actuals",
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(
        timezone.utc
    )


def _source_refresh_due(
    session,
    *,
    airport_code: str,
    source: str,
    target: date,
    as_of: datetime,
    maximum_age_minutes: int,
) -> bool:
    """Return whether a provider needs another current-data poll."""
    latest = session.scalar(
        select(func.max(func.coalesce(Forecast.fetched_at, Forecast.run_at))).where(
            Forecast.airport == airport_code,
            Forecast.source == source,
            Forecast.target_date == target,
        )
    )
    if latest is None:
        return True
    age = _as_utc(as_of) - _as_utc(latest)
    return age >= timedelta(minutes=max(1, maximum_age_minutes))


def _checkpoint_schedule_for_airport(
    airport: dict,
    *,
    local_date: date,
) -> tuple[tuple[str, datetime, int, int], ...]:
    """Return configured local decision checkpoints for one calendar day."""
    zone = ZoneInfo(airport["timezone"])
    configured = airport.get("decision_checkpoints_local") or []
    rows: list[tuple[str, datetime, int, int]] = []
    for item in configured:
        try:
            hour, minute = (int(value) for value in str(item["time"]).split(":", 1))
            label = str(item["label"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append(
            (
                label,
                datetime(
                    local_date.year,
                    local_date.month,
                    local_date.day,
                    hour,
                    minute,
                    tzinfo=zone,
                ),
                int(
                    item.get(
                        "meteoblue_lead_minutes",
                        settings.meteoblue_checkpoint_lead_minutes,
                    )
                ),
                int(
                    item.get(
                        "meteoblue_grace_minutes",
                        settings.meteoblue_checkpoint_grace_minutes,
                    )
                ),
            )
        )
    return tuple(rows)


def _meteoblue_checkpoint_slot(
    airport: dict,
    as_of: datetime,
) -> tuple[str, datetime] | None:
    """Select the one named checkpoint whose configured call window is active."""
    local_as_of = _as_utc(as_of).astimezone(ZoneInfo(airport["timezone"]))
    eligible = [
        (label, target_at)
        for label, target_at, lead_minutes, grace_minutes in _checkpoint_schedule_for_airport(
            airport,
            local_date=local_as_of.date(),
        )
        if target_at - timedelta(minutes=max(0, lead_minutes))
        <= local_as_of
        <= target_at + timedelta(minutes=max(0, grace_minutes))
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda item: abs((local_as_of - item[1]).total_seconds()))


def _provider_lock_key(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _meteoblue_poll_policy(
    session,
    *,
    airport_code: str,
    airport: dict,
    as_of: datetime,
) -> tuple[bool, str]:
    """Enforce a persistent free-tier budget before any Meteoblue request.

    A durable ProviderCall row accounts for both successful and failed attempts.
    One attempt is allowed for each named Madrid checkpoint. PostgreSQL advisory
    locks prevent a Streamlit refresh and a GitHub collector from spending the same
    checkpoint budget concurrently.
    """
    if not settings.meteoblue_api_key or not settings.meteoblue_url_template:
        return False, "not-configured"
    if session.info.get("meteoblue_rate_limited"):
        return False, "rate-limited-cooldown"

    local_as_of = _as_utc(as_of).astimezone(ZoneInfo(airport["timezone"]))
    slot = _meteoblue_checkpoint_slot(airport, as_of)
    if slot is None:
        return False, "outside-checkpoint-window"
    checkpoint_label, checkpoint_at = slot
    local_start = local_as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc)

    cooldown_start = _as_utc(as_of) - timedelta(
        hours=max(1, settings.meteoblue_rate_limit_cooldown_hours)
    )
    recent_rate_limits = list(
        session.scalars(
            select(CollectionCoverage)
            .where(
                CollectionCoverage.data_type == "meteoblue",
                CollectionCoverage.scheduled_at >= cooldown_start,
            )
            .order_by(CollectionCoverage.scheduled_at.desc())
        )
    )
    rate_limit_markers = ("429", "rate limit", "quota", "credit")
    for attempt in recent_rate_limits:
        scheduled = _as_utc(attempt.scheduled_at)
        reason = str(attempt.reason or "").casefold()
        if scheduled >= cooldown_start and any(marker in reason for marker in rate_limit_markers):
            return False, "rate-limited-cooldown"

    existing = session.scalar(
        select(ProviderCall).where(
            ProviderCall.airport == airport_code,
            ProviderCall.provider == "meteoblue",
            ProviderCall.local_date == local_as_of.date(),
            ProviderCall.checkpoint_label == checkpoint_label,
        )
    )
    if existing is not None:
        return False, f"checkpoint-reused:{checkpoint_label}"

    attempts_today = int(
        session.scalar(
            select(func.count(ProviderCall.id)).where(
                ProviderCall.airport == airport_code,
                ProviderCall.provider == "meteoblue",
                ProviderCall.attempted_at >= start_utc,
                ProviderCall.attempted_at < end_utc,
            )
        )
        or 0
    )
    if attempts_today >= max(0, settings.meteoblue_daily_call_limit):
        return False, "budget-skipped"
    if session.bind.dialect.name == "postgresql":
        acquired = bool(
            session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
                {
                    "lock_key": _provider_lock_key(
                        airport_code,
                        "meteoblue",
                        local_as_of.date(),
                        checkpoint_label,
                    )
                },
            )
        )
        if not acquired:
            return False, f"checkpoint-in-progress:{checkpoint_label}"
    session.info["meteoblue_checkpoint"] = {
        "label": checkpoint_label,
        "local_date": local_as_of.date(),
        "target_at": checkpoint_at.astimezone(timezone.utc),
    }
    session.add(
        ProviderCall(
            airport=airport_code,
            provider="meteoblue",
            local_date=local_as_of.date(),
            checkpoint_label=checkpoint_label,
            target_at=checkpoint_at.astimezone(timezone.utc),
            attempted_at=_as_utc(as_of),
            status="reserved",
            rows_written=0,
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False, f"checkpoint-in-progress:{checkpoint_label}"
    return True, f"requested:{checkpoint_label}"


def _record_meteoblue_call(
    session,
    *,
    airport_code: str,
    airport: dict,
    attempted_at: datetime,
    status: str,
    rows_written: int,
    reason: str | None,
) -> None:
    checkpoint = session.info.get("meteoblue_checkpoint")
    if not checkpoint:
        slot = _meteoblue_checkpoint_slot(airport, attempted_at)
        if slot is not None:
            label, target_at = slot
            local_date = _as_utc(attempted_at).astimezone(
                ZoneInfo(airport["timezone"])
            ).date()
            checkpoint = {
                "label": label,
                "local_date": local_date,
                "target_at": target_at.astimezone(timezone.utc),
            }
    if not checkpoint:
        return
    _upsert(
        session,
        ProviderCall,
        {
            "airport": airport_code,
            "provider": "meteoblue",
            "local_date": checkpoint["local_date"],
            "checkpoint_label": checkpoint["label"],
        },
        {
            "target_at": checkpoint["target_at"],
            "attempted_at": _as_utc(attempted_at),
            "status": status,
            "rows_written": int(rows_written),
            "reason": reason,
        },
    )


def _store_current_provider_forecasts(
    session,
    *,
    airport_code: str,
    airport: dict,
    as_of: datetime,
    days: int = 3,
) -> dict[str, object]:
    """Fetch due providers concurrently and persist their rows serially.

    Only network reads run in worker threads. The caller-owned SQLAlchemy session is
    never shared with a worker, keeping SQLite on the established single-writer path.
    """
    started = time.perf_counter()
    local_target = _as_utc(as_of).astimezone(ZoneInfo(airport["timezone"])).date()
    counts: dict[str, object] = {
        "forecasts": 0,
        "hourly_forecasts": 0,
        "open_meteo_polls": 0,
        "meteoblue_polls": 0,
        "provider_timings_seconds": {},
        "provider_status": {},
        "provider_coverage": [],
    }
    forecast_rows: list[dict] = []
    hourly_rows: list[dict] = []
    tasks: dict[str, tuple[Callable, tuple, dict]] = {}
    open_meteo_due = _source_refresh_due(
        session,
        airport_code=airport_code,
        source="open-meteo",
        target=local_target,
        as_of=as_of,
        maximum_age_minutes=settings.live_open_meteo_refresh_minutes,
    )
    if open_meteo_due:
        counts["open_meteo_polls"] = 1
        for model in airport["models"]:
            tasks[f"open-meteo/daily/{model}"] = (
                open_meteo_forecast,
                (airport, model, days),
                {
                    "attempts": 1,
                    "timeout": settings.collector_provider_timeout_seconds,
                    "metadata_attempts": 1,
                    "metadata_timeout": min(
                        5.0, settings.collector_provider_timeout_seconds
                    ),
                },
            )
            tasks[f"open-meteo/hourly/{model}"] = (
                open_meteo_hourly,
                (airport, model, days),
                {
                    "attempts": 1,
                    "timeout": settings.collector_provider_timeout_seconds,
                },
            )

    meteoblue_due, meteoblue_policy = _meteoblue_poll_policy(
        session,
        airport_code=airport_code,
        airport=airport,
        as_of=as_of,
    )
    if meteoblue_due:
        counts["meteoblue_polls"] = 1
        tasks["meteoblue/daily"] = (
            meteoblue_forecast,
            (airport,),
            {
                "attempts": 1,
                "timeout": settings.collector_provider_timeout_seconds,
            },
        )

    task_results: dict[str, list[dict]] = {}
    task_metrics: dict[str, dict[str, object]] = {}
    if settings.meteoblue_api_key and not meteoblue_due:
        task_metrics["meteoblue/daily"] = {
            "status": "skipped",
            "duration_seconds": 0.0,
            "rows_read": 0,
            "attempts": 0,
            "reason": meteoblue_policy,
        }

    def fetch(
        function: Callable, args: tuple, kwargs: dict
    ) -> tuple[list[dict], str | None, float]:
        task_started = time.perf_counter()
        try:
            rows = list(function(*args, **kwargs) or [])
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}", time.perf_counter() - task_started
        return rows, None, time.perf_counter() - task_started

    if tasks:
        workers = max(
            1,
            min(settings.collector_provider_workers, len(tasks)),
        )
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="collector-provider",
        ) as pool:
            futures = {
                pool.submit(fetch, function, args, kwargs): label
                for label, (function, args, kwargs) in tasks.items()
            }
            for future in as_completed(futures):
                label = futures[future]
                rows, error, duration = future.result()
                task_results[label] = rows
                task_metrics[label] = {
                    "status": "failed" if error else "success",
                    "duration_seconds": round(duration, 3),
                    "rows_read": len(rows),
                    "attempts": 1,
                    "reason": error,
                }
                if error:
                    print(f"WARN {airport_code}/{label}: {error}")
                    if label == "meteoblue/daily" and any(
                        marker in error.casefold()
                        for marker in ("429", "rate limit", "quota", "credit")
                    ):
                        session.info["meteoblue_rate_limited"] = True

    for label, rows in task_results.items():
        if label.startswith("open-meteo/hourly/"):
            hourly_rows.extend(rows)
        else:
            forecast_rows.extend(rows)

    counts["hourly_forecasts"] = _upsert_batch(
        session,
        HourlyForecast,
        hourly_rows,
        lambda item: {
            "airport": airport_code,
            "model": item["model"],
            "run_at": item["run_at"],
            "valid_at": item["valid_at"],
        },
        lambda item: {
            "temp_c": item["temp_c"],
            "dewpoint_c": item["dewpoint_c"],
            "cloud_cover": item["cloud_cover"],
            "wind_kph": item["wind_kph"],
            "wind_direction": item["wind_direction"],
            "radiation_wm2": item["radiation_wm2"],
            "temp_850hpa_c": item["temp_850hpa_c"],
        },
        f"{airport_code}/live hourly forecasts",
    )

    counts["forecasts"] += _upsert_batch(
        session,
        Forecast,
        forecast_rows,
        lambda item: {
            "airport": airport_code,
            "model": item["model"],
            "run_at": item["run_at"],
            "target_date": item["target_date"],
        },
        lambda item: {
            "max_temp_c": item["max_temp_c"],
            "source": item["source"],
            "horizon": item["horizon"],
            "model_run_at": item.get("model_run_at"),
            "available_at": item.get("available_at"),
            "fetched_at": item.get("fetched_at", item["run_at"]),
            "provenance_status": item.get("provenance_status"),
        },
        f"{airport_code}/live current forecasts",
    )
    if meteoblue_due:
        meteoblue_metric = task_metrics.get("meteoblue/daily", {})
        meteoblue_rows = task_results.get("meteoblue/daily", [])
        _record_meteoblue_call(
            session,
            airport_code=airport_code,
            airport=airport,
            attempted_at=as_of,
            status=str(meteoblue_metric.get("status") or "failed"),
            rows_written=len(meteoblue_rows),
            reason=(
                str(meteoblue_metric.get("reason"))
                if meteoblue_metric.get("reason")
                else None
            ),
        )
    timings = {
        label: metric["duration_seconds"] for label, metric in task_metrics.items()
    }
    statuses = {label: metric["status"] for label, metric in task_metrics.items()}
    counts["provider_timings_seconds"] = timings
    counts["provider_status"] = statuses
    for provider in ("open-meteo", "meteoblue"):
        provider_metrics = {
            label: metric
            for label, metric in task_metrics.items()
            if label.startswith(provider + "/")
        }
        if not provider_metrics:
            continue
        failures = [
            metric for metric in provider_metrics.values() if metric["status"] != "success"
        ]
        skipped = [
            metric for metric in provider_metrics.values() if metric["status"] == "skipped"
        ]
        skipped_reason = str(skipped[0].get("reason")) if skipped else None
        counts["provider_coverage"].append(
            {
                "airport": airport_code,
                "data_type": provider,
                "status": (
                    skipped_reason
                    if len(skipped) == len(provider_metrics)
                    else "source_or_parser_failed"
                    if len(failures) == len(provider_metrics)
                    else "partial_provider_failure"
                    if failures
                    else "stored_pending_persistence"
                ),
                "latest_source_at": as_of,
                "rows_read": sum(
                    int(metric["rows_read"]) for metric in provider_metrics.values()
                ),
                "rows_written": (
                    int(counts["forecasts"])
                    + int(counts["hourly_forecasts"])
                    if provider == "open-meteo"
                    else len(task_results.get("meteoblue/daily", []))
                ),
                "source_age_minutes": 0.0,
                "duration_seconds": max(
                    float(metric["duration_seconds"])
                    for metric in provider_metrics.values()
                ),
                "attempts": sum(
                    int(metric.get("attempts", 0))
                    for metric in provider_metrics.values()
                ),
                "metrics": provider_metrics,
                "reason": "; ".join(
                    str(metric["reason"])
                    for metric in failures
                    if metric.get("reason")
                )
                or None,
            }
        )
    counts["airport_elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return counts


def _signal_timing(captured_at: datetime, target: date, timezone_name: str) -> str:
    local = captured_at.astimezone(ZoneInfo(timezone_name))
    lead_days = (target - local.date()).days
    if lead_days >= 2:
        return "D-2 or earlier"
    if lead_days == 1:
        return "D-1"
    if local.date() > target:
        return "After target day"
    return "D0 morning" if local.hour < 12 else "D0 live"


def _research_checkpoint_schedule(
    target: date,
    airport: dict,
) -> tuple[tuple[str, datetime], ...]:
    """Return configured Madrid-local information cut-offs for a target day."""
    zone = ZoneInfo(airport["timezone"])
    schedule: list[tuple[str, datetime]] = []
    for item in airport.get("decision_checkpoints_local") or []:
        try:
            hour, minute = (int(value) for value in str(item["time"]).split(":", 1))
            offset = int(item.get("target_day_offset", 0))
            label = str(item["label"])
        except (KeyError, TypeError, ValueError):
            continue
        checkpoint_day = target - timedelta(days=offset)
        schedule.append(
            (
                label,
                datetime(
                    checkpoint_day.year,
                    checkpoint_day.month,
                    checkpoint_day.day,
                    hour,
                    minute,
                    tzinfo=zone,
                ),
            )
        )
    return tuple(schedule)


def _checkpoint_provenance(
    session,
    *,
    code: str,
    target: date,
    checkpoint_at: datetime,
    current_time: datetime,
    label: str,
    expected_models: Iterable[str] | None = None,
) -> dict[str, object]:
    """Describe the exact pre-cutoff forecast information used by a checkpoint."""
    frame = read_archive_live(
        Forecast,
        session.connection(),
        filters={"airport": code, "target_date": target},
    )
    eligible: list[tuple[datetime, object]] = []
    cutoff = _as_utc(checkpoint_at)
    def present(value: object) -> bool:
        return value is not None and not bool(pd.isna(value))

    for row in frame.itertuples():
        effective = next(
            (
                value
                for value in (row.available_at, row.fetched_at, row.run_at)
                if present(value)
            ),
            None,
        )
        if effective is None:
            continue
        effective_utc = _as_utc(effective)
        if effective_utc <= cutoff:
            eligible.append((effective_utc, row))
    latest_by_model: dict[str, tuple[datetime, object]] = {}
    for effective, row in eligible:
        current = latest_by_model.get(row.model)
        if current is None or effective > current[0]:
            latest_by_model[row.model] = (effective, row)
    selected_all = sorted(latest_by_model.values(), key=lambda item: item[1].model)
    expected = {
        str(model) for model in (expected_models or latest_by_model) if str(model)
    }
    selected = [item for item in selected_all if str(item[1].model) in expected]
    fresh_selected = [
        item
        for item in selected
        if max(0.0, (cutoff - item[0]).total_seconds() / 60)
        <= settings.maximum_live_model_age_minutes
    ]
    available_models = {str(item[1].model) for item in selected_all}
    relevant_models = {str(item[1].model) for item in selected}
    extra_models = available_models - expected
    source_at = max((item[0] for item in selected), default=None)
    gap_minutes = (
        max(0.0, (cutoff - source_at).total_seconds() / 60)
        if source_at is not None
        else None
    )
    ages = sorted(
        max(0.0, (cutoff - effective).total_seconds() / 60)
        for effective, _row in selected
    )
    minimum_age = ages[0] if ages else None
    maximum_age = ages[-1] if ages else None
    median_age = (
        ages[len(ages) // 2]
        if len(ages) % 2
        else (ages[len(ages) // 2 - 1] + ages[len(ages) // 2]) / 2
        if ages
        else None
    )
    source_models = len(relevant_models)
    coverage_ratio = min(1.0, source_models / len(expected)) if expected else 0.0
    evidence_class = (
        "unavailable"
        if not relevant_models
        else "complete"
        if coverage_ratio >= 0.8
        else "partial"
        if coverage_ratio >= 0.6
        else "insufficient"
    )
    freshness_status = (
        "unavailable"
        if maximum_age is None
        else "fresh"
        if maximum_age <= 30
        else "aging"
        if maximum_age <= 90
        else "stale"
    )
    available_times = [
        _as_utc(row.available_at)
        for _effective, row in selected
        if present(row.available_at)
    ]
    fetched_times = [
        _as_utc(row.fetched_at)
        for _effective, row in selected
        if present(row.fetched_at)
    ]
    run_times = [
        _as_utc(row.model_run_at)
        for _effective, row in selected
        if present(row.model_run_at)
    ]
    provenance = [
        {
            "model": row.model,
            "source": row.source,
            "model_run_at": row.model_run_at.isoformat() if row.model_run_at else None,
            "available_at": row.available_at.isoformat() if row.available_at else None,
            "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
            "effective_available_at": effective.isoformat(),
            "age_at_cutoff_minutes": max(
                0.0, (cutoff - effective).total_seconds() / 60
            ),
            "relevant_to_checkpoint": str(row.model) in expected,
            "selection_status": (
                "eligible-for-checkpoint"
                if str(row.model) in expected
                else "available-not-expected"
            ),
            "exclusion_reason": (
                None
                if str(row.model) in expected
                else "extra model outside checkpoint expectation set"
            ),
            "provenance_status": row.provenance_status,
        }
        for effective, row in selected_all
    ]
    recorded_at = _as_utc(current_time)
    reconstructed = recorded_at > cutoff + timedelta(
        minutes=max(1, settings.checkpoint_capture_grace_minutes)
    )
    return {
        "checkpoint_label": label,
        "checkpoint_at": cutoff,
        "checkpoint_recorded_at": recorded_at,
        "source_captured_at": source_at,
        "checkpoint_gap_minutes": gap_minutes,
        "checkpoint_reconstructed": reconstructed,
        "checkpoint_status": (
            "unavailable"
            if not relevant_models
            else "reconstructed-causal"
            if reconstructed
            else "scheduled-causal"
        ),
        "freshness_status": freshness_status,
        "evidence_class": evidence_class,
        "source_age_at_checkpoint_minutes": maximum_age,
        "source_age_min_minutes": minimum_age,
        "source_age_median_minutes": median_age,
        "source_age_max_minutes": maximum_age,
        "expected_model_count": len(expected),
        "source_model_count": source_models,
        "available_model_count": len(available_models),
        "fresh_model_count": len(fresh_selected),
        "source_coverage_ratio": coverage_ratio,
        "expected_models_json": json.dumps(sorted(expected), separators=(",", ":")),
        "available_models_json": json.dumps(
            sorted(available_models), separators=(",", ":")
        ),
        "extra_models_json": json.dumps(sorted(extra_models), separators=(",", ":")),
        "forecast_run_at": max(run_times, default=None),
        "forecast_available_at": max(available_times, default=None),
        "forecast_fetched_at": max(fetched_times, default=None),
        "source_provenance_json": json.dumps(provenance, separators=(",", ":")),
    }


def provisional_metar_actuals(
    rows: list[dict],
    airport: dict,
    *,
    as_of: datetime | None = None,
    include_current_day: bool = False,
) -> list[dict]:
    """Create a learning value from a sufficiently complete METAR day."""
    if not rows:
        return []
    now = (as_of or datetime.now(timezone.utc)).astimezone(
        ZoneInfo(airport["timezone"])
    )
    frame = pd.DataFrame(rows)
    if frame.empty or "observed_at" not in frame or "temp_c" not in frame:
        return []
    frame["observed_at"] = pd.to_datetime(frame.observed_at, utc=True)
    frame["local_at"] = frame.observed_at.dt.tz_convert(airport["timezone"])
    latest_allowed = now.date() if include_current_day else now.date() - timedelta(days=1)
    # Only derive the newest eligible day. Older complete days are promoted by
    # _restore_stored_station_actuals from the immutable archive+live path.
    # Re-evaluating every day in a rolling provider window is what allowed a
    # peak that fell out of the 48-hour response to lower a stored Actual.
    frame = frame[frame.local_at.dt.date == latest_allowed].copy()
    if frame.empty:
        return []
    configured_end = str(airport.get("critical_window_local", ["", "18:00"])[-1])
    try:
        end_hour, end_minute = (int(value) for value in configured_end.split(":", 1))
        required_end_minutes = end_hour * 60 + end_minute
    except (TypeError, ValueError):
        required_end_minutes = 18 * 60
    actuals = []
    for target, day in frame.groupby(frame.local_at.dt.date):
        day = day.dropna(subset=["temp_c"]).sort_values("local_at")
        if len(day) < 8:
            continue
        span_hours = (
            day.local_at.iloc[-1] - day.local_at.iloc[0]
        ).total_seconds() / 3600
        latest_minutes = int(day.local_at.iloc[-1].hour) * 60 + int(
            day.local_at.iloc[-1].minute
        )
        if span_hours < 6 or latest_minutes < required_end_minutes:
            continue
        actuals.append(
            {
                "target_date": target,
                "max_temp_c": float(day.temp_c.max()),
            }
        )
    return actuals


def sync_airport_universe(*, include_closed: bool = False) -> dict[str, int]:
    """Persist every discovered market city, including cities without a station map."""
    init_db()
    events = discover_polymarket_temperature_events(include_closed=include_closed)
    city_index = market_city_index()
    now = datetime.now(timezone.utc)
    mapped = 0
    unknown = 0
    with Session() as session:
        if events and not include_closed:
            for existing in session.scalars(
                select(AirportMarketUniverse).where(AirportMarketUniverse.active.is_(True))
            ):
                existing.active = False
        for event in events:
            match = city_index.get(event["market_city"])
            code = match[0] if match else None
            details = match[1] if match else {}
            status = (
                details.get("station_match", "candidate station")
                if match
                else "station mapping required"
            )
            current = session.scalar(
                select(AirportMarketUniverse).where(
                    AirportMarketUniverse.market_city == event["market_city"]
                )
            )
            values = {
                "display_name": event["display_name"],
                "airport": code,
                "mapping_status": status,
                "market_unit": event.get("market_unit"),
                "resolution_source": event.get("resolution_source"),
                "last_seen_at": now,
                "latest_event_slug": event["event_slug"],
                "latest_target_date": event["target_date"],
                "active": bool(event.get("active", True)),
            }
            if current is None:
                session.add(
                    AirportMarketUniverse(
                        market_city=event["market_city"],
                        first_seen_at=now,
                        **values,
                    )
                )
            else:
                for key, value in values.items():
                    setattr(current, key, value)
            mapped += int(code is not None)
            unknown += int(code is None)
        session.commit()
    return {"cities": len(events), "mapped": mapped, "unmapped": unknown}


def build_current_live_nowcast(
    *,
    airport: dict,
    target: date,
    captured_at: datetime,
    forecasts: pd.DataFrame,
    actuals: pd.DataFrame,
    observations: pd.DataFrame,
    hourly: pd.DataFrame,
    markets: pd.DataFrame,
    tafs: pd.DataFrame,
    snapshots: pd.DataFrame,
    variants: pd.DataFrame,
    prior_terminal_status: DayStatus | None = None,
):
    """Build the one canonical current Champion used by every Streamlit view."""
    regime_profiles = continuous_regime_profiles(airport)
    nowcast = build_live_nowcast(
        forecasts=forecasts,
        actuals=actuals,
        observations=observations,
        hourly=hourly,
        markets=markets,
        tafs=tafs,
        timezone_name=airport["timezone"],
        target=target,
        as_of=captured_at,
        wind_profile=airport.get("heat_wind_profile"),
        routine_metar_minutes=airport.get("metar_minutes"),
        pre_metar_guard_minutes=airport.get("pre_metar_guard_minutes", 7),
        critical_window_local=airport.get("critical_window_local"),
        post_convective_profile=regime_profiles["post_convective"],
        heat_regime_profile=regime_profiles["heat"],
        phase_amplitude_profile=regime_profiles["phase"],
        maritime_advection_profile=regime_profiles["maritime_advection"],
        maritime_low_range_profile=regime_profiles["maritime_low_range"],
        live_adjustment_guardrails=airport.get("live_adjustment_guardrails"),
        recent_warm_bias_profile=airport.get("recent_warm_bias_challenger"),
        future_reheating_profile=airport.get("future_reheating"),
        prior_terminal_status=prior_terminal_status,
        maximum_model_age_minutes=settings.maximum_live_model_age_minutes,
    )
    if nowcast is None:
        return None
    memory_config = dict(airport.get("regime_memory") or {})
    memory_config.setdefault(
        "allow_promoted",
        settings.regime_memory_auto_promotion_enabled
        or settings.regime_memory_allow_promoted,
    )
    memory_config.setdefault(
        "minimum_oos_days",
        settings.regime_memory_minimum_oos_days,
    )
    return enrich_nowcast_with_regime_memory(
        nowcast,
        snapshots,
        actuals,
        observations,
        variants,
        airport_profile=airport,
        timezone_name=airport["timezone"],
        target=target,
        as_of=captured_at,
        config=memory_config,
    )


def _build_nowcast_from_session(
    session,
    code: str,
    airport: dict,
    target: date,
    captured_at: datetime,
    market_rows: list[dict],
):
    connection = session.connection()
    zone = ZoneInfo(airport["timezone"])
    target_start = datetime(
        target.year,
        target.month,
        target.day,
        tzinfo=zone,
    ).astimezone(timezone.utc)
    next_day = target + timedelta(days=1)
    target_end = datetime(
        next_day.year,
        next_day.month,
        next_day.day,
        tzinfo=zone,
    ).astimezone(timezone.utc)
    history_start = target - timedelta(days=100)
    forecasts = read_archive_live(
        Forecast,
        connection,
        filters={"airport": code},
        minimums={"target_date": history_start},
        maximums={"target_date": target},
    )
    actuals = read_archive_live(
        DailyActual,
        connection,
        filters={"airport": code},
        minimums={"target_date": target - timedelta(days=120)},
        maximums={"target_date": target - timedelta(days=1)},
    )
    observations = read_archive_live(
        Observation,
        connection,
        filters={"airport": code},
        minimums={"observed_at": target_start - timedelta(days=7)},
        maximums={"observed_at": _as_utc(captured_at)},
    )
    hourly = read_archive_live(
        HourlyForecast,
        connection,
        filters={"airport": code},
        minimums={"valid_at": target_start},
        maximums={"valid_at": target_end},
    )
    tafs = read_archive_live(
        TafReport,
        connection,
        filters={"airport": code},
        minimums={"issue_time": target_start - timedelta(days=2)},
        maximums={"issue_time": _as_utc(captured_at)},
    )
    history_snapshots = read_archive_live(
        ForecastSnapshot,
        connection,
        filters={"airport": code},
        minimums={"target_date": history_start},
        maximums={"target_date": target},
    )
    snapshots = (
        history_snapshots[
            pd.to_datetime(history_snapshots.target_date).dt.date < target
        ].copy()
        if not history_snapshots.empty
        else history_snapshots
    )
    cutoff = pd.Timestamp(_as_utc(captured_at))
    if not forecasts.empty:
        for column in ("run_at", "available_at", "fetched_at"):
            if column in forecasts:
                forecasts[column] = pd.to_datetime(forecasts[column], utc=True, errors="coerce")
        effective = forecasts.get("available_at", pd.Series(pd.NaT, index=forecasts.index))
        effective = effective.fillna(
            forecasts.get("fetched_at", pd.Series(pd.NaT, index=forecasts.index))
        ).fillna(forecasts.run_at)
        forecasts = forecasts[effective <= cutoff].copy()
        # Replays use the time at which guidance first became available, never a
        # later fetch timestamp. This keeps freshness and latest-run selection causal.
        effective = effective.loc[forecasts.index]
        forecasts["run_at"] = effective
        forecasts["fetched_at"] = effective
    if not actuals.empty:
        actuals = actuals[pd.to_datetime(actuals.target_date).dt.date < target].copy()
    if not observations.empty:
        observations["observed_at"] = pd.to_datetime(
            observations.observed_at, utc=True, errors="coerce"
        )
        observations = observations[observations.observed_at <= cutoff].copy()
    if not hourly.empty:
        hourly["run_at"] = pd.to_datetime(hourly.run_at, utc=True, errors="coerce")
        hourly = hourly[hourly.run_at <= cutoff].copy()
    if not tafs.empty:
        for column in ("issue_time", "collected_at", "first_seen_at"):
            if column in tafs:
                tafs[column] = pd.to_datetime(tafs[column], utc=True, errors="coerce")
        known_at = tafs.get(
            "first_seen_at", pd.Series(pd.NaT, index=tafs.index)
        ).fillna(tafs.get("collected_at", pd.Series(pd.NaT, index=tafs.index)))
        known_at = known_at.fillna(tafs.issue_time)
        tafs = tafs[(tafs.issue_time <= cutoff) & (known_at <= cutoff)].copy()
    if market_rows:
        market_rows = [
            row
            for row in market_rows
            if _as_utc(row.get("captured_at", captured_at)) <= _as_utc(captured_at)
        ]
    variants = read_archive_live(
        ForecastVariantSnapshot,
        connection,
        filters={"airport": code},
        minimums={"target_date": history_start},
        maximums={"target_date": target - timedelta(days=1)},
    )
    if not variants.empty:
        variants = variants[
            pd.to_datetime(variants.target_date).dt.date < target
        ].copy()
    prior_terminal_status = None
    if not history_snapshots.empty:
        same_target = history_snapshots[
            (pd.to_datetime(history_snapshots.target_date).dt.date == target)
            & (
                pd.to_datetime(history_snapshots.captured_at, utc=True, errors="coerce")
                < cutoff
            )
            & history_snapshots.day_phase.isin(["locked", "final", "resolved"])
        ].sort_values("captured_at")
        if not same_target.empty:
            previous = same_target.iloc[-1]
            observed = previous.get("observed_max_c")
            bucket = (
                math.floor(float(observed) + 0.5)
                if pd.notna(observed)
                else None
            )
            prior_terminal_status = DayStatus(
                phase=str(previous.day_phase),
                label="Peak locked",
                is_locked=True,
                minimum_bucket=bucket,
                maximum_bucket=bucket,
                remaining_heating_c=0.0,
                explanation="Terminal state restored from the prior causal snapshot.",
            )
    return build_current_live_nowcast(
        airport=airport,
        target=target,
        captured_at=captured_at,
        forecasts=forecasts,
        actuals=actuals,
        observations=observations,
        hourly=hourly,
        markets=pd.DataFrame(market_rows),
        tafs=tafs,
        snapshots=snapshots,
        variants=variants,
        prior_terminal_status=prior_terminal_status,
    )


def _live_snapshot_provenance(nowcast, airport: dict) -> dict[str, object]:
    """Persist the model set and age that actually formed a live Champion."""
    frame = nowcast.model_freshness.copy()
    if frame.empty or "model" not in frame:
        return {}
    expected = {
        str(model)
        for model in [*airport.get("models", []), "meteoblue"]
        if str(model)
    }
    available = set(frame.model.dropna().astype(str))
    used = set(nowcast.current.model.dropna().astype(str))
    relevant_available = available & expected
    if "is_fresh" in frame:
        fresh_mask = frame["is_fresh"].astype(bool)
    else:
        fresh_mask = (
            pd.to_numeric(frame.get("age_minutes"), errors="coerce")
            <= settings.maximum_live_model_age_minutes
        )
    fresh = set(frame.loc[fresh_mask, "model"].dropna().astype(str)) & expected
    extra = available - expected
    used_rows = frame[frame.model.astype(str).isin(used)].copy()
    ages = sorted(
        float(value)
        for value in pd.to_numeric(
            used_rows.get("age_minutes", pd.Series(dtype=float)), errors="coerce"
        ).dropna()
    )
    minimum_age = ages[0] if ages else None
    maximum_age = ages[-1] if ages else None
    median_age = (
        ages[len(ages) // 2]
        if len(ages) % 2
        else (ages[len(ages) // 2 - 1] + ages[len(ages) // 2]) / 2
        if ages
        else None
    )
    freshness = (
        "unavailable"
        if maximum_age is None
        else "fresh"
        if maximum_age <= 30
        else "aging"
        if maximum_age <= 90
        else "stale"
    )

    def latest_timestamp(column: str) -> datetime | None:
        if column not in used_rows:
            return None
        values = pd.to_datetime(used_rows[column], utc=True, errors="coerce").dropna()
        return values.max().to_pydatetime() if not values.empty else None

    provenance = []
    def timestamp_text(value: object) -> str | None:
        if value is None or bool(pd.isna(value)):
            return None
        return pd.Timestamp(value).isoformat()

    for row in frame.itertuples():
        model = str(row.model)
        age = getattr(row, "age_minutes", None)
        age_value = float(age) if age is not None and not pd.isna(age) else None
        if model in used:
            exclusion_reason = None
            selection_status = "used"
        elif model not in expected:
            exclusion_reason = "extra model outside configured Champion set"
            selection_status = "available-not-expected"
        elif age_value is not None and age_value > settings.maximum_live_model_age_minutes:
            exclusion_reason = "stale at this checkpoint"
            selection_status = "excluded"
        else:
            exclusion_reason = "not selected by current Champion input filter"
            selection_status = "excluded"
        provenance.append(
            {
                "model": model,
                "source": getattr(row, "source", None),
                "model_run_at": timestamp_text(getattr(row, "model_run_at", None)),
                "available_at": timestamp_text(getattr(row, "available_at", None)),
                "fetched_at": timestamp_text(getattr(row, "fetched_at", None)),
                "age_minutes": age_value,
                "used_by_champion": model in used,
                "expected": model in expected,
                "selection_status": selection_status,
                "exclusion_reason": exclusion_reason,
            }
        )
    coverage = min(1.0, len(relevant_available) / len(expected)) if expected else 0.0
    return {
        "source_captured_at": latest_timestamp("data_timestamp"),
        "freshness_status": freshness,
        "evidence_class": (
            "complete"
            if coverage >= 0.8
            else "partial"
            if coverage >= 0.6
            else "insufficient"
        ),
        "source_age_at_checkpoint_minutes": maximum_age,
        "source_age_min_minutes": minimum_age,
        "source_age_median_minutes": median_age,
        "source_age_max_minutes": maximum_age,
        "expected_model_count": len(expected),
        "source_model_count": len(relevant_available),
        "available_model_count": len(available),
        "fresh_model_count": len(fresh),
        "used_model_count": len(used),
        "source_coverage_ratio": coverage,
        "expected_models_json": json.dumps(sorted(expected), separators=(",", ":")),
        "available_models_json": json.dumps(sorted(available), separators=(",", ":")),
        "used_models_json": json.dumps(sorted(used), separators=(",", ":")),
        "extra_models_json": json.dumps(sorted(extra), separators=(",", ":")),
        "forecast_run_at": latest_timestamp("model_run_at"),
        "forecast_available_at": latest_timestamp("available_at"),
        "forecast_fetched_at": latest_timestamp("fetched_at"),
        "source_provenance_json": json.dumps(provenance, separators=(",", ":")),
    }


def _record_forecast_snapshot(
    session,
    code: str,
    airport: dict,
    target: date,
    captured_at: datetime,
    nowcast,
    *,
    checkpoint_metadata: dict[str, object] | None = None,
) -> int:
    """Persist one comparable observation of every forecast transformation."""
    if nowcast is None:
        return 0
    local_capture = captured_at.astimezone(ZoneInfo(airport["timezone"]))
    metar_conditioned_available = (
        target == local_capture.date() and nowcast.observed_max is not None
    )
    guidance = nowcast.taf_guidance
    taf_conflict = bool(
        guidance is not None
        and (
            guidance.agreement.startswith("Mild conflict")
            or guidance.agreement.startswith("Contradicts model")
        )
    )
    pre_taf_probabilities = dict(nowcast.metar_conditioned_probabilities or {})
    champion_probabilities = dict(nowcast.probabilities or {})
    pre_taf_mode = (
        max(pre_taf_probabilities, key=pre_taf_probabilities.get)
        if pre_taf_probabilities
        else None
    )
    champion_mode = (
        max(champion_probabilities, key=champion_probabilities.get)
        if champion_probabilities
        else None
    )
    taf_modal_flip = bool(
        guidance is not None
        and pre_taf_mode is not None
        and champion_mode is not None
        and pre_taf_mode != champion_mode
        and (
            abs(float(nowcast.taf_adjustment_c)) > 1e-9
            or abs(float(guidance.spread_addition_c)) > 1e-9
        )
    )
    row = {
        "airport": code,
        "target_date": target,
        "captured_at": captured_at,
        "timing": _signal_timing(captured_at, target, airport["timezone"]),
        "raw_model_mean_c": nowcast.raw_model_mean,
        "weighted_raw_c": nowcast.weighted_raw_mean,
        "bias_corrected_equal_c": nowcast.bias_corrected_equal_mean,
        "bias_corrected_c": nowcast.corrected.mean,
        "metar_conditioned_c": (
            nowcast.metar_conditioned_mean if metar_conditioned_available else None
        ),
        "final_forecast_c": nowcast.final_forecast_mean,
        "raw_spread_c": nowcast.raw_model_spread,
        "weighted_raw_spread_c": nowcast.weighted_raw_spread,
        "bias_corrected_equal_spread_c": nowcast.bias_corrected_equal_spread,
        "bias_corrected_spread_c": nowcast.corrected.spread,
        "metar_conditioned_spread_c": (
            nowcast.metar_conditioned_spread if metar_conditioned_available else None
        ),
        "final_spread_c": nowcast.final_forecast_spread,
        "observed_max_c": nowcast.observed_max,
        "latest_metar_at": nowcast.latest_observation_at,
        "expected_peak_at": nowcast.expected_peak_at,
        "hours_to_peak": nowcast.hours_to_peak,
        "day_phase": nowcast.day_status.phase,
        "model_count": len(nowcast.current),
        "taf_adjustment_c": nowcast.taf_adjustment_c,
        "taf_conflict": taf_conflict,
        "taf_report_id": guidance.report_id if guidance is not None else None,
        "taf_issue_time": guidance.issue_time if guidance is not None else None,
        "taf_first_seen_at": guidance.first_seen_at if guidance is not None else None,
        "taf_max_temp_c": guidance.max_temp_c if guidance is not None else None,
        "taf_content_hash": guidance.content_hash if guidance is not None else None,
        "pre_taf_modal_bucket_c": pre_taf_mode,
        "champion_modal_bucket_c": champion_mode,
        "taf_modal_bucket_flip": taf_modal_flip,
        "temp_anchor_adjustment_c": nowcast.adjustment_contributions.get("temperature_anchor", 0.0),
        "dryness_adjustment_c": nowcast.adjustment_contributions.get("dryness", 0.0),
        "dewpoint_trend_adjustment_c": nowcast.adjustment_contributions.get("dewpoint_trend", 0.0),
        "cloud_adjustment_c": nowcast.adjustment_contributions.get("cloud", 0.0),
        "heating_rate_adjustment_c": nowcast.adjustment_contributions.get("heating_rate", 0.0),
        "recent_error_adjustment_c": nowcast.adjustment_contributions.get(
            "recent_station_error", 0.0
        ),
        "radiation_adjustment_c": nowcast.adjustment_contributions.get("radiation", 0.0),
        "wind_adjustment_c": nowcast.adjustment_contributions.get("wind", 0.0),
        "run_trend_adjustment_c": nowcast.adjustment_contributions.get("run_trend", 0.0),
        "late_dry_mixing_adjustment_c": nowcast.adjustment_contributions.get(
            "late_dry_mixing", 0.0
        ),
        "failed_convection_adjustment_c": nowcast.adjustment_contributions.get(
            "failed_convection", 0.0
        ),
        "clear_sky_override_adjustment_c": nowcast.adjustment_contributions.get(
            "clear_sky_override", 0.0
        ),
        "rapid_heat_ramp_adjustment_c": float(
            nowcast.live_features.get("rapid_heat_ramp_adjustment_c", 0.0) or 0.0
        ),
        "regional_cluster_adjustment_c": float(
            nowcast.live_features.get("regional_cluster_adjustment_c", 0.0) or 0.0
        ),
        "persistent_hot_adjustment_c": float(
            nowcast.live_features.get("persistent_hot_adjustment_c", 0.0) or 0.0
        ),
        "phase_anchor_delta_c": float(
            nowcast.live_features.get("phase_anchor_delta_c", 0.0) or 0.0
        ),
        "maritime_advection_adjustment_c": float(
            nowcast.live_features.get("maritime_advection_adjustment_c", 0.0)
            or 0.0
        ),
        "rapid_heat_ramp_active": bool(
            nowcast.live_features.get("rapid_heat_ramp_active", 0)
        ),
        "regional_cluster_active": bool(
            nowcast.live_features.get("regional_cluster_active", 0)
        ),
        "persistent_hot_active": bool(
            nowcast.live_features.get("persistent_hot_active", 0)
        ),
        "phase_vs_amplitude_active": bool(
            nowcast.live_features.get("phase_vs_amplitude_active", 0)
        ),
        "maritime_advection_active": bool(
            nowcast.live_features.get("maritime_advection_active", 0)
        ),
        "maritime_low_range_active": bool(
            nowcast.live_features.get("maritime_low_range_active", 0)
        ),
        "post_convective_active": bool(
            nowcast.live_features.get("post_convective_uncertainty_active", 0)
        ),
        "post_convective_reports": int(
            nowcast.live_features.get("post_convective_reports_48h", 0) or 0
        ),
        "post_convective_spread_multiplier": float(
            nowcast.live_features.get("post_convective_spread_multiplier", 1.0)
            or 1.0
        ),
        "model_ceiling_reached_early": bool(
            nowcast.live_features.get("model_ceiling_reached_early", 0)
        ),
        "live_adjustment_c": nowcast.adjustment_contributions.get("total", 0.0),
        "features_json": json.dumps(nowcast.live_features, separators=(",", ":")),
        "peak_lock_json": json.dumps(
            {
                "phase": nowcast.day_status.phase,
                "label": nowcast.day_status.label,
                "explanation": nowcast.day_status.explanation,
                "remaining_model_rise_c": nowcast.remaining_rise_c,
                "future_radiation_max_wm2": nowcast.future_radiation_max,
                "observed_max_c": nowcast.observed_max,
            },
            separators=(",", ":"),
        ),
        "checkpoint_label": None,
        "checkpoint_at": None,
        "source_captured_at": None,
        "checkpoint_gap_minutes": None,
        "checkpoint_reconstructed": False,
        "checkpoint_status": None,
        "freshness_status": None,
        "evidence_class": None,
        "source_age_at_checkpoint_minutes": None,
        "source_age_min_minutes": None,
        "source_age_median_minutes": None,
        "source_age_max_minutes": None,
        "expected_model_count": None,
        "source_model_count": None,
        "available_model_count": None,
        "fresh_model_count": None,
        "used_model_count": None,
        "source_coverage_ratio": None,
        "expected_models_json": "[]",
        "available_models_json": "[]",
        "used_models_json": "[]",
        "extra_models_json": "[]",
        "forecast_run_at": None,
        "forecast_available_at": None,
        "forecast_fetched_at": None,
        "source_provenance_json": "[]",
        "post_peak_diagnostic_json": json.dumps(
            post_peak_diagnostic(nowcast, captured_at), separators=(",", ":")
        ),
        "market_snapshot_status": None,
        "market_snapshot_at": None,
        "market_bucket_count": None,
    }
    live_lineage = _live_snapshot_provenance(nowcast, airport)
    row.update(live_lineage)
    if checkpoint_metadata:
        row.update(checkpoint_metadata)
        # Checkpoint availability and actual Champion selection answer different
        # questions. Preserve the causal availability list while adding the
        # actual used/excluded decision from the frozen nowcast.
        row["used_model_count"] = live_lineage.get("used_model_count")
        row["used_models_json"] = live_lineage.get("used_models_json", "[]")
        try:
            checkpoint_sources = json.loads(
                str(checkpoint_metadata.get("source_provenance_json") or "[]")
            )
            live_sources = {
                str(item.get("model")): item
                for item in json.loads(str(live_lineage.get("source_provenance_json") or "[]"))
            }
            for source in checkpoint_sources:
                selection = live_sources.get(str(source.get("model")), {})
                source["used_by_champion"] = bool(selection.get("used_by_champion", False))
                source["selection_status"] = selection.get(
                    "selection_status", source.get("selection_status")
                )
                source["exclusion_reason"] = selection.get(
                    "exclusion_reason", source.get("exclusion_reason")
                )
            row["source_provenance_json"] = json.dumps(
                checkpoint_sources, separators=(",", ":")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return _upsert_batch(
        session,
        ForecastSnapshot,
        [row],
        lambda item: {
            "airport": item["airport"],
            "target_date": item["target_date"],
            "captured_at": item["captured_at"],
        },
        lambda item: {
            key: value
            for key, value in item.items()
            if key not in {"airport", "target_date", "captured_at"}
        },
        f"{code}/forecast ladder/{target}",
    )


def _record_forecast_variants(
    session,
    code: str,
    airport: dict,
    target: date,
    captured_at: datetime,
    nowcast,
) -> int:
    """Persist the champion and every active one-factor-disabled challenger."""
    if nowcast is None or not nowcast.challenger_variants:
        return 0
    timing = _signal_timing(captured_at, target, airport["timezone"])
    rows = [
        {
            "airport": code,
            "target_date": target,
            "captured_at": captured_at,
            "timing": timing,
            "variant": "Champion",
            "factor": None,
            "forecast_c": nowcast.final_forecast_mean,
            "spread_c": nowcast.final_forecast_spread,
            "probabilities_json": json.dumps(
                nowcast.probabilities,
                separators=(",", ":"),
            ),
            "forecast_confidence": nowcast.forecast_confidence,
            "day_phase": nowcast.day_status.phase,
        }
    ]
    for variant, values in nowcast.challenger_variants.items():
        rows.append(
            {
                "airport": code,
                "target_date": target,
                "captured_at": captured_at,
                "timing": timing,
                "variant": variant,
                "factor": values["factor"],
                "forecast_c": values["forecast_mean_c"],
                "spread_c": values["spread_c"],
                "probabilities_json": json.dumps(
                    values["probabilities"],
                    separators=(",", ":"),
                ),
                "forecast_confidence": values["forecast_confidence"],
                "day_phase": nowcast.day_status.phase,
            }
        )
    return _upsert_batch(
        session,
        ForecastVariantSnapshot,
        rows,
        lambda item: {
            "airport": item["airport"],
            "target_date": item["target_date"],
            "captured_at": item["captured_at"],
            "variant": item["variant"],
        },
        lambda item: {
            key: value
            for key, value in item.items()
            if key not in {"airport", "target_date", "captured_at", "variant"}
        },
        f"{code}/champion challengers/{target}",
    )


def _record_regime_memory_snapshot(
    session,
    code: str,
    airport: dict,
    target: date,
    captured_at: datetime,
    nowcast,
) -> int:
    """Persist the explainable early-warning state and its leakage-free analogs."""
    if nowcast is None or nowcast.regime_memory is None:
        return 0
    memory = nowcast.regime_memory
    row = {
        "airport": code,
        "target_date": target,
        "captured_at": captured_at,
        "timing": _signal_timing(captured_at, target, airport["timezone"]),
        "status": memory.status,
        "label": memory.label,
        "confidence": memory.confidence,
        "analog_count": memory.analog_count,
        "best_similarity": memory.best_similarity,
        "center_adjustment_c": memory.center_adjustment_c,
        "suggested_forecast_c": memory.suggested_forecast_c,
        "suggested_spread_c": memory.suggested_spread_c,
        "shadow_only": memory.shadow_only,
        "applied_to_champion": memory.applied_to_champion,
        "promotion_status": memory.promotion.status,
        "promotion_eligible": memory.promotion.eligible,
        "oos_days": memory.promotion.oos_days,
        "regimes_json": json.dumps(
            [
                {
                    "name": state.name,
                    "status": state.status,
                    "confidence": state.confidence,
                    "source": state.source,
                    "champion_effect": state.champion_effect,
                    "supports": list(state.supports),
                    "contradictions": list(state.contradictions),
                    "explanation": state.explanation,
                }
                for state in memory.regimes
            ],
            separators=(",", ":"),
        ),
        "analogs_json": json.dumps(
            [
                {
                    "target_date": analog.target_date,
                    "captured_at": analog.captured_at,
                    "similarity": analog.similarity,
                    "forecast_c": analog.forecast_c,
                    "actual_c": analog.actual_c,
                    "residual_c": analog.residual_c,
                    "matched_on": list(analog.matched_on),
                }
                for analog in memory.analogs
            ],
            separators=(",", ":"),
        ),
        "pro_signals_json": json.dumps(memory.pro_signals, separators=(",", ":")),
        "contra_signals_json": json.dumps(memory.contra_signals, separators=(",", ":")),
        "explanation": memory.explanation,
        "feature_signature_json": json.dumps(
            memory.feature_signature,
            separators=(",", ":"),
        ),
    }
    return _upsert_batch(
        session,
        RegimeMemorySnapshot,
        [row],
        lambda item: {
            "airport": item["airport"],
            "target_date": item["target_date"],
            "captured_at": item["captured_at"],
        },
        lambda item: {
            key: value
            for key, value in item.items()
            if key not in {"airport", "target_date", "captured_at"}
        },
        f"{code}/regime memory/{target}",
    )


def _record_signal_snapshots(
    session,
    code: str,
    airport: dict,
    market_rows: list[dict],
    nowcast=None,
) -> int:
    """Journal the exact model-versus-market view created by this collection."""
    if not market_rows or all(bool(row.get("closed")) for row in market_rows):
        return 0
    captured_at = max(row["captured_at"] for row in market_rows)
    target = market_rows[0]["target_date"]
    market_frame = pd.DataFrame(market_rows)
    if nowcast is None:
        nowcast = _build_nowcast_from_session(
            session, code, airport, target, captured_at, market_rows
        )
    if nowcast is None:
        return 0
    comparison = market_edges(nowcast.probabilities, market_frame)
    conflict = detect_market_model_conflict(nowcast.probabilities, market_frame)
    if nowcast.day_status.is_locked:
        comparison["signal"] = "Day complete"
    elif nowcast.metar_pending:
        comparison["signal"] = "METAR guard"
    elif conflict.is_conflict:
        comparison["signal"] = "Market-model conflict"
    timing = _signal_timing(captured_at, target, airport["timezone"])
    rows = []
    for row in comparison.itertuples():
        rows.append(
            {
                "market_id": str(row.market_id),
                "captured_at": captured_at,
                "airport": code,
                "target_date": target,
                "event_slug": str(row.event_slug),
                "bucket_label": str(row.bucket_label),
                "timing": timing,
                "model_probability": float(row.model_probability),
                "market_probability": float(row.yes_price),
                "buy_price": float(row.buy_price) if pd.notna(row.buy_price) else None,
                "edge": float(row.edge) if pd.notna(row.edge) else None,
                "signal": str(row.signal),
                "day_phase": nowcast.day_status.phase,
                "model_count": len(nowcast.current),
            }
        )
    return _upsert_batch(
        session,
        SignalSnapshot,
        rows,
        lambda item: {
            "market_id": item["market_id"],
            "captured_at": item["captured_at"],
        },
        lambda item: {
            key: value for key, value in item.items() if key not in {"market_id", "captured_at"}
        },
        f"{code}/signal journal/{target}",
    )


def _record_strategy_snapshots(
    session,
    code: str,
    airport: dict,
    market_rows: list[dict],
    nowcast,
) -> int:
    """Record one mode-bucket benchmark entry for every forecast stage."""
    if nowcast is None or not market_rows or all(bool(row.get("closed")) for row in market_rows):
        return 0
    captured_at = max(row["captured_at"] for row in market_rows)
    target = market_rows[0]["target_date"]
    timing = _signal_timing(captured_at, target, airport["timezone"])
    local_capture = captured_at.astimezone(ZoneInfo(airport["timezone"]))
    rows = []
    for strategy, probabilities in nowcast.stage_probabilities.items():
        if strategy == "METAR conditioned" and (
            target != local_capture.date() or nowcast.observed_max is None
        ):
            continue
        model_bucket = max(probabilities, key=probabilities.get)
        matches = [
            market
            for market in market_rows
            if (market.get("bucket_low_c") is None or model_bucket >= float(market["bucket_low_c"]))
            and (
                market.get("bucket_high_c") is None
                or model_bucket <= float(market["bucket_high_c"])
            )
        ]
        if not matches:
            continue
        market = min(
            matches,
            key=lambda item: (
                float("inf")
                if item.get("bucket_low_c") is None or item.get("bucket_high_c") is None
                else float(item["bucket_high_c"]) - float(item["bucket_low_c"])
            ),
        )
        buy_price = (
            float(market["best_ask"])
            if market.get("best_ask") is not None
            else float(market["yes_price"])
        )
        rows.append(
            {
                "airport": code,
                "target_date": target,
                "captured_at": captured_at,
                "timing": timing,
                "strategy": strategy,
                "market_id": str(market["market_id"]),
                "bucket_label": str(market["bucket_label"]),
                "model_bucket_c": int(model_bucket),
                "model_probability": float(probabilities[model_bucket]),
                "market_probability": float(market["yes_price"]),
                "buy_price": buy_price,
                "price_basis": (
                    "live best ask"
                    if market.get("best_ask") is not None
                    else "displayed market price"
                ),
                "day_phase": nowcast.day_status.phase,
            }
        )
    return _upsert_batch(
        session,
        StrategySnapshot,
        rows,
        lambda item: {
            "airport": item["airport"],
            "target_date": item["target_date"],
            "captured_at": item["captured_at"],
            "timing": item["timing"],
            "strategy": item["strategy"],
        },
        lambda item: {
            key: value
            for key, value in item.items()
            if key not in {"airport", "target_date", "captured_at", "timing", "strategy"}
        },
        f"{code}/strategy journal/{target}",
    )


def _record_shadow_evaluations(
    session,
    code: str,
    airport: dict,
    market_rows: list[dict],
    books: dict[str, dict],
    nowcast,
) -> tuple[int, int]:
    """Persist fee-, slippage- and depth-aware paper decisions."""
    if nowcast is None or not market_rows:
        return 0, 0
    captured_at = max(row["captured_at"] for row in market_rows)
    target = market_rows[0]["target_date"]
    conflict = detect_market_model_conflict(
        nowcast.probabilities,
        pd.DataFrame(market_rows),
    )
    raw_probabilities = getattr(nowcast, "stage_probabilities", {}).get(
        "Raw model mean",
        {},
    )
    if not raw_probabilities:
        print(
            f"WARN {code}/shadow watcher/{target}: raw probability lineage missing; "
            "no partial shadow row was stored."
        )
        return 0, 0
    rows = evaluate_shadow_markets(
        airport=code,
        target=target,
        captured_at=captured_at,
        timing=_signal_timing(captured_at, target, airport["timezone"]),
        probabilities=nowcast.probabilities,
        raw_probabilities=raw_probabilities,
        markets=pd.DataFrame(market_rows),
        books=books,
        forecast_confidence=nowcast.forecast_confidence,
        day_status=nowcast.day_status,
        metar_pending=nowcast.metar_pending,
        market_model_conflict=conflict.is_conflict,
        forecast_stale=nowcast.forecast_data_stale,
        recommendations_enabled=settings.edge_recommendations_enabled,
    )
    if any(
        row.get("raw_probability") is None
        or row.get("fair_probability") is None
        or row.get("forecast_snapshot_at") is None
        for row in rows
    ):
        print(
            f"WARN {code}/shadow watcher/{target}: incomplete probability lineage; "
            "no partial shadow row was stored."
        )
        return 0, 0
    shadow_count = _upsert_batch(
        session,
        ShadowEvaluation,
        rows,
        lambda item: {
            "market_id": item["market_id"],
            "captured_at": item["captured_at"],
        },
        lambda item: {
            key: value
            for key, value in item.items()
            if key not in {"market_id", "captured_at"}
        },
        f"{code}/shadow watcher/{target}",
    )
    basket = build_shadow_basket(rows, pd.DataFrame(market_rows))
    if basket is None:
        return shadow_count, 0
    basket_row = {
        "airport": code,
        "target_date": target,
        "event_slug": str(market_rows[0]["event_slug"]),
        "captured_at": captured_at,
        "timing": _signal_timing(captured_at, target, airport["timezone"]),
        "strategy": "Executable positive-edge basket",
        "market_ids_json": json.dumps(basket.market_ids, separators=(",", ":")),
        "bucket_labels_json": json.dumps(
            basket.bucket_labels,
            separators=(",", ":"),
        ),
        "market_count": len(basket.market_ids),
        "fair_probability": basket.fair_probability,
        "total_cost": basket.total_cost,
        "net_edge": basket.net_edge,
        "top_model_bucket": basket.top_model_bucket,
        "top_model_included": basket.top_model_included,
        "middle_bucket_excluded": basket.middle_bucket_excluded,
        "status": basket.status,
        "forecast_confidence": nowcast.forecast_confidence,
        "day_phase": nowcast.day_status.phase,
        "warnings_json": json.dumps(basket.warnings, separators=(",", ":")),
    }
    basket_count = _upsert_batch(
        session,
        BasketSnapshot,
        [basket_row],
        lambda item: {
            "airport": item["airport"],
            "target_date": item["target_date"],
            "captured_at": item["captured_at"],
            "strategy": item["strategy"],
        },
        lambda item: {
            key: value
            for key, value in item.items()
            if key not in {"airport", "target_date", "captured_at", "strategy"}
        },
        f"{code}/event basket/{target}",
    )
    return shadow_count, basket_count


def collect(airport_codes: list[str] | None = None, days: int = 3) -> dict[str, int]:
    init_db()
    counts = {
        "forecasts": 0,
        "hourly_forecasts": 0,
        "observations": 0,
        "taf_reports": 0,
        "market_prices": 0,
        "signals": 0,
        "strategy_snapshots": 0,
        "forecast_snapshots": 0,
        "forecast_variants": 0,
        "regime_memory_snapshots": 0,
        "actuals": 0,
        "provisional_actuals": 0,
        "restored_actuals": 0,
    }
    catalog = airports()
    selected_codes = airport_codes or list(catalog)
    if airport_codes is None:
        selected_codes = list(trading_airports())
    try:
        fetched_tafs = recent_tafs(selected_codes)
    except Exception as exc:
        print(f"WARN TAF: {exc}")
        fetched_tafs = []
    with Session() as session:
        for code in selected_codes:
            airport = catalog[code]
            batches = []
            for model in airport["models"]:
                try:
                    batches.extend(open_meteo_forecast(airport, model, days))
                except Exception as exc:
                    print(f"WARN {code}/{model}: {exc}")
                try:
                    hourly_rows = open_meteo_hourly(airport, model, days)
                except Exception as exc:
                    print(f"WARN {code}/{model} hourly: {exc}")
                else:
                    counts["hourly_forecasts"] += _upsert_batch(
                        session,
                        HourlyForecast,
                        hourly_rows,
                        lambda item: {
                            "airport": code,
                            "model": item["model"],
                            "run_at": item["run_at"],
                            "valid_at": item["valid_at"],
                        },
                        lambda item: {
                            "temp_c": item["temp_c"],
                            "dewpoint_c": item["dewpoint_c"],
                            "cloud_cover": item["cloud_cover"],
                            "wind_kph": item["wind_kph"],
                            "wind_direction": item["wind_direction"],
                            "radiation_wm2": item["radiation_wm2"],
                            "temp_850hpa_c": item["temp_850hpa_c"],
                        },
                        f"{code}/{model} hourly",
                    )
            try:
                batches.extend(meteoblue_forecast(airport))
            except Exception as exc:
                print(f"WARN {code}/meteoblue: {exc}")
            counts["forecasts"] += _upsert_batch(
                session,
                Forecast,
                batches,
                lambda item: {
                    "airport": code,
                    "model": item["model"],
                    "run_at": item["run_at"],
                    "target_date": item["target_date"],
                },
                lambda item: {
                    "max_temp_c": item["max_temp_c"],
                    "source": item["source"],
                    "horizon": item["horizon"],
                    "model_run_at": item.get("model_run_at"),
                    "available_at": item.get("available_at"),
                    "fetched_at": item.get("fetched_at", item["run_at"]),
                    "provenance_status": item.get("provenance_status"),
                },
                f"{code} daily forecasts",
            )
            try:
                metar_rows = recent_metars(code, hours=48)
            except Exception as exc:
                print(f"WARN {code}/METAR: {exc}")
            else:
                counts["observations"] += _upsert_batch(
                    session,
                    Observation,
                    metar_rows,
                    lambda item: {"airport": code, "observed_at": item["observed_at"]},
                    lambda item: {
                        key: value for key, value in item.items() if key != "observed_at"
                    },
                    f"{code}/METAR",
                )
                provisional_start = datetime.combine(
                    datetime.now(ZoneInfo(airport["timezone"])).date()
                    - timedelta(days=1),
                    datetime.min.time(),
                    ZoneInfo(airport["timezone"]),
                ).astimezone(timezone.utc)
                stored_metars = read_archive_live(
                    Observation,
                    session.connection(),
                    filters={"airport": code},
                    minimums={"observed_at": provisional_start},
                )
                provisional_rows = provisional_metar_actuals(
                    stored_metars.where(stored_metars.notna(), None).to_dict(
                        orient="records"
                    ),
                    airport,
                )
                stored_provisional = _store_actual_rows(
                    session,
                    code,
                    provisional_rows,
                    source="metar-provisional",
                    label=f"{code}/provisional METAR actuals",
                )
                counts["actuals"] += stored_provisional
                counts["provisional_actuals"] += stored_provisional
            airport_tafs = [row for row in fetched_tafs if row["airport"] == code]
            counts["taf_reports"] += _store_taf_rows(
                session, airport_tafs, catalog, f"{code}/TAF"
            )
            actual_end = date.today() - timedelta(days=6)
            actual_start = actual_end - timedelta(days=13)
            try:
                actual_rows = historical_actuals(airport, actual_start, actual_end)
            except Exception as exc:
                print(f"WARN {code}/recent actuals: {exc}")
            else:
                counts["actuals"] += _store_reanalysis_actuals(
                    session,
                    code,
                    actual_rows,
                )
            restored = _restore_stored_station_actuals(
                session,
                code,
                airport,
                as_of=datetime.now(timezone.utc),
            )
            counts["actuals"] += restored
            counts["restored_actuals"] += restored
            local_today = datetime.now(ZoneInfo(airport["timezone"])).date()
            for offset in range(-2, days):
                market_target = local_today + timedelta(days=offset)
                market_rows: list[dict] = []
                try:
                    market_rows = polymarket_prices(airport, market_target)
                except Exception as exc:
                    print(f"WARN {code}/Polymarket/{market_target}: {exc}")
                else:
                    counts["market_prices"] += _upsert_batch(
                        session,
                        MarketSnapshot,
                        market_rows,
                        lambda item: {
                            "market_id": item["market_id"],
                            "captured_at": item["captured_at"],
                        },
                        lambda item: {
                            "airport": code,
                            **{
                                key: value
                                for key, value in item.items()
                                if key not in {"market_id", "captured_at"}
                            },
                        },
                        f"{code}/Polymarket/{market_target}",
                    )
                if offset >= 0:
                    captured_at = (
                        max(row["captured_at"] for row in market_rows)
                        if market_rows
                        else datetime.now(timezone.utc)
                    )
                    try:
                        nowcast = _build_nowcast_from_session(
                            session,
                            code,
                            airport,
                            market_target,
                            captured_at,
                            market_rows,
                        )
                        counts["forecast_snapshots"] += _record_forecast_snapshot(
                            session,
                            code,
                            airport,
                            market_target,
                            captured_at,
                            nowcast,
                        )
                        counts["forecast_variants"] += _record_forecast_variants(
                            session,
                            code,
                            airport,
                            market_target,
                            captured_at,
                            nowcast,
                        )
                        counts["regime_memory_snapshots"] += (
                            _record_regime_memory_snapshot(
                                session,
                                code,
                                airport,
                                market_target,
                                captured_at,
                                nowcast,
                            )
                        )
                        if not market_rows:
                            continue
                        counts["signals"] += _record_signal_snapshots(
                            session, code, airport, market_rows, nowcast=nowcast
                        )
                        counts["strategy_snapshots"] += _record_strategy_snapshots(
                            session, code, airport, market_rows, nowcast
                        )
                    except Exception as exc:
                        print(f"WARN {code}/forecast journal/{market_target}: {exc}")
        session.commit()
    return counts


def collect_research_checkpoints(
    airport_codes: list[str] | None = None,
    *,
    now: datetime | None = None,
    window_minutes: int = 35,
    catchup_hours: int = 48,
    sync_universe: bool = True,
) -> dict[str, int]:
    """Collect the four configured Madrid decision checkpoints.

    A scheduled snapshot is written shortly after its target time but is rebuilt
    strictly from guidance available at or before that target. A later catch-up is
    explicitly reconstructed. Provider refresh happens in the consolidated live
    collector before this function is called.
    """
    init_db()
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    counts = {
        "universe_cities": 0,
        "mapped_cities": 0,
        "unmapped_cities": 0,
        "airports_due": 0,
        "forecasts": 0,
        "forecast_snapshots": 0,
        "forecast_variants": 0,
        "regime_memory_snapshots": 0,
        "actuals": 0,
        "checkpoints_reconstructed": 0,
        "checkpoints_missing_inputs": 0,
        "checkpoint_market_snapshots": 0,
        "checkpoint_market_missing": 0,
    }
    if sync_universe:
        try:
            universe_counts = sync_airport_universe()
        except Exception as exc:
            print(f"WARN Polymarket airport-universe sync: {exc}")
        else:
            counts["universe_cities"] = universe_counts["cities"]
            counts["mapped_cities"] = universe_counts["mapped"]
            counts["unmapped_cities"] = universe_counts["unmapped"]

    catalog = research_airports()
    with Session() as session:
        universe_rows = list(
            session.scalars(
                select(AirportMarketUniverse).where(
                    AirportMarketUniverse.active.is_(True),
                    AirportMarketUniverse.airport.is_not(None),
                )
            )
        )
        targets_by_airport: dict[str, set[date]] = {}
        for row in universe_rows:
            if row.airport and row.latest_target_date:
                targets_by_airport.setdefault(row.airport, set()).add(row.latest_target_date)
        selected_codes = airport_codes or sorted(
            {*targets_by_airport, *trading_airports()}
        )
        for code in selected_codes:
            if code not in catalog:
                continue
            airport = catalog[code]
            zone = ZoneInfo(airport["timezone"])
            local_now = current_time.astimezone(zone)
            targets = set(targets_by_airport.get(code) or set())
            targets.update(
                {
                    local_now.date() - timedelta(days=1),
                    local_now.date(),
                    local_now.date() + timedelta(days=1),
                }
            )
            if not targets and airport_codes:
                targets = {local_now.date(), local_now.date() + timedelta(days=1)}
            due: list[tuple[date, str, datetime]] = []
            for target in targets or set():
                for label, cutoff in _research_checkpoint_schedule(target, airport):
                    cutoff_utc = cutoff.astimezone(timezone.utc)
                    seconds_after_cutoff = (current_time - cutoff_utc).total_seconds()
                    in_capture_window = (
                        0
                        <= seconds_after_cutoff
                        <= max(1, catchup_hours) * 3600
                    )
                    if not in_capture_window:
                        continue
                    existing = session.scalar(
                        select(ForecastSnapshot.id).where(
                            ForecastSnapshot.airport == code,
                            ForecastSnapshot.target_date == target,
                            ForecastSnapshot.checkpoint_label == label,
                            ForecastSnapshot.checkpoint_at == cutoff_utc,
                        )
                    )
                    if existing is None:
                        due.append((target, label, cutoff_utc))
            if not due:
                continue
            counts["airports_due"] += 1
            batches: list[dict] = []
            refresh_due = False
            if refresh_due:
                models = airport.get(
                    "research_models",
                    ["ecmwf_ifs025", "gfs_global", "icon_global"],
                )
                with ThreadPoolExecutor(
                    max_workers=max(
                        1,
                        min(settings.collector_provider_workers, len(models)),
                    ),
                    thread_name_prefix="checkpoint-provider",
                ) as pool:
                    futures = {
                        pool.submit(
                            open_meteo_forecast,
                            airport,
                            model,
                            3,
                            attempts=1,
                            timeout=settings.collector_provider_timeout_seconds,
                            metadata_attempts=1,
                            metadata_timeout=min(
                                5.0, settings.collector_provider_timeout_seconds
                            ),
                        ): model
                        for model in models
                    }
                    for future in as_completed(futures):
                        model = futures[future]
                        try:
                            batches.extend(future.result())
                        except Exception as exc:
                            print(f"WARN {code}/{model} research checkpoint: {exc}")
            counts["forecasts"] += _upsert_batch(
                session,
                Forecast,
                batches,
                lambda item: {
                    "airport": code,
                    "model": item["model"],
                    "run_at": item["run_at"],
                    "target_date": item["target_date"],
                },
                lambda item: {
                    "max_temp_c": item["max_temp_c"],
                    "source": item["source"],
                    "horizon": item["horizon"],
                    "model_run_at": item.get("model_run_at"),
                    "available_at": item.get("available_at"),
                    "fetched_at": item.get("fetched_at", item["run_at"]),
                    "provenance_status": item.get("provenance_status"),
                },
                f"{code}/research checkpoint forecasts",
            )
            if refresh_due:
                try:
                    actual_end = current_time.date() - timedelta(days=6)
                    actual_rows = historical_actuals(
                        airport, actual_end - timedelta(days=13), actual_end
                    )
                except Exception as exc:
                    print(f"WARN {code}/research actuals: {exc}")
                else:
                    counts["actuals"] += _store_reanalysis_actuals(
                        session,
                        code,
                        actual_rows,
                    )
            session.flush()
            for target, label, cutoff in due:
                try:
                    metadata = _checkpoint_provenance(
                        session,
                        code=code,
                        target=target,
                        checkpoint_at=cutoff,
                        current_time=current_time,
                        label=label,
                        expected_models=[*airport.get("models", []), "meteoblue"],
                    )
                    if metadata["checkpoint_status"] == "unavailable":
                        counts["checkpoints_missing_inputs"] += 1
                        continue
                    market_rows: list[dict] = []
                    market_status = "not-requested-reconstructed"
                    market_snapshot_at = None
                    if not bool(metadata["checkpoint_reconstructed"]):
                        try:
                            market_rows = list(polymarket_prices(airport, target) or [])
                        except Exception as exc:
                            market_status = f"provider-error:{type(exc).__name__}"
                            counts["checkpoint_market_missing"] += 1
                            print(f"WARN {code}/{label} checkpoint market: {exc}")
                        else:
                            if market_rows:
                                counts["checkpoint_market_snapshots"] += _upsert_batch(
                                    session,
                                    MarketSnapshot,
                                    market_rows,
                                    lambda item: {
                                        "market_id": item["market_id"],
                                        "captured_at": item["captured_at"],
                                    },
                                    lambda item: {
                                        "airport": code,
                                        **{
                                            key: value
                                            for key, value in item.items()
                                            if key not in {"market_id", "captured_at"}
                                        },
                                    },
                                    f"{code}/{label} checkpoint market/{target}",
                                )
                                market_status = "stored-at-checkpoint"
                                market_snapshot_at = max(
                                    _as_utc(row.get("captured_at", cutoff))
                                    for row in market_rows
                                )
                            else:
                                market_status = "missing-at-checkpoint"
                                counts["checkpoint_market_missing"] += 1
                    metadata.update(
                        {
                            "market_snapshot_status": market_status,
                            "market_snapshot_at": market_snapshot_at,
                            "market_bucket_count": len(market_rows),
                        }
                    )
                    nowcast = _build_nowcast_from_session(
                        session,
                        code,
                        airport,
                        target,
                        cutoff,
                        market_rows,
                    )
                    if nowcast is None:
                        counts["checkpoints_missing_inputs"] += 1
                        continue
                    counts["forecast_snapshots"] += _record_forecast_snapshot(
                        session,
                        code,
                        airport,
                        target,
                        cutoff,
                        nowcast,
                        checkpoint_metadata=metadata,
                    )
                    counts["forecast_variants"] += _record_forecast_variants(
                        session,
                        code,
                        airport,
                        target,
                        cutoff,
                        nowcast,
                    )
                    counts["regime_memory_snapshots"] += (
                        _record_regime_memory_snapshot(
                            session,
                            code,
                            airport,
                            target,
                            cutoff,
                            nowcast,
                        )
                    )
                    counts["checkpoints_reconstructed"] += int(
                        bool(metadata["checkpoint_reconstructed"])
                    )
                except Exception as exc:
                    print(f"WARN {code}/research checkpoint journal/{target}: {exc}")
            session.commit()
    return counts


def collect_live_aviation(
    airport_code: str,
    *,
    include_taf: bool = False,
) -> dict[str, object]:
    """Lightweight dashboard poller: METAR every minute, TAF on a slower cadence."""
    init_db()
    catalog = airports()
    if airport_code not in catalog:
        raise KeyError(f"Unknown airport: {airport_code}")
    metar_rows = recent_metars(
        airport_code,
        hours=48,
        attempts=1,
        timeout=5,
    )
    taf_rows = recent_tafs([airport_code], attempts=1, timeout=5) if include_taf else []
    counts: dict[str, object] = {
        "observations": 0,
        "taf_reports": 0,
        "latest_metar": None,
        "latest_taf": None,
    }
    with Session() as session:
        counts["observations"] = _upsert_batch(
            session,
            Observation,
            metar_rows,
            lambda item: {"airport": airport_code, "observed_at": item["observed_at"]},
            lambda item: {key: value for key, value in item.items() if key != "observed_at"},
            f"{airport_code}/live METAR",
        )
        if taf_rows:
            counts["taf_reports"] = _store_taf_rows(
                session, taf_rows, catalog, f"{airport_code}/live TAF"
            )
        session.commit()
    if metar_rows:
        counts["latest_metar"] = max(row["observed_at"] for row in metar_rows)
    if taf_rows:
        counts["latest_taf"] = max(row["issue_time"] for row in taf_rows)
    return counts


def collect_live_trading_refresh(
    airport_code: str,
    target: date,
    *,
    maximum_workers: int = 12,
) -> dict[str, object]:
    """Refresh one dashboard information set without running historical collection.

    Provider calls run concurrently and use one bounded attempt. Database writes remain
    serialized in this process. With Neon, both the consolidated GitHub collector and a
    manual Streamlit refresh persist into the same durable store; provider-call rows and
    database constraints prevent duplicate checkpoint spending.
    """
    init_db()
    started = time.perf_counter()
    catalog = airports()
    if airport_code not in catalog:
        raise KeyError(f"Unknown airport: {airport_code}")
    airport = catalog[airport_code]
    refresh_time = datetime.now(timezone.utc)
    local_today = datetime.now(ZoneInfo(airport["timezone"])).date()
    days = max(3, min(16, (target - local_today).days + 1))
    models = list(dict.fromkeys(str(model) for model in airport.get("models", [])))

    with Session() as session:
        open_meteo_due = _source_refresh_due(
            session,
            airport_code=airport_code,
            source="open-meteo",
            target=target,
            as_of=refresh_time,
            maximum_age_minutes=settings.live_open_meteo_refresh_minutes,
        )
        meteoblue_due, meteoblue_policy = _meteoblue_poll_policy(
            session,
            airport_code=airport_code,
            airport=airport,
            as_of=refresh_time,
        )

    tasks: dict[str, tuple[Callable, tuple, dict]] = {}
    if open_meteo_due:
        for model in models:
            tasks[f"forecast/{model}"] = (
                open_meteo_forecast,
                (airport, model, days),
                {
                    "attempts": 1,
                    "timeout": 7,
                    "metadata_attempts": 1,
                    "metadata_timeout": 5,
                },
            )
            tasks[f"hourly/{model}"] = (
                open_meteo_hourly,
                (airport, model, days),
                {"attempts": 1, "timeout": 7},
            )
    if meteoblue_due:
        tasks["forecast/meteoblue"] = (
            meteoblue_forecast,
            (airport,),
            {"attempts": 1, "timeout": 7},
        )
    tasks.update(
        {
            "metar": (
                recent_metars,
                (airport_code,),
                {"hours": 48, "attempts": 1, "timeout": 5},
            ),
            "taf": (
                recent_tafs,
                ([airport_code],),
                {"attempts": 1, "timeout": 5},
            ),
            "polymarket": (
                polymarket_prices,
                (airport, target),
                {"attempts": 1, "timeout": 7},
            ),
        }
    )
    results: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    workers = max(1, min(int(maximum_workers), len(tasks)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="live-refresh") as pool:
        futures = {
            pool.submit(function, *args, **kwargs): label
            for label, (function, args, kwargs) in tasks.items()
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                payload = future.result()
            except Exception as exc:
                errors[label] = f"{type(exc).__name__}: {exc}"
            else:
                results[label] = list(payload or [])

    forecast_rows = [
        row
        for label, rows in results.items()
        if label.startswith("forecast/")
        for row in rows
    ]
    hourly_rows = [
        row
        for label, rows in results.items()
        if label.startswith("hourly/")
        for row in rows
    ]
    metar_rows = results.get("metar", [])
    taf_rows = results.get("taf", [])
    market_rows = results.get("polymarket", [])
    counts: dict[str, object] = {
        "forecasts": 0,
        "hourly_forecasts": 0,
        "observations": 0,
        "taf_reports": 0,
        "market_prices": 0,
        "models_configured": len(models) + int(bool(settings.meteoblue_api_key)),
        "models_requested": (len(models) if open_meteo_due else 0)
        + int(meteoblue_due),
        "models_reused": (0 if open_meteo_due else len(models))
        + int(bool(settings.meteoblue_api_key) and not meteoblue_due),
        "models_refreshed": 0,
        "meteoblue_status": meteoblue_policy,
        "errors": errors,
    }
    with Session() as session:
        counts["forecasts"] = _upsert_batch(
            session,
            Forecast,
            forecast_rows,
            lambda item: {
                "airport": airport_code,
                "model": item["model"],
                "run_at": item["run_at"],
                "target_date": item["target_date"],
            },
            lambda item: {
                "max_temp_c": item["max_temp_c"],
                "source": item["source"],
                "horizon": item["horizon"],
                "model_run_at": item.get("model_run_at"),
                "available_at": item.get("available_at"),
                "fetched_at": item.get("fetched_at", item["run_at"]),
                "provenance_status": item.get("provenance_status"),
            },
            f"{airport_code}/dashboard forecasts",
        )
        counts["hourly_forecasts"] = _upsert_batch(
            session,
            HourlyForecast,
            hourly_rows,
            lambda item: {
                "airport": airport_code,
                "model": item["model"],
                "run_at": item["run_at"],
                "valid_at": item["valid_at"],
            },
            lambda item: {
                "temp_c": item["temp_c"],
                "dewpoint_c": item["dewpoint_c"],
                "cloud_cover": item["cloud_cover"],
                "wind_kph": item["wind_kph"],
                "wind_direction": item["wind_direction"],
                "radiation_wm2": item["radiation_wm2"],
                "temp_850hpa_c": item["temp_850hpa_c"],
            },
            f"{airport_code}/dashboard hourly forecasts",
        )
        counts["observations"] = _upsert_batch(
            session,
            Observation,
            metar_rows,
            lambda item: {"airport": airport_code, "observed_at": item["observed_at"]},
            lambda item: {key: value for key, value in item.items() if key != "observed_at"},
            f"{airport_code}/dashboard METAR",
        )
        counts["taf_reports"] = _store_taf_rows(
            session,
            taf_rows,
            catalog,
            f"{airport_code}/dashboard TAF",
        )
        counts["market_prices"] = _upsert_batch(
            session,
            MarketSnapshot,
            market_rows,
            lambda item: {
                "market_id": item["market_id"],
                "captured_at": item["captured_at"],
            },
            lambda item: {
                "airport": airport_code,
                **{
                    key: value
                    for key, value in item.items()
                    if key not in {"market_id", "captured_at"}
                },
            },
            f"{airport_code}/dashboard Polymarket/{target}",
        )
        if meteoblue_due:
            meteoblue_error = errors.get("forecast/meteoblue")
            meteoblue_rows = results.get("forecast/meteoblue", [])
            _record_meteoblue_call(
                session,
                airport_code=airport_code,
                airport=airport,
                attempted_at=refresh_time,
                status="failed" if meteoblue_error else "success",
                rows_written=len(meteoblue_rows),
                reason=meteoblue_error,
            )
            session.add(
                CollectionCoverage(
                    run_id=(
                        f"streamlit-{airport_code}-"
                        f"{refresh_time.strftime('%Y%m%dT%H%M%S%f')}"
                    ),
                    airport=airport_code,
                    data_type="meteoblue",
                    status=(
                        "source_or_parser_failed"
                        if meteoblue_error
                        else "stored-local-refresh"
                    ),
                    scheduled_at=refresh_time,
                    latest_source_at=refresh_time if meteoblue_rows else None,
                    rows_read=len(meteoblue_rows),
                    rows_written=len(meteoblue_rows),
                    source_age_minutes=0.0 if meteoblue_rows else None,
                    attempts=1,
                    reason=meteoblue_error,
                )
            )
        session.commit()
    target_models = {
        str(row["model"])
        for row in forecast_rows
        if row.get("target_date") == target
    }
    counts["models_refreshed"] = len(target_models)
    counts["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    return counts


def collect_all_live_trading_refresh(
    targets: dict[str, date] | None = None,
) -> dict[str, object]:
    """Refresh every Trading Desk airport once while keeping SQLite writes serial."""
    started = time.perf_counter()
    catalog = trading_airports()
    results: dict[str, dict[str, object]] = {}
    for code, airport in catalog.items():
        target = (targets or {}).get(code)
        if target is None:
            target = datetime.now(ZoneInfo(airport["timezone"])).date()
        try:
            results[code] = collect_live_trading_refresh(code, target)
        except Exception as exc:
            results[code] = {
                "errors": {"refresh": f"{type(exc).__name__}: {exc}"},
                "elapsed_seconds": 0.0,
                "models_requested": 0,
                "models_refreshed": 0,
                "models_reused": 0,
                "observations": 0,
                "taf_reports": 0,
                "market_prices": 0,
            }
    failed = [code for code, result in results.items() if result.get("errors")]
    return {
        "airports": results,
        "successful_airports": len(results) - len(failed),
        "failed_airports": failed,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }


def _current_nowcast_from_session(
    session,
    *,
    code: str,
    airport: dict,
    target: date,
    captured_at: datetime,
    market_frame: pd.DataFrame,
):
    """Load exactly the same current-data window as Airport detail."""
    target_zone = ZoneInfo(airport["timezone"])
    target_start_utc = datetime(
        target.year, target.month, target.day, tzinfo=target_zone
    ).astimezone(timezone.utc)
    target_end_utc = target_start_utc + timedelta(days=1)
    connection = session.connection()
    forecasts = read_archive_live(
        Forecast,
        connection,
        filters={"airport": code},
        minimums={"target_date": target - timedelta(days=90)},
    )
    actuals = read_archive_live(DailyActual, connection, filters={"airport": code})
    observations = read_archive_live(Observation, connection, filters={"airport": code})
    hourly = read_archive_live(
        HourlyForecast,
        connection,
        filters={"airport": code},
        minimums={"valid_at": target_start_utc},
        maximums={"valid_at": target_end_utc},
    )
    tafs = read_archive_live(TafReport, connection, filters={"airport": code})
    snapshots = read_archive_live(
        ForecastSnapshot, connection, filters={"airport": code}
    )
    variants = read_archive_live(
        ForecastVariantSnapshot, connection, filters={"airport": code}
    )
    return build_current_live_nowcast(
        airport=airport,
        target=target,
        captured_at=captured_at,
        forecasts=forecasts,
        actuals=actuals,
        observations=observations,
        hourly=hourly,
        markets=market_frame,
        tafs=tafs,
        snapshots=snapshots,
        variants=variants,
    )


def live_trading_overview(
    targets: dict[str, date] | None = None,
    *,
    as_of: datetime | None = None,
) -> list[dict[str, object]]:
    """Build small, serialisable summaries for all six Trading Desk airports.

    Airport data are read and reduced one airport at a time. The caller retains
    only the compact dictionaries, not six copies of historical DataFrames.
    """
    captured_at = _as_utc(as_of or datetime.now(timezone.utc))
    catalog = trading_airports()
    rows: list[dict[str, object]] = []
    with Session() as session:
        for code, airport in catalog.items():
            target = (targets or {}).get(code)
            if target is None:
                target = captured_at.astimezone(ZoneInfo(airport["timezone"])).date()
            market_frame = read_archive_live(
                MarketSnapshot,
                session.connection(),
                filters={"airport": code, "target_date": target},
            )
            if not market_frame.empty:
                market_frame["captured_at"] = pd.to_datetime(
                    market_frame.captured_at, utc=True, errors="coerce"
                )
                market_frame = market_frame.sort_values("captured_at").drop_duplicates(
                    "market_id", keep="last"
                )
            nowcast = _current_nowcast_from_session(
                session,
                code=code,
                airport=airport,
                target=target,
                captured_at=captured_at,
                market_frame=market_frame,
            )
            if nowcast is None:
                rows.append(
                    {
                        "airport": code,
                        "name": airport["name"],
                        "target_date": target,
                        "calculated_at": captured_at,
                        "status": "No current forecast",
                    }
                )
                continue

            snapshots = read_archive_live(
                ForecastSnapshot,
                session.connection(),
                filters={"airport": code},
            )
            actuals = read_archive_live(
                DailyActual,
                session.connection(),
                filters={"airport": code},
            )
            history = forecast_ladder_history(
                snapshots,
                actuals,
                timezone_name=airport["timezone"],
                expected_checkpoint_models=list(
                    airport.get("research_models", airport.get("models", []))
                ),
            )
            reliability = forecast_ladder_oos_reliability(history)
            champion_reliability = (
                reliability[reliability.stage.str.endswith("· Champion")]
                .where(lambda frame: frame.notna(), None)
                .to_dict("records")
                if not reliability.empty
                else []
            )
            ranked_buckets = sorted(
                dict(nowcast.probabilities).items(),
                key=lambda item: item[1],
                reverse=True,
            )[:5]
            relevant_buckets = [
                {"bucket": int(bucket), "probability": float(probability)}
                for bucket, probability in sorted(ranked_buckets)
            ]
            def optional_float(value: object) -> float | None:
                return float(value) if value is not None and not pd.isna(value) else None

            rows.append(
                {
                    "airport": code,
                    "name": airport["name"],
                    "timezone": airport["timezone"],
                    "target_date": target,
                    "calculated_at": captured_at,
                    "status": nowcast.day_status.label,
                    "champion_c": float(nowcast.final_forecast_mean),
                    "latest_metar_c": optional_float(nowcast.current_observed_temp),
                    "latest_metar_at": nowcast.latest_observation_at,
                    "metar_max_c": optional_float(nowcast.observed_max),
                    "temperature_trend_c_per_hour": optional_float(nowcast.heating_rate),
                    "forecast_stale": bool(nowcast.forecast_data_stale),
                    "stale_models": list(nowcast.stale_models),
                    "forecast_chain": [
                        {"stage": "Raw ensemble", "value_c": float(nowcast.raw_model_mean)},
                        {"stage": "Bias-corrected", "value_c": float(nowcast.corrected.mean)},
                        {
                            "stage": "Live weather-adjusted",
                            "value_c": optional_float(nowcast.metar_conditioned_mean),
                        },
                        {"stage": "Champion", "value_c": float(nowcast.final_forecast_mean)},
                    ],
                    "reliability": champion_reliability,
                    "relevant_buckets": relevant_buckets,
                }
            )
    return rows


def collect_aviation_journal(
    airport_codes: list[str] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Collect METAR and the current TAF revision for every trading airport."""
    init_db()
    captured_at = _as_utc(now or datetime.now(timezone.utc))
    catalog = trading_airports()
    requested = [code for code in (airport_codes or list(catalog)) if code in catalog]
    coverage: list[dict[str, object]] = []
    tasks: dict[str, tuple[Callable, tuple, dict]] = {
        "taf": (
            recent_tafs,
            (requested,),
            {"attempts": 1, "timeout": settings.collector_provider_timeout_seconds},
        )
    }
    for code in requested:
        tasks[f"metar/{code}"] = (
            recent_metars,
            (code,),
            {
                "hours": 48,
                "attempts": 1,
                "timeout": min(6.0, settings.collector_provider_timeout_seconds),
            },
        )

    def fetch(
        function: Callable, args: tuple, kwargs: dict
    ) -> tuple[list[dict], str | None, float]:
        task_started = time.perf_counter()
        try:
            rows = list(function(*args, **kwargs) or [])
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}", time.perf_counter() - task_started
        return rows, None, time.perf_counter() - task_started

    fetched: dict[str, list[dict]] = {}
    provider_metrics: dict[str, dict[str, object]] = {}
    workers = max(1, min(settings.collector_provider_workers, len(tasks)))
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="collector-aviation",
    ) as pool:
        futures = {
            pool.submit(fetch, function, args, kwargs): label
            for label, (function, args, kwargs) in tasks.items()
        }
        for future in as_completed(futures):
            label = futures[future]
            rows, error, duration = future.result()
            fetched[label] = rows
            provider_metrics[label] = {
                "status": "failed" if error else "success",
                "duration_seconds": round(duration, 3),
                "rows_read": len(rows),
                "attempts": 1,
                "reason": error,
            }
    taf_rows = fetched.get("taf", [])
    taf_error = provider_metrics.get("taf", {}).get("reason")
    totals = {
        "observations_read": 0,
        "observations_written": 0,
        "taf_read": len(taf_rows),
        "taf_written": 0,
        "restored_actuals": 0,
    }
    with Session() as session:
        for code in requested:
            airport = catalog[code]
            previous_metar = session.scalar(
                select(func.max(Observation.observed_at)).where(Observation.airport == code)
            )
            metar_rows = fetched.get(f"metar/{code}", [])
            metar_metric = provider_metrics.get(f"metar/{code}", {})
            metar_error = metar_metric.get("reason")
            before_count = int(
                session.scalar(
                    select(func.count(Observation.id)).where(Observation.airport == code)
                )
                or 0
            )
            _upsert_batch(
                session,
                Observation,
                metar_rows,
                lambda item: {"airport": code, "observed_at": item["observed_at"]},
                lambda item: {key: value for key, value in item.items() if key != "observed_at"},
                f"{code}/collector METAR",
            )
            session.flush()
            after_count = int(
                session.scalar(
                    select(func.count(Observation.id)).where(Observation.airport == code)
                )
                or 0
            )
            metar_written = max(0, after_count - before_count)
            latest_metar = max(
                (row["observed_at"] for row in metar_rows), default=previous_metar
            )
            if metar_error:
                metar_status = "source_or_parser_failed"
                metar_reason = metar_error
            elif not metar_rows or (
                previous_metar is not None
                and latest_metar is not None
                and _as_utc(latest_metar) <= _as_utc(previous_metar)
            ):
                metar_status = "source_had_no_new_report"
                metar_reason = "Collector ran; the source returned no newer METAR."
            else:
                metar_status = "stored_pending_persistence"
                metar_reason = None
            totals["observations_read"] += len(metar_rows)
            totals["observations_written"] += metar_written
            # Repair completed station days on every collector pass. This must
            # not depend on the airport being inside a trading or post-peak
            # window: a deployment or delayed GitHub schedule can otherwise
            # miss the only time in which self-healing used to run.
            totals["restored_actuals"] += _restore_stored_station_actuals(
                session,
                code,
                airport,
                as_of=captured_at,
            )
            coverage.append(
                {
                    "airport": code,
                    "data_type": "metar",
                    "status": metar_status,
                    "latest_source_at": latest_metar,
                    "rows_read": len(metar_rows),
                    "rows_written": metar_written,
                    "source_age_minutes": (
                        max(0.0, (captured_at - _as_utc(latest_metar)).total_seconds() / 60)
                        if latest_metar is not None
                        else None
                    ),
                    "duration_seconds": metar_metric.get("duration_seconds"),
                    "attempts": metar_metric.get("attempts", 1),
                    "metrics": metar_metric,
                    "reason": metar_reason,
                }
            )

            airport_tafs = [row for row in taf_rows if row["airport"] == code]
            written = _store_taf_rows(
                session, airport_tafs, catalog, f"{code}/collector TAF"
            )
            totals["taf_written"] += written
            latest_taf = max(
                (row["issue_time"] for row in airport_tafs), default=None
            )
            coverage.append(
                {
                    "airport": code,
                    "data_type": "taf",
                    "status": (
                        "source_or_parser_failed"
                        if taf_error
                        else "stored_pending_persistence"
                        if written
                        else "source_had_no_new_report"
                    ),
                    "latest_source_at": latest_taf,
                    "rows_read": len(airport_tafs),
                    "rows_written": written,
                    "source_age_minutes": (
                        max(0.0, (captured_at - _as_utc(latest_taf)).total_seconds() / 60)
                        if latest_taf is not None
                        else None
                    ),
                    "duration_seconds": provider_metrics.get("taf", {}).get(
                        "duration_seconds"
                    ),
                    "attempts": provider_metrics.get("taf", {}).get("attempts", 1),
                    "metrics": provider_metrics.get("taf", {}),
                    "reason": taf_error,
                }
            )
        session.commit()
    return {
        "counts": totals,
        "coverage": coverage,
        "provider_metrics": provider_metrics,
    }


def backfill_taf_revision(
    airport_codes: list[str],
    issued_as_of: datetime,
) -> dict[str, object]:
    """Backfill a historically retained TAF without rewriting production causality."""
    init_db()
    catalog = airports()
    rows = historical_tafs_at(airport_codes, issued_as_of)
    with Session() as session:
        written = _store_taf_rows(session, rows, catalog, "historical TAF backfill")
        session.commit()
    return {
        "requested_airports": airport_codes,
        "issued_as_of": _as_utc(issued_as_of).isoformat(),
        "rows_read": len(rows),
        "rows_written": written,
        "status": "restored" if written else "already_present_or_unavailable",
    }


def in_critical_window(airport: dict, now: datetime) -> bool:
    """Return whether an airport is inside its configured local decision window."""
    configured = airport.get("critical_window_local")
    if not isinstance(configured, (list, tuple)) or len(configured) != 2:
        return False
    local = now.astimezone(ZoneInfo(airport["timezone"]))

    def minutes(value: object) -> int:
        hour, minute = str(value).split(":", maxsplit=1)
        return int(hour) * 60 + int(minute)

    start, end = (minutes(value) for value in configured)
    current = local.hour * 60 + local.minute
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def in_forecast_refresh_window(airport: dict, now: datetime) -> bool:
    """Poll current model data from D0 morning through the end of live trading."""
    local = _as_utc(now).astimezone(ZoneInfo(airport["timezone"]))
    configured = airport.get("forecast_refresh_window_local")
    if not isinstance(configured, (list, tuple)) or len(configured) != 2:
        critical = airport.get("critical_window_local", ["11:30", "18:00"])
        configured = ["06:00", critical[-1]]

    def minutes(value: object) -> int:
        hour, minute = str(value).split(":", maxsplit=1)
        return int(hour) * 60 + int(minute)

    start, end = (minutes(value) for value in configured)
    current = local.hour * 60 + local.minute
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def in_final_metar_collection_window(airport: dict, now: datetime) -> bool:
    """Continue METAR-only collection after trading through the evening report."""
    local = _as_utc(now).astimezone(ZoneInfo(airport["timezone"]))
    critical = airport.get("critical_window_local", ["11:30", "18:00"])
    configured_start = (
        critical[-1]
        if isinstance(critical, (list, tuple)) and len(critical) == 2
        else "18:00"
    )
    configured_end = airport.get("final_metar_collection_end_local", "21:35")

    def minutes(value: object) -> int:
        hour, minute = str(value).split(":", maxsplit=1)
        return int(hour) * 60 + int(minute)

    start = minutes(configured_start)
    end = minutes(configured_end)
    current = local.hour * 60 + local.minute
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def collect_live_decision_checkpoints(
    airport_codes: list[str] | None = None,
    *,
    now: datetime | None = None,
    aviation_already_collected: bool = False,
) -> dict[str, object]:
    """Persist live decisions and keep journaling every later METAR through evening."""
    init_db()
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    catalog = trading_airports()
    requested = airport_codes or list(catalog)
    due_codes = [
        code
        for code in requested
        if code in catalog and in_critical_window(catalog[code], captured_at)
    ]
    final_metar_codes = [
        code
        for code in requested
        if code in catalog
        and in_final_metar_collection_window(catalog[code], captured_at)
    ]
    metar_due_codes = list(dict.fromkeys([*due_codes, *final_metar_codes]))
    forecast_due_codes = [
        code
        for code in requested
        if code in catalog and in_forecast_refresh_window(catalog[code], captured_at)
    ]
    counts = {
        "airports_due": len(due_codes),
        "final_metar_airports_due": len(final_metar_codes),
        "forecast_airports_due": len(forecast_due_codes),
        "forecasts": 0,
        "hourly_forecasts": 0,
        "open_meteo_polls": 0,
        "meteoblue_polls": 0,
        "observations": 0,
        "taf_reports": 0,
        "market_prices": 0,
        "forecast_snapshots": 0,
        "forecast_variants": 0,
        "regime_memory_snapshots": 0,
        "signals": 0,
        "strategy_snapshots": 0,
        "shadow_evaluations": 0,
        "basket_snapshots": 0,
        "provisional_actuals": 0,
        "restored_actuals": 0,
        "post_peak_snapshots": 0,
        "post_peak_no_new_metar": 0,
        "provider_timings_seconds": {},
        "provider_status": {},
        "provider_coverage": [],
        "airport_timings_seconds": {},
    }
    if not metar_due_codes and not forecast_due_codes:
        return counts
    fetched_tafs = []
    if due_codes and not aviation_already_collected:
        try:
            fetched_tafs = recent_tafs(due_codes, attempts=2, timeout=10)
        except Exception as exc:
            print(f"WARN live-decision TAF batch: {exc}")
    with Session() as session:
        for code in forecast_due_codes:
            provider_counts = _store_current_provider_forecasts(
                session,
                airport_code=code,
                airport=catalog[code],
                as_of=captured_at,
            )
            for key in (
                "forecasts",
                "hourly_forecasts",
                "open_meteo_polls",
                "meteoblue_polls",
            ):
                counts[key] += int(provider_counts.get(key, 0))
            counts["provider_timings_seconds"][code] = provider_counts.get(
                "provider_timings_seconds", {}
            )
            counts["provider_status"][code] = provider_counts.get(
                "provider_status", {}
            )
            counts["provider_coverage"].extend(
                provider_counts.get("provider_coverage", [])
            )
            counts["airport_timings_seconds"][code] = provider_counts.get(
                "airport_elapsed_seconds", 0.0
            )
        session.commit()
        for code in metar_due_codes:
            airport = catalog[code]
            local_now = captured_at.astimezone(ZoneInfo(airport["timezone"]))
            provisional_start = datetime.combine(
                local_now.date() - timedelta(days=1),
                datetime.min.time(),
                ZoneInfo(airport["timezone"]),
            ).astimezone(timezone.utc)
            if aviation_already_collected:
                metar_frame = read_archive_live(
                    Observation,
                    session.connection(),
                    filters={"airport": code},
                    minimums={"observed_at": provisional_start},
                )
                metar_rows = metar_frame.where(metar_frame.notna(), None).to_dict(
                    orient="records"
                )
            else:
                try:
                    metar_rows = recent_metars(
                        code,
                        hours=48,
                        attempts=2,
                        timeout=10,
                    )
                except Exception as exc:
                    print(f"WARN {code}/live-decision METAR: {exc}")
                    metar_rows = []
                counts["observations"] += _upsert_batch(
                    session,
                    Observation,
                    metar_rows,
                    lambda item: {"airport": code, "observed_at": item["observed_at"]},
                    lambda item: {
                        key: value for key, value in item.items() if key != "observed_at"
                    },
                    f"{code}/live-decision METAR",
                )
                metar_frame = read_archive_live(
                    Observation,
                    session.connection(),
                    filters={"airport": code},
                    minimums={"observed_at": provisional_start},
                )
                metar_rows = metar_frame.where(metar_frame.notna(), None).to_dict(
                    orient="records"
                )
            counts["restored_actuals"] += _restore_stored_station_actuals(
                session,
                code,
                airport,
                as_of=captured_at,
            )
            provisional_rows = provisional_metar_actuals(
                metar_rows,
                airport,
                as_of=captured_at,
                include_current_day=code in final_metar_codes,
            )
            counts["provisional_actuals"] += _store_actual_rows(
                session,
                code,
                provisional_rows,
                source="metar-provisional",
                label=f"{code}/live provisional METAR actuals",
            )
            local_target = captured_at.astimezone(ZoneInfo(airport["timezone"])).date()
            if code not in due_codes:
                latest_report_at = max(
                    (row.get("observed_at") for row in metar_rows),
                    default=None,
                )
                latest_journaled_at = session.scalar(
                    select(func.max(ForecastSnapshot.latest_metar_at)).where(
                        ForecastSnapshot.airport == code,
                        ForecastSnapshot.target_date == local_target,
                    )
                )
                if latest_report_at is None or (
                    latest_journaled_at is not None
                    and _as_utc(latest_journaled_at) >= _as_utc(latest_report_at)
                ):
                    counts["post_peak_no_new_metar"] += 1
                    session.commit()
                    continue
            market_rows: list[dict] = []
            if code in due_codes:
                airport_tafs = [row for row in fetched_tafs if row["airport"] == code]
                counts["taf_reports"] += _store_taf_rows(
                    session,
                    airport_tafs,
                    catalog,
                    f"{code}/live-decision TAF",
                )
                market_started = time.perf_counter()
                try:
                    market_rows = polymarket_prices(airport, local_target)
                except Exception as exc:
                    print(f"WARN {code}/live-decision Polymarket: {exc}")
                    market_error = f"{type(exc).__name__}: {exc}"
                else:
                    market_error = None
                market_duration = round(time.perf_counter() - market_started, 3)
                market_rows_written = _upsert_batch(
                    session,
                    MarketSnapshot,
                    market_rows,
                    lambda item: {
                        "market_id": item["market_id"],
                        "captured_at": item["captured_at"],
                    },
                    lambda item: {
                        "airport": code,
                        **{
                            key: value
                            for key, value in item.items()
                            if key not in {"market_id", "captured_at"}
                        },
                    },
                    f"{code}/live-decision Polymarket",
                )
                counts["market_prices"] += market_rows_written
                counts["provider_timings_seconds"].setdefault(code, {})[
                    "polymarket/prices"
                ] = market_duration
                counts["provider_status"].setdefault(code, {})[
                    "polymarket/prices"
                ] = "failed" if market_error else "success"
                counts["provider_coverage"].append(
                    {
                        "airport": code,
                        "data_type": "polymarket",
                        "status": (
                            "source_or_parser_failed"
                            if market_error
                            else "stored_pending_persistence"
                            if market_rows
                            else "source_had_no_new_report"
                        ),
                        "latest_source_at": max(
                            (row["captured_at"] for row in market_rows),
                            default=None,
                        ),
                        "rows_read": len(market_rows),
                        "rows_written": market_rows_written,
                        "source_age_minutes": 0.0 if market_rows else None,
                        "duration_seconds": market_duration,
                        "attempts": 1,
                        "metrics": {},
                        "reason": market_error,
                    }
                )
            token_ids = [
                str(row["token_id"])
                for row in market_rows
                if row.get("token_id") and not row.get("closed")
            ]
            books = {}
            if token_ids:
                clob_started = time.perf_counter()
                try:
                    books = polymarket_order_books(token_ids)
                except Exception as exc:
                    print(f"WARN {code}/live-decision CLOB books: {exc}")
                    clob_error = f"{type(exc).__name__}: {exc}"
                else:
                    clob_error = None
                clob_duration = round(time.perf_counter() - clob_started, 3)
                counts["provider_timings_seconds"].setdefault(code, {})[
                    "polymarket/clob"
                ] = clob_duration
                counts["provider_status"].setdefault(code, {})[
                    "polymarket/clob"
                ] = "failed" if clob_error else "success"
                counts["provider_coverage"].append(
                    {
                        "airport": code,
                        "data_type": "polymarket-clob",
                        "status": (
                            "source_or_parser_failed"
                            if clob_error
                            else "stored_pending_persistence"
                            if books
                            else "source_had_no_new_report"
                        ),
                        "latest_source_at": max(
                            (book.get("fetched_at") for book in books.values()),
                            default=None,
                        ),
                        "rows_read": len(books),
                        "rows_written": 0,
                        "source_age_minutes": 0.0 if books else None,
                        "duration_seconds": clob_duration,
                        "attempts": 1,
                        "metrics": {"requested_tokens": len(token_ids)},
                        "reason": clob_error,
                    }
                )
            snapshot_at = (
                max(row["captured_at"] for row in market_rows) if market_rows else captured_at
            )
            try:
                nowcast = _build_nowcast_from_session(
                    session,
                    code,
                    airport,
                    local_target,
                    snapshot_at,
                    market_rows,
                )
                counts["forecast_snapshots"] += _record_forecast_snapshot(
                    session,
                    code,
                    airport,
                    local_target,
                    snapshot_at,
                    nowcast,
                )
                counts["forecast_variants"] += _record_forecast_variants(
                    session,
                    code,
                    airport,
                    local_target,
                    snapshot_at,
                    nowcast,
                )
                counts["regime_memory_snapshots"] += _record_regime_memory_snapshot(
                    session,
                    code,
                    airport,
                    local_target,
                    snapshot_at,
                    nowcast,
                )
                counts["post_peak_snapshots"] += int(code not in due_codes)
                if market_rows:
                    counts["signals"] += _record_signal_snapshots(
                        session,
                        code,
                        airport,
                        market_rows,
                        nowcast=nowcast,
                    )
                    counts["strategy_snapshots"] += _record_strategy_snapshots(
                        session,
                        code,
                        airport,
                        market_rows,
                        nowcast,
                    )
                    shadow_count, basket_count = _record_shadow_evaluations(
                        session,
                        code,
                        airport,
                        market_rows,
                        books,
                        nowcast,
                    )
                    counts["shadow_evaluations"] += shadow_count
                    counts["basket_snapshots"] += basket_count
            except Exception as exc:
                print(f"WARN {code}/live-decision snapshot: {exc}")
            session.commit()
    return counts


def backfill(days: int = 365, airport_codes: list[str] | None = None) -> dict[str, int]:
    init_db()
    # Reanalysis products can arrive several days late. A six-day safety margin
    # prevents a whole first-time backfill from failing on incomplete recent data.
    end = date.today() - timedelta(days=6)
    start = end - timedelta(days=days - 1)
    counts = {"forecasts": 0, "actuals": 0}
    catalog = research_airports()
    with Session() as session:
        for code in airport_codes or list(catalog):
            airport = catalog[code]
            try:
                actual_rows = historical_actuals(airport, start, end)
                airport_actuals = _store_reanalysis_actuals(
                    session,
                    code,
                    actual_rows,
                )
                counts["actuals"] += airport_actuals
                print(f"OK {code}/actuals: {airport_actuals} days")
            except Exception as exc:
                print(f"WARN {code}/historical actuals: {exc}")
            for model in airport["models"]:
                try:
                    forecast_rows = previous_run_d1(airport, model, start, end)
                    model_rows = _upsert_batch(
                        session,
                        Forecast,
                        forecast_rows,
                        lambda item: {
                            "airport": code,
                            "model": model,
                            "run_at": item["run_at"],
                            "target_date": item["target_date"],
                        },
                        lambda item: {
                            "max_temp_c": item["max_temp_c"],
                            "source": item["source"],
                            "horizon": item["horizon"],
                            "model_run_at": item.get("model_run_at"),
                            "available_at": item.get("available_at"),
                            "fetched_at": item.get("fetched_at"),
                            "provenance_status": item.get("provenance_status"),
                        },
                        f"{code}/{model} backfill",
                    )
                    counts["forecasts"] += model_rows
                    print(f"OK {code}/{model}: {model_rows} days")
                except Exception as exc:
                    print(f"WARN {code}/{model} backfill: {exc}")
                # Keep the free data endpoint below burst-rate limits.
                time.sleep(1)
        session.commit()
    return counts


def backfill_market_history(
    days: int = 30,
    airport_codes: list[str] | None = None,
) -> dict[str, int]:
    """Sample historical Polymarket prices at fixed D-1 and D0 decision times."""
    init_db()
    catalog = airports()
    selected_codes = airport_codes or list(trading_airports())
    counts = {"market_prices": 0, "airport_days": 0}
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=max(1, days) - 1)
    with Session() as session:
        for code in selected_codes:
            airport = catalog[code]
            zone = ZoneInfo(airport["timezone"])
            for offset in range((end - start).days + 1):
                target = start + timedelta(days=offset)
                sample_times = [
                    datetime(
                        target.year,
                        target.month,
                        target.day,
                        20,
                        tzinfo=zone,
                    ).astimezone(timezone.utc)
                    - timedelta(days=1),
                    datetime(
                        target.year,
                        target.month,
                        target.day,
                        10,
                        tzinfo=zone,
                    ).astimezone(timezone.utc),
                ]
                try:
                    rows = polymarket_historical_prices(airport, target, sample_times)
                except Exception as exc:
                    print(f"WARN {code}/historical market/{target}: {exc}")
                    continue
                stored = _upsert_batch(
                    session,
                    MarketSnapshot,
                    rows,
                    lambda item: {
                        "market_id": item["market_id"],
                        "captured_at": item["captured_at"],
                    },
                    lambda item: {
                        "airport": code,
                        **{
                            key: value
                            for key, value in item.items()
                            if key not in {"market_id", "captured_at"}
                        },
                    },
                    f"{code}/historical market/{target}",
                )
                counts["market_prices"] += stored
                counts["airport_days"] += int(stored > 0)
                session.commit()
                time.sleep(0.25)
    return counts
