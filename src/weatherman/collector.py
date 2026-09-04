from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select, text

from . import __version__
from .db import (
    CollectionCoverage,
    CollectionRun,
    Forecast,
    ForecastSnapshot,
    MarketSnapshot,
    Observation,
    Session,
    TafReport,
    init_db,
    refresh_database_connections,
)
from .history import (
    DEFAULT_ARCHIVE_DIRECTORY,
    read_archive_live,
    validate_history_archive,
)
from .maintenance import (
    DATABASE_WARNING_BYTES,
    configured_sqlite_path,
)
from .service import (
    backfill_market_history,
    backfill_taf_revision,
    collect,
    collect_aviation_journal,
    collect_live_decision_checkpoints,
    collect_research_checkpoints,
)
from .settings import ROOT, trading_airports


COLLECTOR_INTERVAL_MINUTES = 30
COLLECTOR_SLOT_OFFSET_MINUTES = 7
ACTIVE_UTC_HOURS = tuple(range(5, 21))
SAFETY_UTC_HOURS = (0, 1, 2, 3, 4, 21, 22, 23)
LATEST_COVERAGE_REPORT = ROOT / "data" / "collection" / "coverage-latest.json"
STAGE1_RECOVERY_REPORT = (
    ROOT / "data" / "collection" / "recovery-2026-08-10-11.json"
)


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _environment_time(name: str) -> datetime | None:
    configured = os.getenv(name, "").strip()
    if not configured:
        return None
    return _utc(datetime.fromisoformat(configured.replace("Z", "+00:00")))


def _expected_slot_at(event_created_at: datetime) -> datetime:
    """Infer the most recent slot from the adaptive Madrid cron.

    GitHub does not expose the intended cron slot in the run payload. This value is
    therefore explicitly an inference; event creation and queue start remain stored
    separately so it cannot be mistaken for an observed scheduler timestamp.
    """
    explicit = _environment_time("WEATHERMAN_EXPECTED_SLOT_AT")
    if explicit is not None:
        return explicit
    created = _utc(event_created_at)
    candidates = [
        slot
        for day in (created.date() - timedelta(days=1), created.date())
        for slot in _declared_slots_for_day(day)
        if slot <= created
    ]
    return max(candidates)


def _declared_slots_for_day(day) -> tuple[datetime, ...]:
    slots: list[datetime] = []
    for hour in SAFETY_UTC_HOURS:
        slots.append(
            datetime(
                day.year,
                day.month,
                day.day,
                hour,
                COLLECTOR_SLOT_OFFSET_MINUTES,
                tzinfo=timezone.utc,
            )
        )
    for hour in ACTIVE_UTC_HOURS:
        for minute in range(
            COLLECTOR_SLOT_OFFSET_MINUTES,
            60,
            COLLECTOR_INTERVAL_MINUTES,
        ):
            slots.append(
                datetime(
                    day.year,
                    day.month,
                    day.day,
                    hour,
                    minute,
                    tzinfo=timezone.utc,
                )
            )
    return tuple(sorted(slots))


def _declared_slots_between(start: datetime, end: datetime) -> tuple[datetime, ...]:
    current_day = _utc(start).date()
    end_day = _utc(end).date()
    slots: list[datetime] = []
    while current_day <= end_day:
        slots.extend(
            slot
            for slot in _declared_slots_for_day(current_day)
            if _utc(start) <= slot <= _utc(end)
        )
        current_day += timedelta(days=1)
    return tuple(slots)


def _scheduler_times(
    started_at: datetime,
    *,
    trigger: str,
) -> tuple[datetime, datetime, datetime]:
    event_created = (
        _environment_time("WEATHERMAN_EVENT_CREATED_AT")
        or _environment_time("WEATHERMAN_SCHEDULED_AT")
        or started_at
    )
    queue_started = _environment_time("WEATHERMAN_RUN_STARTED_AT") or event_created
    expected = (
        _expected_slot_at(event_created)
        if trigger == "schedule"
        else _environment_time("WEATHERMAN_EXPECTED_SLOT_AT") or event_created
    )
    return expected, event_created, queue_started


