"""Start with --mode plan or dry-run. The full backfill is fail-closed gated."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from weatherman.historical_archive import (
    VERSION,
    ArchiveError,
    BudgetExceeded,
    RawCache,
    actuals_from_metars,
    atomic_json,
    candidate_runs,
    metars,
    model_run,
    tafs,
)
from weatherman.historical_replay import (
    build_checkpoint,
    clean,
    engine_fingerprint,
    save_records,
    schedule,
    summary,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["plan", "dry-run", "backfill"], default="plan")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 4, 1))
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--days", help="Comma-separated dry-run dates (3–5 days)")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--timing-policy", choices=["strict", "conservative"], default="strict")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--max-requests", type=int, default=250)
    args = parser.parse_args()
    if args.start > args.end or args.max_requests < 0:
        parser.error("Invalid range or request budget")
    repo = Path(__file__).resolve().parents[1]
    root = args.archive.resolve()
    if root == repo or repo in root.parents:
        parser.error("Archive must be outside the Git repository")
    airport = json.loads((repo / "src/weatherman/data/airports.json").read_text())["LEMD"]
    fingerprint = engine_fingerprint(repo)
    if args.mode == "plan":
        n = (args.end - args.start).days + 1
        print(
            json.dumps(
                {
                    "version": VERSION,
                    "days": n,
                    "checkpoints": n * 4,
                    "max_candidate_model_requests_before_cache": n * 4 * 2 * len(airport["models"]),
                    "point_data_only": True,
                    "production_neon_used": False,
                    "archive_start_note": "Most exact runs start April 2, not April 1.",
                    "estimated_point_response_bytes": [5000, 40000],
                    "status": "dry_run_required",
                },
                indent=2,
            )
        )
        return 0
    if args.mode == "dry-run":
        if not args.days:
            parser.error("Dry run requires --days with 3–5 representative dates")
        targets = sorted(set(date.fromisoformat(d.strip()) for d in args.days.split(",")))
        if not 3 <= len(targets) <= 5 or any(d < args.start or d > args.end for d in targets):
            parser.error("Choose 3–5 distinct dates within requested range")
    else:
        gate = root / "results/dry-run-gate.json"
        previous = json.loads(gate.read_text()) if gate.exists() else {}
        if not (
            previous.get("passed")
            and previous.get("engine_sha256") == fingerprint
            and previous.get("timing_policy") == args.timing_policy
            and previous.get("version") == VERSION
        ):
            raise SystemExit(
                "Full backfill blocked: no matching successful fidelity/leakage dry run"
            )
        targets = [args.start + timedelta(days=i) for i in range((args.end - args.start).days + 1)]
    cache = RawCache(
        root, offline=args.offline, max_requests=args.max_requests, retry_failed=args.retry_failed
    )
    normalized = root / "normalized"
    normalized.mkdir(parents=True, exist_ok=True)
    records = []
    coverage = []
    interrupted = False
    try:
        for target in targets:
            print(f"Archive {target}: METAR, TAF and exact model runs", flush=True)
            errors = []
            try:
                observations = metars(cache, target - timedelta(days=3), target + timedelta(days=2))
            except ArchiveError as exc:
                if isinstance(exc, BudgetExceeded):
                    raise
                observations = pd.DataFrame()
                errors.append(f"METAR: {exc}")
            try:
                aviation = tafs(cache, target - timedelta(days=2), target + timedelta(days=1))
            except ArchiveError as exc:
                if isinstance(exc, BudgetExceeded):
                    raise
                aviation = pd.DataFrame()
                errors.append(f"TAF: {exc}")
            for name, frame in (("metar", observations), ("taf", aviation)):
                frame.to_parquet(normalized / f"{target}-{name}.parquet", index=False)
            actuals = actuals_from_metars(observations)
            paths = []
            day_coverage = {}
            for model in airport["models"]:
                runs = sorted(
                    {run for _, cutoff in schedule(target) for run in candidate_runs(model, cutoff)}
                )
                valid_runs = 0
                for run in runs:
                    try:
                        frame = model_run(cache, airport, model, run)
                        paths.append(frame)
                        valid_runs += 1
                    except ArchiveError as exc:
                        if isinstance(exc, BudgetExceeded):
                            raise
                        errors.append(f"{model} {run.isoformat()}: {exc}")
                day_coverage[model] = {"candidate_runs": len(runs), "archived_runs": valid_runs}
            hourly = pd.concat(paths, ignore_index=True) if paths else pd.DataFrame()
            hourly.to_parquet(normalized / f"{target}-model-paths.parquet", index=False)
            actuals.to_parquet(normalized / f"{target}-actuals.parquet", index=False)
            for label, cutoff in schedule(target):
                records.append(
                    build_checkpoint(
                        airport=airport,
                        target=target,
                        label=label,
                        checkpoint=cutoff,
                        paths=hourly,
                        observations=observations,
                        tafs=aviation,
                        actuals=actuals,
                        timing_policy=args.timing_policy,
                        source_errors=errors,
                    )
                )
            coverage.append(
                {
                    "target_date": str(target),
                    "models": day_coverage,
                    "metar_reports": len(observations),
                    "taf_revisions": len(aviation),
                }
            )
            cache.state.setdefault("days", {})[str(target)] = {
                "status": "processed_partial" if errors else "processed",
                "errors": errors,
                "coverage": day_coverage,
            }
            atomic_json(root / "backfill_state.json", cache.state)
            atomic_json(root / "derived/checkpoints.json", records)
    except BudgetExceeded as exc:
        interrupted = True
        print(str(exc), flush=True)
    finally:
        cache.close()
    report = summary(records)
    cached_requests = list(cache.state["requests"].values())
    report.update(
        {
            "requested_start": str(args.start),
            "requested_end": str(args.end),
            "requested_dry_run_dates": [str(t) for t in targets],
            "source_coverage": coverage,
            "interrupted": interrupted,
            "api_requests_this_run": cache.requests,
            "api_response_bytes_this_run": cache.download_bytes,
            "cache_hits_this_run": cache.hits,
            "distinct_cached_requests": len(cached_requests),
            "cached_successful_response_bytes": sum(r.get("bytes", 0) for r in cached_requests),
            "oldest_full_fidelity_date": None,
            "newest_full_fidelity_date": None,
            "oldest_partial_fidelity_date": min((r["target_date"] for r in records), default=None),
            "newest_partial_fidelity_date": max((r["target_date"] for r in records), default=None),
            "complete_actual_target_days": len(
                {r["target_date"] for r in records if r.get("actual_complete")}
            ),
            "engine_sha256": fingerprint,
            "new_regimes": 0,
            "existing_challengers_modified": False,
        }
    )
    if records:
        master = save_records(root, records)
        report["master_dataset"] = str(master)
        report["master_dataset_bytes"] = master.stat().st_size
    report["archive_size_bytes"] = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    atomic_json(root / "results/historical-replay-summary.json", clean(report))
    if args.mode == "dry-run":
        atomic_json(
            root / "results/dry-run-gate.json",
            {
                "version": VERSION,
                "passed": report["dry_run_passed"] and not interrupted,
                "timing_policy": args.timing_policy,
                "engine_sha256": fingerprint,
            },
        )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "target_days",
                    "checkpoints",
                    "evidence_counts",
                    "status_counts",
                    "large_backfill_status",
                    "api_requests_this_run",
                    "api_response_bytes_this_run",
                )
            },
            indent=2,
        )
    )
    return 0 if report["dry_run_passed"] and not interrupted else 2


if __name__ == "__main__":
    raise SystemExit(main())
