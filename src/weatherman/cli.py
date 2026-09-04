from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .collector import coverage_audit, recover_stage1_gaps, run_collector
from .hourly_archive import archive_sqlite_history, rebuild_manifest
from .service import (
    backfill,
    backfill_market_history,
    backfill_taf_revision,
    collect,
    collect_live_decision_checkpoints,
    collect_research_checkpoints,
    sync_airport_universe,
)
from .maintenance import maintain_sqlite_database
from .db import ENGINE, Base, init_db
from .research_diagnostics import (
    peak_lock_research_report,
    replay_readiness_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="weatherman")
    subs = parser.add_subparsers(dest="command", required=True)
    collect_cmd = subs.add_parser("collect")
    collect_cmd.add_argument("--airports", nargs="*")
    collect_cmd.add_argument("--days", type=int, default=3)
    collector_cmd = subs.add_parser("run-collector")
    collector_cmd.add_argument("--airports", nargs="*")
    collector_cmd.add_argument(
        "--mode",
        choices=("auto", "aviation", "fixed", "closeout"),
        default="auto",
    )
    collector_cmd.add_argument("--force-models", action="store_true")
    collector_cmd.add_argument("--recover-known-taf-gap", action="store_true")
    backfill_cmd = subs.add_parser("backfill")
    backfill_cmd.add_argument("--airports", nargs="*")
    backfill_cmd.add_argument("--days", type=int, default=365)
    market_cmd = subs.add_parser("backfill-market-history")
    market_cmd.add_argument("--airports", nargs="*")
    market_cmd.add_argument("--days", type=int, default=30)
    research_cmd = subs.add_parser("collect-research-checkpoints")
    research_cmd.add_argument("--airports", nargs="*")
    research_cmd.add_argument("--window-minutes", type=int, default=35)
    research_cmd.add_argument("--catchup-hours", type=int, default=48)
    research_cmd.add_argument("--skip-universe-sync", action="store_true")
    decision_cmd = subs.add_parser("collect-live-decisions")
    decision_cmd.add_argument("--airports", nargs="*")
    universe_cmd = subs.add_parser("sync-airport-universe")
    universe_cmd.add_argument("--include-closed", action="store_true")
    maintenance_cmd = subs.add_parser("maintain-database")
    maintenance_cmd.add_argument("--retention-days", type=int, default=3)
    maintenance_cmd.add_argument("--hourly-days", type=int)
    maintenance_cmd.add_argument("--archive-directory", type=Path)
    taf_backfill_cmd = subs.add_parser("backfill-taf-revision")
    taf_backfill_cmd.add_argument("--airports", nargs="+", required=True)
    taf_backfill_cmd.add_argument("--at", required=True)
    coverage_cmd = subs.add_parser("audit-coverage")
    coverage_cmd.add_argument("--report", type=Path)
    coverage_cmd.add_argument("--fast", action="store_true")
    recovery_cmd = subs.add_parser("recover-stage1-gaps")
    recovery_cmd.add_argument("--airports", nargs="*")
    recovery_cmd.add_argument("--report", type=Path)
    archive_cmd = subs.add_parser("archive-hourly-history")
    archive_cmd.add_argument("--database", type=Path, required=True)
    archive_cmd.add_argument("--archive-directory", type=Path, required=True)
    archive_cmd.add_argument("--before")
    audit_cmd = subs.add_parser("audit-hourly-archive")
    audit_cmd.add_argument("--archive-directory", type=Path, default=Path("data/hourly_archive"))
    peak_lock_cmd = subs.add_parser("research-peak-lock")
    peak_lock_cmd.add_argument("--report", type=Path)
    readiness_cmd = subs.add_parser("replay-readiness")
    readiness_cmd.add_argument("--report", type=Path)
    subs.add_parser("init-db")
    subs.add_parser("database-health")
    args = parser.parse_args()
    if args.command == "run-collector":
        result = run_collector(
            args.airports,
            collection_mode=args.mode,
            force_models=args.force_models,
            recover_known_gap=args.recover_known_taf_gap,
        )
    elif args.command == "collect":
        result = collect(args.airports, args.days)
    elif args.command == "backfill-market-history":
        result = backfill_market_history(args.days, args.airports)
    elif args.command == "collect-research-checkpoints":
        result = collect_research_checkpoints(
            args.airports,
            window_minutes=args.window_minutes,
            catchup_hours=args.catchup_hours,
            sync_universe=not args.skip_universe_sync,
        )
    elif args.command == "collect-live-decisions":
        result = collect_live_decision_checkpoints(args.airports)
    elif args.command == "sync-airport-universe":
        result = sync_airport_universe(include_closed=args.include_closed)
    elif args.command == "maintain-database":
        result = maintain_sqlite_database(
            retention_days=args.retention_days,
            hourly_retention_days=args.hourly_days,
            archive_directory=args.archive_directory,
        )
    elif args.command == "backfill-taf-revision":
        result = backfill_taf_revision(
            args.airports,
            datetime.fromisoformat(args.at.replace("Z", "+00:00")),
        )
    elif args.command == "audit-coverage":
        result = coverage_audit(
            **({"report_path": args.report} if args.report is not None else {}),
            full_archive_validation=not args.fast,
        )
    elif args.command == "recover-stage1-gaps":
        result = recover_stage1_gaps(
            args.airports,
            **({"report_path": args.report} if args.report is not None else {}),
        )
    elif args.command == "archive-hourly-history":
        result = archive_sqlite_history(
            args.database,
            args.archive_directory,
            before=args.before,
        )
    elif args.command == "audit-hourly-archive":
        result = rebuild_manifest(args.archive_directory)
    elif args.command == "research-peak-lock":
        result = peak_lock_research_report(
            **({"report_path": args.report} if args.report is not None else {})
        )
    elif args.command == "replay-readiness":
        result = replay_readiness_report(
            **({"report_path": args.report} if args.report is not None else {})
        )
    elif args.command == "init-db":
        init_db()
        result = {
            "status": "ready",
            "backend": ENGINE.dialect.name,
            "tables": len(Base.metadata.tables),
        }
    elif args.command == "database-health":
        init_db()
        with ENGINE.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        result = {
            "status": "healthy",
            "backend": ENGINE.dialect.name,
        }
    else:
        result = backfill(args.days, args.airports)
    print(result)


if __name__ == "__main__":
    main()