def _scheduled_at(started_at: datetime) -> datetime:
    """Backward-compatible helper used by older callers and tests."""
    return _scheduler_times(started_at, trigger="schedule")[0]


def _collection_mode(
    requested_mode: str | None,
    *,
    scheduled_at: datetime,
    trigger: str,
) -> str:
    """Choose a lightweight aviation pass or one of the auditable full runs."""
    configured = (
        requested_mode
        or os.getenv("WEATHERMAN_COLLECTION_MODE", "")
        or "auto"
    ).strip().casefold()
    if configured in {"aviation", "fixed", "closeout"}:
        return configured
    if configured != "auto":
        raise ValueError(f"Unknown collection mode: {configured}")
    if "closeout" in trigger.casefold():
        return "closeout"
    madrid_local = _utc(scheduled_at).astimezone(ZoneInfo("Europe/Madrid"))
    if madrid_local.hour in {9, 12, 16, 20} and 0 <= madrid_local.minute <= 20:
        return "fixed"
    return "aviation"


def _run_id() -> str:
    github_id = os.getenv("GITHUB_RUN_ID", "").strip()
    attempt = os.getenv("GITHUB_RUN_ATTEMPT", "1").strip()
    return f"github-{github_id}-{attempt}" if github_id else f"local-{uuid.uuid4()}"


def _json(payload: object) -> str:
    return json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _start_run(
    run_id: str,
    *,
    scheduled_at: datetime,
    event_created_at: datetime | None = None,
    queue_started_at: datetime | None = None,
    started_at: datetime,
    trigger: str,
    airport_codes: list[str],
) -> str | None:
    with Session() as session:
        airport_scope = _json(sorted(airport_codes))
        slot_lock_key = f"weatherman-collector:{scheduled_at.isoformat()}:{airport_scope}"
        if session.get_bind().dialect.name == "postgresql":
            # Serialize Cloudflare and GitHub fallback invocations for the same
            # intended slot without requiring a production schema migration.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": slot_lock_key},
            )

        # If a prior pending row is present in the database checked out by this
        # run, its Git commit necessarily reached main successfully.
        prior = list(
            session.scalars(
                select(CollectionRun).where(
                    CollectionRun.persistence_status == "pending_commit",
                    CollectionRun.ended_at.is_not(None),
                )
            )
        )
        for row in prior:
            row.persistence_status = "persisted"
            prior_coverage = list(
                session.scalars(
                    select(CollectionCoverage).where(
                        CollectionCoverage.run_id == row.run_id,
                        CollectionCoverage.status == "stored_pending_persistence",
                    )
                )
            )
            for coverage in prior_coverage:
                coverage.status = "stored_persisted"
        existing = session.scalar(
            select(CollectionRun)
            .where(
                CollectionRun.scheduled_at == scheduled_at,
                CollectionRun.airports_json == airport_scope,
                CollectionRun.overall_status.in_(["running", "success"]),
            )
            .order_by(CollectionRun.started_at.desc())
        )
        if existing is not None and (
            existing.overall_status == "success"
            or _utc(existing.started_at) >= started_at - timedelta(minutes=30)
        ):
            session.commit()
            return existing.run_id
        session.add(
            CollectionRun(
                run_id=run_id,
                scheduled_at=scheduled_at,
                event_created_at=event_created_at,
                queue_started_at=queue_started_at,
                started_at=started_at,
                collector_version=__version__,
                trigger=trigger,
                overall_status="running",
                scheduler_drift_seconds=max(
                    0.0, (started_at - scheduled_at).total_seconds()
                ),
                trigger_delay_seconds=(
                    max(0.0, (event_created_at - scheduled_at).total_seconds())
                    if event_created_at is not None
                    else None
                ),
                queue_delay_seconds=(
                    max(0.0, (queue_started_at - event_created_at).total_seconds())
                    if event_created_at is not None and queue_started_at is not None
                    else None
                ),
                airports_json=airport_scope,
                source_status_json="{}",
                rows_read_json="{}",
                rows_written_json="{}",
                source_age_json="{}",
                persistence_status="pending_commit",
            )
        )
        session.commit()
    return None


