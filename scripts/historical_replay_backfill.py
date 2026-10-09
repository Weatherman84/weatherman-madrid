"""Start with --mode plan or dry-run. The full backfill is fail-closed gated."""

from __future__ import annotations

import argparse
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
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
    model_runs,
    tafs,
)
from weatherman.historical_replay import (
    build_checkpoint,
    clean,
    daily_forecasts,
    engine_fingerprint,
    save_records,
    schedule,
    summary,
)


def download_runs(cache, airport, groups):
    """Eight in-flight point requests, with the shared cache's global rate limit."""

    def fetch(item):
        run, models = item
        try:
            return model_runs(cache, airport, models, run), []
        except BudgetExceeded:
            raise
        except ArchiveError as exc:
            if len(models) == 1:
                return pd.DataFrame(), [f"{models[0]} {run}: {exc}"]
            frames, errors = [], []
            # Preserve accessible models if one member makes a batch unavailable.
            for model in models:
                try:
                    frames.append(model_run(cache, airport, model, run))
                except BudgetExceeded:
                    raise
                except ArchiveError as inner:
                    errors.append(f"{model} {run}: {inner}")
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(), errors

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, sorted(groups.items())))
    frames = [frame for frame, _ in results if not frame.empty]
    return (
        pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(),
        [error for _, errors in results for error in errors],
    )


