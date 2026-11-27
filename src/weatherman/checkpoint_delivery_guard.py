"""Operational guard for Madrid fixed-checkpoint delivery.

This module deliberately lives outside the protected forecast baseline.  It does
not calculate or modify a forecast.  It schedules one pre-cutoff model refresh,
checks whether the protected collector wrote each currently due checkpoint and
adds that delivery result to the existing CollectionRun journal.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .collector import run_collector
from .db import CollectionRun, ForecastSnapshot, Session
from .service import _research_checkpoint_schedule
from .settings import research_airports


CAPTURE_WINDOW_MINUTES = 35
MADRID_TIMEZONE = "Europe/Madrid"


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _environment_time(name: str) -> datetime | None:
    value = os.getenv(name, "").strip()
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00"))) if value else None


def scheduler_reference(now: datetime) -> datetime:
    """Use the intended slot where supplied, never a later reconstructed time."""
    return (
        _environment_time("WEATHERMAN_EXPECTED_SLOT_AT")
        or _environment_time("WEATHERMAN_EVENT_CREATED_AT")
        or _utc(now)
    )


def guarded_collection_mode(requested_mode: str, scheduled_at: datetime) -> str:
    """Add a Madrid 08:xx pre-cutoff refresh without changing the collector."""
    normalized = str(requested_mode or "auto").strip().casefold()
    if normalized != "auto":
        return normalized
    local = _utc(scheduled_at).astimezone(ZoneInfo(MADRID_TIMEZONE))
    if local.hour == 8 and 0 <= local.minute <= 20:
        return "fixed"
    return "auto"


def _provider_diagnostics(result: dict[str, object], airport: str) -> list[dict[str, object]]:
    live = result.get("live_decisions")
    coverage = live.get("provider_coverage", []) if isinstance(live, dict) else []
    return [
        {
            "provider": item.get("data_type"),
            "status": item.get("status"),
            "rows_read": item.get("rows_read"),
            "rows_written": item.get("rows_written"),
            "reason": item.get("reason"),
        }
        for item in coverage
        if isinstance(item, dict)
        and item.get("airport") == airport
        and item.get("data_type") in {"open-meteo", "meteoblue"}
    ]


def _due_specs(
    airport_codes: list[str],
    *,
    now: datetime,
) -> list[tuple[str, date, str, datetime]]:
    catalog = research_airports()
    current = _utc(now)
    due: list[tuple[str, date, str, datetime]] = []
    for code in airport_codes:
        airport = catalog.get(code)
        if airport is None:
            continue
        local_date = current.astimezone(ZoneInfo(airport["timezone"])).date()
        for target in (local_date, local_date + timedelta(days=1)):
            for label, cutoff in _research_checkpoint_schedule(target, airport):
                cutoff_utc = cutoff.astimezone(timezone.utc)
                delay = (current - cutoff_utc).total_seconds()
                if 0 <= delay <= CAPTURE_WINDOW_MINUTES * 60:
                    due.append((code, target, label, cutoff_utc))
    return due


def checkpoint_delivery_attempts(
    airport_codes: list[str],
    *,
    now: datetime,
    collector_result: dict[str, object],
) -> list[dict[str, object]]:
    """Classify every checkpoint currently due after the protected collector ran."""
    attempts: list[dict[str, object]] = []
    with Session() as session:
        for code, target, label, cutoff in _due_specs(airport_codes, now=now):
            row = session.scalar(
                select(ForecastSnapshot).where(
                    ForecastSnapshot.airport == code,
                    ForecastSnapshot.target_date == target,
                    ForecastSnapshot.checkpoint_label == label,
                    ForecastSnapshot.checkpoint_at == cutoff,
                )
            )
            provider = _provider_diagnostics(collector_result, code)
            if row is None:
                checkpoint_counts = collector_result.get("checkpoints")
                missing_inputs = (
                    int(checkpoint_counts.get("checkpoints_missing_inputs", 0))
                    if isinstance(checkpoint_counts, dict)
                    else 0
                )
                attempts.append(
                    {
                        "airport": code,
                        "target_date": target.isoformat(),
                        "checkpoint_label": label,
                        "checkpoint_at": cutoff.isoformat(),
                        "checkpoint_recorded_at": _utc(now).isoformat(),
                        "status": "missing",
                        "checkpoint_status": "unavailable",
                        "checkpoint_reconstructed": False,
                        "reason": (
                            "no_forecast_inputs_available_at_or_before_cutoff"
                            if missing_inputs
                            else "no_forecast_snapshot_written_for_due_cutoff"
                        ),
                        "provider_diagnostics": provider,
                    }
                )
                continue
            attempts.append(
                {
                    "airport": code,
                    "target_date": target.isoformat(),
                    "checkpoint_label": label,
                    "checkpoint_at": cutoff.isoformat(),
                    "checkpoint_recorded_at": (
                        _utc(row.checkpoint_recorded_at).isoformat()
                        if row.checkpoint_recorded_at is not None
                        else None
                    ),
                    "status": (
                        "reconstructed" if row.checkpoint_reconstructed else "written"
                    ),
                    "checkpoint_status": row.checkpoint_status,
                    "checkpoint_reconstructed": bool(row.checkpoint_reconstructed),
                    "evidence_class": row.evidence_class,
                    "expected_model_count": row.expected_model_count,
                    "available_model_count": row.available_model_count,
                    "source_model_count": row.source_model_count,
                    "fresh_model_count": row.fresh_model_count,
                    "used_model_count": row.used_model_count,
                    "forecast_available_at": (
                        _utc(row.forecast_available_at).isoformat()
                        if row.forecast_available_at is not None
                        else None
                    ),
                    "forecast_fetched_at": (
                        _utc(row.forecast_fetched_at).isoformat()
                        if row.forecast_fetched_at is not None
                        else None
                    ),
                    "provider_diagnostics": provider,
                }
            )
    return attempts


def persist_delivery_audit(
    run_id: str,
    attempts: list[dict[str, object]],
) -> str:
    """Attach delivery evidence to the existing run without a schema migration."""
    has_gap = any(item["status"] in {"missing", "failed"} for item in attempts)
    with Session() as session:
        row = session.scalar(select(CollectionRun).where(CollectionRun.run_id == run_id))
        if row is None:
            return "run-not-found"
        try:
            payload = json.loads(row.rows_written_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        collector = payload.setdefault("collector", {})
        checkpoints = collector.setdefault("checkpoints", {})
        checkpoints["checkpoint_attempts"] = attempts
        checkpoints["checkpoint_delivery_status"] = "partial" if has_gap else "complete"
        row.rows_written_json = json.dumps(
            payload, default=str, separators=(",", ":"), sort_keys=True
        )
        if has_gap:
            row.overall_status = "partial"
            row.error_reason = "one or more due forecast checkpoints were not written"
        session.commit()
    return "partial" if has_gap else "complete"


def run_guarded_madrid_collector(
    *,
    requested_mode: str = "auto",
    now: datetime | None = None,
) -> dict[str, object]:
    started = _utc(now or datetime.now(timezone.utc))
    mode = guarded_collection_mode(requested_mode, scheduler_reference(started))
    result = run_collector(["LEMD"], now=started, collection_mode=mode)
    if result.get("status") == "duplicate-skipped":
        return {**result, "checkpoint_delivery_audit": "duplicate-skipped"}
    attempts = checkpoint_delivery_attempts(
        ["LEMD"], now=started, collector_result=result
    )
    audit_status = persist_delivery_audit(str(result["run_id"]), attempts)
    final_status = "partial" if audit_status == "partial" else str(result["status"])
    return {
        **result,
        "status": final_status,
        "effective_collection_mode": mode,
        "checkpoint_delivery_audit": {
            "status": audit_status,
            "attempts": attempts,
            "precutoff_morning_refresh": mode == "fixed"
            and scheduler_reference(started)
            .astimezone(ZoneInfo(MADRID_TIMEZONE))
            .hour
            == 8,
        },
    }