def _finish_run(
    run_id: str,
    *,
    status: str,
    results: dict[str, object],
    coverage: list[dict[str, object]],
    error_reason: str | None = None,
) -> None:
    ended_at = datetime.now(timezone.utc)
    with Session() as session:
        row = session.scalar(select(CollectionRun).where(CollectionRun.run_id == run_id))
        if row is None:
            return
        row.ended_at = ended_at
        row.execution_seconds = max(
            0.0, (ended_at - _utc(row.started_at)).total_seconds()
        )
        row.overall_status = status
        row.persistence_status = "persisted"
        row.error_reason = error_reason
        row.source_status_json = _json(
            {item["airport"] + "/" + item["data_type"]: item["status"] for item in coverage}
        )
        row.rows_read_json = _json(
            {item["airport"] + "/" + item["data_type"]: item["rows_read"] for item in coverage}
        )
        row.rows_written_json = _json(
            {
                **{
                    item["airport"] + "/" + item["data_type"]: item["rows_written"]
                    for item in coverage
                },
                "collector": results,
            }
        )
        row.source_age_json = _json(
            {
                item["airport"] + "/" + item["data_type"]: item["source_age_minutes"]
                for item in coverage
            }
        )
        row.provider_metrics_json = _json(
            {
                "aviation": results.get("aviation_provider_metrics", {}),
                "live": (
                    results.get("live_decisions", {}).get(
                        "provider_timings_seconds", {}
                    )
                    if isinstance(results.get("live_decisions"), dict)
                    else {}
                ),
                "status": (
                    results.get("live_decisions", {}).get("provider_status", {})
                    if isinstance(results.get("live_decisions"), dict)
                    else {}
                ),
            }
        )
        row.airport_metrics_json = _json(
            results.get("live_decisions", {}).get("airport_timings_seconds", {})
            if isinstance(results.get("live_decisions"), dict)
            else {}
        )
        for item in coverage:
            session.add(
                CollectionCoverage(
                    run_id=run_id,
                    airport=str(item["airport"]),
                    data_type=str(item["data_type"]),
                    status=str(item["status"]),
                    scheduled_at=row.scheduled_at,
                    latest_source_at=item.get("latest_source_at"),
                    rows_read=int(item.get("rows_read", 0)),
                    rows_written=int(item.get("rows_written", 0)),
                    source_age_minutes=item.get("source_age_minutes"),
                    duration_seconds=item.get("duration_seconds"),
                    attempts=item.get("attempts"),
                    metrics_json=_json(item.get("metrics", {})),
                    reason=item.get("reason"),
                )
            )
        session.commit()


def _recover_madrid_taf_gap() -> dict[str, object]:
    target_issue = datetime(2026, 8, 10, 11, tzinfo=timezone.utc)
    with Session() as session:
        history = read_archive_live(
            TafReport,
            session.bind,
            filters={"airport": "LEMD", "issue_time": target_issue},
        )
    exact_revision = (
        not history.empty
        and "raw_taf" in history
        and history.raw_taf.astype(str).str.contains(
            r"\bLEMD\s+101100Z\b.*\bTX38/1016Z\b", regex=True
        ).any()
    )
    if exact_revision:
        return {"status": "already_present", "issue_time": target_issue.isoformat()}
    if datetime.now(timezone.utc) > target_issue + timedelta(days=15):
        return {
            "status": "outside_provider_retention",
            "issue_time": target_issue.isoformat(),
        }
    try:
        return backfill_taf_revision(
            ["LEMD"], target_issue + timedelta(minutes=30)
        )
    except Exception as exc:
        return {
            "status": "unavailable",
            "issue_time": target_issue.isoformat(),
            "reason": f"{type(exc).__name__}: {exc}",
        }