def warmup(cache, airport, targets, normalized, days):
    """Native D-1 calibration pairs only; observations never substitute forecasts."""
    needed = sorted(
        {
            target - timedelta(days=i)
            for target in targets
            for i in range(1, days + 1)
            if target - timedelta(days=i) >= date(2026, 4, 3)
        }
    )
    directory = normalized / "warmup"
    directory.mkdir(exist_ok=True)

    def one_day(day):
        path = directory / f"{day}.parquet"
        if path.exists():
            return pd.read_parquet(path)
        print(f"Warmup D-1 {day}", flush=True)
        groups = {}
        cutoff = schedule(day)[0][1]
        for model in airport["models"]:
            run = candidate_runs(model, cutoff, count=1)[0]
            groups.setdefault(run, []).append(model)
        hourly, errors = download_runs(cache, airport, groups)
        forecasts = daily_forecasts(hourly)
        if not forecasts.empty:
            forecasts = forecasts[pd.to_datetime(forecasts.target_date).dt.date == day]
        # Failed or partial model sets are not certified/resume-skipped.
        if not errors and not forecasts.empty and set(forecasts.model) == set(airport["models"]):
            forecasts.to_parquet(path, index=False)
        return forecasts

    with ThreadPoolExecutor(max_workers=4) as pool:
        frames = list(pool.map(one_day, needed))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


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
    parser.add_argument("--warmup-days", type=int, default=90)
    args = parser.parse_args()
    if args.start > args.end or args.max_requests < 0 or not 0 <= args.warmup_days <= 90:
        parser.error("Invalid range or request budget")
    repo = Path(__file__).resolve().parents[1]
    root = args.archive.resolve()
    if root == repo or repo in root.parents:
        parser.error("Archive must be outside the Git repository")
    airport = json.loads((repo / "src/weatherman/data/airports.json").read_text())["LEMD"]
    fingerprint = engine_fingerprint(repo)
    if args.end >= date.today():
        parser.error("End must be a completed historical day (before today UTC)")
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
            and previous.get("warmup_days") == args.warmup_days
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
    cohort = hashlib.sha256(
        json.dumps(
            {"engine": fingerprint, "timing": args.timing_policy, "warmup_days": args.warmup_days},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    day_directory = root / "derived" / cohort / "days"
    day_directory.mkdir(parents=True, exist_ok=True)
    try:
        history = warmup(cache, airport, targets, normalized, args.warmup_days)
        if args.mode == "backfill":
            groups = {}
            for target in targets:
                for model in airport["models"]:
                    for _, cutoff in schedule(target):
                        for run in candidate_runs(model, cutoff):
                            groups.setdefault(run, set()).add(model)
            print(f"Prefetch {len(groups)} distinct model initialization groups", flush=True)
            download_runs(cache, airport, {run: sorted(models) for run, models in groups.items()})
        earliest = min(targets) - timedelta(days=args.warmup_days + 3)
        observations_all = metars(cache, earliest, max(targets) + timedelta(days=2))
        actuals_all = actuals_from_metars(observations_all)
        for target in targets:
            saved = day_directory / f"{target}.json"
            if saved.exists():
                restored = json.loads(saved.read_text())
                records.extend(restored["records"])
                coverage.append(restored["coverage"])
                print(f"Resume {target}: four persisted checkpoints", flush=True)
                continue
            print(f"Archive {target}: METAR, TAF and exact model runs", flush=True)
            errors = []
            start = pd.Timestamp(target - timedelta(days=3), tz="Europe/Madrid")
            stop = pd.Timestamp(target + timedelta(days=2), tz="Europe/Madrid")
            observations = observations_all[
                (pd.to_datetime(observations_all.observed_at, utc=True) >= start)
                & (pd.to_datetime(observations_all.observed_at, utc=True) < stop)
            ].copy()
            try:
                aviation = tafs(
                    cache,
                    target - timedelta(days=2),
                    target + timedelta(days=1),
                    checkpoints=[cutoff for _, cutoff in schedule(target)],
                )
            except ArchiveError as exc:
                if isinstance(exc, BudgetExceeded):
                    raise
                aviation = pd.DataFrame()
                errors.append(f"TAF: {exc}")
            for name, frame in (("metar", observations), ("taf", aviation)):
                frame.to_parquet(normalized / f"{target}-{name}.parquet", index=False)
            actuals = actuals_all
            day_coverage = {}
            groups = {}
            for model in airport["models"]:
                runs = sorted(
                    {run for _, cutoff in schedule(target) for run in candidate_runs(model, cutoff)}
                )
                for run in runs:
                    groups.setdefault(run, []).append(model)
                day_coverage[model] = {"candidate_runs": len(runs), "archived_runs": 0}
            hourly, model_errors = download_runs(cache, airport, groups)
            errors.extend(model_errors)
            for model in airport["models"]:
                day_coverage[model]["archived_runs"] = (
                    len(set(hourly[hourly.model == model].model_run_at)) if not hourly.empty else 0
                )
            hourly.to_parquet(normalized / f"{target}-model-paths.parquet", index=False)
            actuals.to_parquet(normalized / f"{target}-actuals.parquet", index=False)
            day_records = []
            for label, cutoff in schedule(target):
                day_records.append(
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
                        history_forecasts=history,
                    )
                )
            records.extend(day_records)
            day_info = {
                "target_date": str(target),
                "models": day_coverage,
                "metar_reports": len(observations),
                "taf_revisions": len(aviation),
            }
            coverage.append(day_info)
            atomic_json(saved, {"records": day_records, "coverage": day_info})
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
    if args.mode == "backfill":
        complete = not interrupted and len(records) == len(targets) * 4
        report["large_backfill_status"] = (
            "completed_diagnostic_only" if complete else "incomplete_resumable"
        )
        report["all_requested_days_processed"] = complete
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
            "oldest_partial_fidelity_date": min(
                (r["target_date"] for r in records if r.get("champion_center_c") is not None),
                default=None,
            ),
            "newest_partial_fidelity_date": max(
                (r["target_date"] for r in records if r.get("champion_center_c") is not None),
                default=None,
            ),
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
                "warmup_days": args.warmup_days,
                "diagnostic_only": not report["full_fidelity_gate_passed"],
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
    success = report.get("all_requested_days_processed", report["dry_run_passed"])
    return 0 if success and not interrupted else 2


if __name__ == "__main__":
    raise SystemExit(main())