def run_collector(
    airport_codes: list[str] | None = None,
    *,
    now: datetime | None = None,
    trigger: str | None = None,
    collection_mode: str | None = None,
    force_models: bool = False,
    recover_known_gap: bool = False,
) -> dict[str, object]:
    """Run every scheduled write through one auditable Python entry point."""
    init_db()
    started_at = _utc(now or datetime.now(timezone.utc))
    run_trigger = (
        trigger
        or os.getenv("WEATHERMAN_TRIGGER_SOURCE", "").strip()
        or os.getenv("GITHUB_EVENT_NAME", "manual")
    )
    scheduled_at, event_created_at, queue_started_at = _scheduler_times(
        started_at,
        trigger=run_trigger,
    )
    mode = _collection_mode(
        collection_mode,
        scheduled_at=scheduled_at,
        trigger=run_trigger,
    )
    run_id = _run_id()
    catalog = trading_airports()
    requested = [code for code in (airport_codes or list(catalog)) if code in catalog]
    duplicate_run_id = _start_run(
        run_id,
        scheduled_at=scheduled_at,
        event_created_at=event_created_at,
        queue_started_at=queue_started_at,
        started_at=started_at,
        trigger=run_trigger,
        airport_codes=requested,
    )
    if duplicate_run_id is not None:
        return {
            "run_id": run_id,
            "duplicate_of": duplicate_run_id,
            "scheduled_at": scheduled_at.isoformat(),
            "event_created_at": event_created_at.isoformat(),
            "queue_started_at": queue_started_at.isoformat(),
            "started_at": started_at.isoformat(),
            "status": "duplicate-skipped",
            "collection_mode": mode,
        }
    coverage: list[dict[str, object]] = []
    results: dict[str, object] = {}
    try:
        aviation = collect_aviation_journal(requested, now=started_at)
        coverage.extend(aviation["coverage"])
        results["aviation"] = aviation["counts"]
        results["aviation_provider_metrics"] = aviation.get("provider_metrics", {})
        results["taf_gap_recovery"] = (
            _recover_madrid_taf_gap() if recover_known_gap else {"status": "skipped"}
        )
        if force_models:
            results["forced_models"] = collect(requested, days=3)
        if mode == "fixed":
            results["live_decisions"] = collect_live_decision_checkpoints(
                requested,
                now=started_at,
                aviation_already_collected=True,
                force_forecast_refresh=True,
                record_live_snapshots=False,
            )
            coverage.extend(results["live_decisions"].get("provider_coverage", []))
            # The standardized checkpoint is the only full forecast snapshot for
            # this scheduled pass. Its cutoff remains the configured local time.
            results["checkpoints"] = collect_research_checkpoints(
                requested,
                window_minutes=35,
                catchup_hours=48,
                sync_universe=False,
                now=started_at,
            )
        elif mode == "closeout":
            results["live_decisions"] = collect_live_decision_checkpoints(
                requested,
                now=started_at,
                aviation_already_collected=True,
            )
            coverage.extend(results["live_decisions"].get("provider_coverage", []))
            results["checkpoints"] = collect_research_checkpoints(
                requested,
                window_minutes=35,
                catchup_hours=48,
                sync_universe=False,
                now=started_at,
            )
        else:
            results["live_decisions"] = {"status": "skipped-lightweight-aviation"}
            results["checkpoints"] = {"status": "not-due"}
        _finish_run(run_id, status="success", results=results, coverage=coverage)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _finish_run(
            run_id,
            status="failed",
            results=results,
            coverage=coverage,
            error_reason=reason,
        )
        raise
    return {
        "run_id": run_id,
        "scheduled_at": scheduled_at.isoformat(),
        "event_created_at": event_created_at.isoformat(),
        "queue_started_at": queue_started_at.isoformat(),
        "started_at": started_at.isoformat(),
        "status": "success",
        "collection_mode": mode,
        **results,
    }


def recover_stage1_gaps(
    airport_codes: list[str] | None = None,
    *,
    now: datetime | None = None,
    report_path: Path = STAGE1_RECOVERY_REPORT,
) -> dict[str, object]:
    """Recover what is still causally reconstructable for 10/11 August 2026.

    The report deliberately distinguishes official/provider history from original
    live state.  It never relabels a later fetch as a contemporaneous snapshot.
    """
    started_at = _utc(now or datetime.now(timezone.utc))
    collector = run_collector(
        airport_codes,
        now=started_at,
        trigger="stage1-gap-recovery",
        force_models=True,
        recover_known_gap=True,
    )
    markets = backfill_market_history(2, airport_codes)
    report: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collector_version": __version__,
        "scope": "2026-08-10 through 2026-08-11 Stage-1 gaps",
        "collector": collector,
        "market_history": markets,
        "classification": {
            "metar": {
                "status": "provider_history_or_live",
                "detail": "Stored observations retain their observed time and actual fetch time.",
            },
            "taf": {
                "status": "official_revision_backfill",
                "detail": (
                    "The known LEMD 2026-08-10 11:00Z revision is requested exactly; "
                    "backfilled first-seen time prevents historical leakage."
                ),
            },
            "scheduled_checkpoints": {
                "status": "reconstructed_causal_where_possible",
                "detail": (
                    "Only inputs whose provider availability predates the checkpoint "
                    "are eligible; reconstruction is explicitly marked."
                ),
            },
            "market": {
                "status": "historical_samples_not_original_orderbook",
                "detail": "D-1/D0 historical samples are backfilled from provider history.",
            },
            "original_live_states": {
                "status": "not_recoverable",
                "detail": (
                    "Missed screen state, original order book and unrecorded intraday "
                    "snapshots are not recreated or presented as observed live history."
                ),
            },
        },
    }
    _atomic_json(report_path, report)
    return report


def coverage_audit(
    *,
    now: datetime | None = None,
    report_path: Path = LATEST_COVERAGE_REPORT,
    full_archive_validation: bool = True,
) -> dict[str, object]:
    """Explain recent collector gaps, stale sources and archive/DB health."""
    init_db()
    checked_at = _utc(now or datetime.now(timezone.utc))
    warnings: list[dict[str, object]] = []
    cadence_metrics: dict[str, object] = {
        "measurement": "no v10.7.7+ scheduler lineage available",
        "runs": 0,
        "expected_slots": 0,
        "missing_slots": 0,
    }
    catalog = trading_airports()
    with Session() as session:
        runs = pd.read_sql(
            select(CollectionRun).where(
                CollectionRun.scheduled_at >= checked_at - timedelta(hours=24)
            ),
            session.bind,
        )
        if runs.empty:
            warnings.append(
                {
                    "severity": "error",
                    "type": "collector_missing",
                    "message": "No collector run is stored for the last 24 hours.",
                }
            )
        else:
            runs["scheduled_at"] = pd.to_datetime(runs.scheduled_at, utc=True)
            runs = runs.sort_values("scheduled_at")
            measured_slots = set(runs.scheduled_at.array.to_pydatetime())
            declared_slots = _declared_slots_between(
                runs.scheduled_at.min().to_pydatetime(),
                checked_at,
            )
            missing_declared = [
                slot
                for slot in declared_slots
                if slot <= checked_at - timedelta(minutes=20)
                and slot not in measured_slots
            ]
            if missing_declared:
                warnings.append(
                    {
                        "severity": "warning",
                        "type": "collector_historical_gap",
                        "active": False,
                        "message": (
                            f"{len(missing_declared)} declared collector slot(s) are "
                            "missing in the measured span."
                        ),
                    }
                )
                recent_missing = [
                    slot
                    for slot in missing_declared
                    if slot >= checked_at - timedelta(hours=1)
                ]
                if recent_missing:
                    warnings.append(
                        {
                            "severity": "error",
                            "type": "collector_gap",
                            "active": True,
                            "message": (
                                "Current collector cadence missed "
                                f"{len(recent_missing)} recent declared slot(s)."
                            ),
                        }
                    )
            latest_expected = _expected_slot_at(checked_at)
            expected_delay = (checked_at - latest_expected).total_seconds() / 60
            if (
                latest_expected > runs.scheduled_at.max().to_pydatetime()
                and expected_delay > 20
            ):
                warnings.append(
                    {
                        "severity": "error",
                        "type": "collector_missing_recent",
                        "active": True,
                        "message": (
                            "The latest declared collector slot is still missing "
                            f"{expected_delay:.0f} minutes after its scheduled time."
                        ),
                    }
                )
            failed = runs[runs.overall_status != "success"]
            for row in failed.itertuples():
                later_success = (
                    (runs.scheduled_at > row.scheduled_at)
                    & (runs.overall_status == "success")
                ).any()
                warnings.append(
                    {
                        "severity": "error",
                        "type": "collector_failed",
                        "active": not bool(later_success),
                        "message": (
                            f"Run {row.run_id} ended as {row.overall_status}: "
                            f"{row.error_reason}"
                        ),
                    }
                )
            late = runs[runs.scheduler_drift_seconds > 15 * 60]
            for row in late.itertuples():
                warnings.append(
                    {
                        "severity": "warning",
                        "type": "collector_late",
                        "active": bool(
                            row.scheduled_at >= checked_at - timedelta(hours=1)
                        ),
                        "message": (
                            f"Run {row.run_id} started "
                            f"{row.scheduler_drift_seconds / 60:.0f} minutes late."
                        ),
                    }
                )
            lineage = runs[
                pd.to_datetime(
                    runs.get("event_created_at"), utc=True, errors="coerce"
                ).notna()
            ].copy()
            if not lineage.empty:
                for column in (
                    "event_created_at",
                    "queue_started_at",
                    "started_at",
                    "ended_at",
                ):
                    lineage[column] = pd.to_datetime(
                        lineage[column], utc=True, errors="coerce"
                    )
                first_slot = lineage.scheduled_at.min()
                last_slot = lineage.scheduled_at.max()
                expected_slots = len(_declared_slots_between(first_slot, last_slot))
                observed_slots = lineage.scheduled_at.nunique()
                runtime = pd.to_numeric(
                    lineage.execution_seconds, errors="coerce"
                ).dropna()
                trigger_delay = pd.to_numeric(
                    lineage.trigger_delay_seconds, errors="coerce"
                ).dropna()
                queue_delay = pd.to_numeric(
                    lineage.queue_delay_seconds, errors="coerce"
                ).dropna()
                trigger_coverage = (
                    float(observed_slots / expected_slots) if expected_slots else 0.0
                )
                median_queue_delay = (
                    float(queue_delay.median()) if not queue_delay.empty else None
                )
                p95_execution = (
                    float(runtime.quantile(0.95)) if not runtime.empty else None
                )
                scheduler_boundary = (
                    trigger_coverage < 0.8
                    and (median_queue_delay is None or median_queue_delay < 60)
                    and (p95_execution is None or p95_execution < 600)
                )
                cadence_metrics = {
                    "measurement": (
                        "Expected slots are inferred from the declared offset cron; "
                        "GitHub does not expose dropped-trigger causes."
                    ),
                    "runs": int(len(lineage)),
                    "expected_slots": expected_slots,
                    "observed_slots": int(observed_slots),
                    "missing_slots": max(0, expected_slots - int(observed_slots)),
                    "trigger_coverage": trigger_coverage,
                    "median_trigger_delay_seconds": (
                        float(trigger_delay.median()) if not trigger_delay.empty else None
                    ),
                    "median_queue_delay_seconds": median_queue_delay,
                    "median_execution_seconds": (
                        float(runtime.median()) if not runtime.empty else None
                    ),
                    "p95_execution_seconds": p95_execution,
                    "diagnosis": (
                        "Observed bottleneck is scheduled-trigger delivery: recorded jobs "
                        "start without a material concurrency queue and finish below the "
                        "declared active interval. GitHub does not expose why uncreated schedule "
                        "events were dropped."
                        if scheduler_boundary
                        else "No single scheduler-versus-runtime bottleneck is established."
                    ),
                }

        for code, airport in catalog.items():
            def source_frame(model):
                if full_archive_validation:
                    return read_archive_live(
                        model,
                        session.bind,
                        filters={"airport": code},
                    )
                return pd.read_sql(
                    select(model).where(model.airport == code),
                    session.bind,
                )

            observations = source_frame(Observation)
            forecasts = source_frame(Forecast)
            tafs = source_frame(TafReport)
            markets = source_frame(MarketSnapshot)
            for data_type, frame, column, maximum_age in (
                ("METAR", observations, "observed_at", 90),
                ("forecast", forecasts, "fetched_at", 210),
                ("TAF", tafs, "fetched_at", 720),
                ("market", markets, "captured_at", 180),
            ):
                if frame.empty or column not in frame:
                    warnings.append(
                        {
                            "severity": "warning",
                            "type": "source_missing",
                            "airport": code,
                            "data_type": data_type,
                            "message": f"{code}: no {data_type} data is stored.",
                        }
                    )
                    continue
                latest = pd.to_datetime(frame[column], utc=True, errors="coerce").max()
                if pd.isna(latest):
                    continue
                age = (pd.Timestamp(checked_at) - latest).total_seconds() / 60
                local = checked_at.astimezone(ZoneInfo(airport["timezone"]))
                in_active_day = 5 <= local.hour <= 22
                if in_active_day and age > maximum_age:
                    warnings.append(
                        {
                            "severity": "warning",
                            "type": "source_stale",
                            "airport": code,
                            "data_type": data_type,
                            "age_minutes": round(age, 1),
                            "message": f"{code}: {data_type} is {age:.0f} minutes old.",
                        }
                    )

            snapshots = source_frame(ForecastSnapshot)
            local_today = checked_at.astimezone(ZoneInfo(airport["timezone"])).date()
            for target in (local_today, local_today + timedelta(days=1)):
                for configured in airport.get("decision_checkpoints_local") or []:
                    label = str(configured["label"])
                    target_offset = int(configured.get("target_day_offset", 0))
                    hour, minute = (
                        int(value) for value in str(configured["time"]).split(":", 1)
                    )
                    cutoff = datetime.combine(
                        target - timedelta(days=target_offset),
                        datetime.min.time().replace(hour=hour, minute=minute),
                        ZoneInfo(airport["timezone"]),
                    ).astimezone(timezone.utc)
                    if checked_at < cutoff + timedelta(minutes=35):
                        continue
                    candidates = snapshots
                    if not candidates.empty:
                        candidates = candidates[
                            (pd.to_datetime(candidates.target_date).dt.date == target)
                            & (candidates.checkpoint_label == label)
                        ]
                    if candidates.empty:
                        warnings.append(
                            {
                                "severity": "error",
                                "type": "checkpoint_missing",
                                "airport": code,
                                "checkpoint": label,
                                "target_date": target.isoformat(),
                                "message": f"{code}: {label} is missing for {target}.",
                            }
                        )

    database_path = configured_sqlite_path()
    database_bytes = database_path.stat().st_size if database_path and database_path.exists() else 0
    if database_bytes >= DATABASE_WARNING_BYTES:
        warnings.append(
            {
                "severity": "error",
                "type": "database_growth",
                "database_bytes": database_bytes,
                "message": f"Active SQLite is {database_bytes / 1024 / 1024:.1f} MiB.",
            }
        )
    if full_archive_validation:
        try:
            manifest = validate_history_archive(DEFAULT_ARCHIVE_DIRECTORY)
            archive_status = "verified"
        except Exception as exc:
            manifest = {}
            archive_status = "failed"
            warnings.append(
                {
                    "severity": "error",
                    "type": "archive_verification_failed",
                    "message": f"Archive validation failed: {type(exc).__name__}: {exc}",
                }
            )
    else:
        manifest_path = DEFAULT_ARCHIVE_DIRECTORY / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        archive_status = "deferred_to_daily_verification"
    for warning in warnings:
        warning.setdefault("active", True)
    active_warning_count = sum(bool(item.get("active")) for item in warnings)
    report: dict[str, object] = {
        "checked_at": checked_at.isoformat(),
        "collector_version": __version__,
        "database_bytes": database_bytes,
        "archive_status": archive_status,
        "archive_partitions": int(manifest.get("partition_count", 0)),
        "archive_rows": int(manifest.get("total_rows", 0)),
        "warning_count": len(warnings),
        "active_warning_count": active_warning_count,
        "cadence": cadence_metrics,
        "warnings": warnings,
    }
    _atomic_json(report_path, report)
    refresh_database_connections()
    return report
