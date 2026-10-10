"""Enrich an existing isolated archive, then rerun the protected engine offline."""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import multiprocessing
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from weatherman.historical_archive import (
    ArchiveError,
    BudgetExceeded,
    RawCache,
    atomic_json,
    digest,
    utc,
)
from weatherman.historical_enrichment import (
    ENRICHMENT_VERSION,
    PREVIOUS_URL,
    fetch_ogimet,
    fetch_pressure,
    policy_engine,
    policy_parameters,
    shift_availability,
)
from weatherman.historical_replay import (
    build_checkpoint,
    clean,
    engine_fingerprint,
    metrics,
    save_records,
    schedule,
    summary,
)
from weatherman.decision import DayStatus
from weatherman.checkpoint_challengers import D0_ANCHOR_POLICIES, _late_active_policies


def write_parquet(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.parquet")
    frame.to_parquet(temporary, index=False)
    temporary.replace(path)


def policy_names(checkpoint):
    if checkpoint.startswith("D0"):
        return list(D0_ANCHOR_POLICIES)
    if checkpoint.startswith("Late"):
        return list(_late_active_policies())
    return ["current_protected_forecast"]


def available_prior(rows, current, window=None):
    target = date.fromisoformat(current["target_date"])
    cutoff = utc(current["checkpoint_at"])
    return [
        r
        for r in rows
        if r["checkpoint_label"] == current["checkpoint_label"]
        and r["evidence_class"] == current["evidence_class"]
        and r.get("actual_complete")
        and r.get("actual_bucket") is not None
        and r.get("champion_center_c") is not None
        and r["target_date"] < current["target_date"]
        and (
            window is None
            or date.fromisoformat(r["target_date"]) >= target - timedelta(days=window)
        )
        and r.get("actual_available_at")
        and utc(r["actual_available_at"]) <= cutoff
    ]


def choose_full_policy(prior, family):
    scored = {
        name: metrics(
            [r["full_policy_forecasts"][name] for r in prior if name in r["full_policy_forecasts"]]
        )
        for name in family
    }
    baseline = family[0]
    if len(prior) < 10:
        return baseline, scored
    best = min(
        family,
        key=lambda n: (-scored[n].get("modal_accuracy", 0), scored[n].get("mae_c", float("inf"))),
    )
    b = scored[baseline]
    a = scored[best]
    if best != baseline and (
        a["modal_accuracy"] > b["modal_accuracy"] + 1e-12
        or (
            abs(a["modal_accuracy"] - b["modal_accuracy"]) < 1e-12
            and b["mae_c"] - a["mae_c"] >= 0.02
        )
    ):
        return best, scored
    return baseline, scored


def compact(row):
    fields = [
        "target_date",
        "checkpoint_label",
        "checkpoint_at",
        "evidence_class",
        "champion_center_c",
        "champion_modal_bucket",
        "champion_probabilities",
        "top1_probability",
        "stored_metar_actual_c",
        "actual_bucket",
        "actual_complete",
        "actual_available_at",
        "research_only",
        "automatic_promotion",
        "status",
    ]
    return {k: row.get(k) for k in fields}


def study(rows, split, excluded):
    usable = [
        r
        for r in rows
        if r.get("champion_center_c") is not None and r["target_date"] not in excluded
    ]
    result = []
    records = []
    for checkpoint, _, _ in __import__(
        "weatherman.historical_replay", fromlist=["CHECKPOINTS"]
    ).CHECKPOINTS:
        group = [r for r in usable if r["checkpoint_label"] == checkpoint]
        family = policy_names(checkpoint)
        anchor = {
            "target_date": split,
            "checkpoint_at": dict(schedule(date.fromisoformat(split)))[checkpoint].isoformat(),
            "checkpoint_label": checkpoint,
            "evidence_class": "historical_partial_fidelity",
        }
        train = available_prior(group, anchor)
        choice, matrix = choose_full_policy(train, family)
        evaluation = [r for r in group if r["target_date"] >= split]
        entry = {
            "checkpoint": checkpoint,
            "frozen_policy": choice,
            "freeze_at": anchor["checkpoint_at"],
            "training_n": len(train),
            "training_parameter_matrix": matrix,
            "evaluation_baseline": metrics(evaluation),
            "evaluation_frozen_policy": metrics(
                [r["full_policy_forecasts"][choice] for r in evaluation]
            ),
            "walk_forward": [],
        }
        for window in (None, 90):
            predictions = []
            for current in group:
                prior = available_prior(group, current, window)
                chosen, _ = choose_full_policy(prior, family)
                prediction = {
                    **current["full_policy_forecasts"][chosen],
                    "selected_policy": chosen,
                    "prior_n": len(prior),
                    "window": "expanding" if window is None else "rolling_90_days",
                }
                predictions.append(prediction)
            entry["walk_forward"].append(
                {
                    "window": "expanding" if window is None else "rolling_90_days",
                    "baseline": metrics(group),
                    "policy": metrics(predictions),
                }
            )
            records.extend(predictions)
        result.append(entry)
    return {
        "research_only": True,
        "automatic_promotion": False,
        "certified_oos": False,
        "evaluation_period_previously_viewed": True,
        "evaluation_is_new_untouched_holdout": False,
        "full_engine_policy_rerun": True,
        "method": "isolated_protected_function_with_pre_guard_correction_hooks",
        "new_regimes": 0,
        "new_challengers": 0,
        "results": result,
    }, records


def acquire_previous(cache, root, airport):
    # This is a separate 48-hour lead calibration sensitivity, NEVER a silent D-1 replacement.
    prev_path = root / "normalized/previous-lead48.parquet"
    if not prev_path.exists():
        data, meta = cache.get(
            PREVIOUS_URL,
            {
                "latitude": airport["latitude"],
                "longitude": airport["longitude"],
                "models": ",".join(sorted(airport["models"])),
                "start_date": "2025-12-31",
                "end_date": "2026-04-02",
                "hourly": "temperature_2m_previous_day2",
                "timezone": "UTC",
            },
            kind="json",
        )
        payload = json.loads(data)["hourly"]
        times = pd.DatetimeIndex(pd.to_datetime(payload["time"], utc=True))
        pairs = []
        for model in airport["models"]:
            key = "temperature_2m_previous_day2_" + model
            frame = pd.DataFrame(
                {"valid_at": times, "temp_c": payload.get(key, [None] * len(times))}
            )
            frame["target_date"] = frame.valid_at.dt.tz_convert("Europe/Madrid").dt.date
            for day, g in frame.groupby("target_date"):
                if not date(2026, 1, 1) <= day <= date(2026, 4, 2):
                    continue
                start = pd.Timestamp(day, tz="Europe/Madrid")
                end = start + pd.DateOffset(days=1)
                expected = pd.date_range(start, end, freq="h", inclusive="left").tz_convert("UTC")
                if len(expected.difference(pd.DatetimeIndex(g.valid_at))) or g.temp_c.isna().any():
                    continue
                known = (
                    g.valid_at.max() - pd.Timedelta(hours=48) + pd.Timedelta(hours=6, minutes=10)
                )
                if known > utc(dict(schedule(day))["D-1 Evening @20:00"]):
                    continue
                pairs.append(
                    {
                        "airport": "LEMD",
                        "model": model,
                        "source": "open-meteo",
                        "target_date": day,
                        "max_temp_c": float(g.temp_c.max()),
                        "run_at": known,
                        "available_at": known,
                        "model_run_at": pd.NaT,
                        "model_run_upper_bound_at": g.valid_at.max() - pd.Timedelta(hours=48),
                        "horizon": "D-1",
                        "availability_basis": "estimated_fixed_lead48_plus_publication_delay",
                        "calibration_evidence": "fixed_48h_lead_sensitivity_not_exact_d1_run",
                        "source_sha256": meta["sha256"],
                        "fetched_at": meta["fetched_at"],
                    }
                )
        write_parquet(prev_path, pd.DataFrame(pairs))
        print(f"Previous fixed-lead48 calibration {len(pairs)} rows", flush=True)


def acquire(root, source, airport, max_requests):
    cache = RawCache(root, max_requests=max_requests, retry_failed=True)
    errors = []
    try:
        taf_path = root / "normalized/ogimet-tafs.parquet"
        if not taf_path.exists():
            print("Acquire OGIMET full issuance period", flush=True)
            aviation = fetch_ogimet(cache, date(2026, 3, 1), date(2026, 10, 9))
            aviation = aviation.drop_duplicates(["issue_time", "content_hash"])
            write_parquet(taf_path, aviation)
            print(f"OGIMET {len(aviation)} original bulletins", flush=True)
        files = sorted((source / "normalized").glob("*-model-paths.parquet"))
        groups = {}
        for path in files:
            frame = pd.read_parquet(path)
            if frame.empty:
                continue
            frame = frame[["model", "model_run_at"]]
            for model, run in frame.drop_duplicates().itertuples(index=False):
                groups.setdefault(utc(run), set()).add(model)
        ordered = sorted(groups.items())
        print(f"Acquire pressure: {len(ordered)} init groups", flush=True)

        def one(item):
            run, models = item
            path = root / "normalized/pressure" / f"{run:%Y%m%dT%H%M}.parquet"
            if path.exists():
                return None
            try:
                frame = fetch_pressure(cache, airport, run, sorted(models))
                write_parquet(path, frame)
            except BudgetExceeded:
                raise
            except ArchiveError as exc:
                return f"pressure {run}: {exc}"
            return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            for i, error in enumerate(pool.map(one, ordered), 1):
                if error:
                    errors.append(error)
                if i % 40 == 0:
                    print(f"Pressure {i}/{len(ordered)} requests={cache.requests}", flush=True)
        acquire_previous(cache, root, airport)
    finally:
        atomic_json(
            root / "results/acquisition.json",
            {
                "requests": cache.requests,
                "bytes": cache.download_bytes,
                "errors": errors,
                "research_only": True,
                "production_reads": 0,
                "production_writes": 0,
            },
        )
        cache.close()


_REPLAY_CONTEXT = None


def replay_one_day(item):
    root, source, airport, pressure, history, previous, actuals, tafs, directory, targets = (
        _REPLAY_CONTEXT
    )
    index, day_string = item
    rows = []  # First pass; rebuilt memory is attached in a second causal pass.
    saved = directory / f"{day_string}.json"
    if saved.exists():
        stored = json.loads(saved.read_text())
        print(f"Resume {day_string}", flush=True)
        return stored["records"], stored["sensitivity"]
    day = date.fromisoformat(day_string)
    paths = pd.read_parquet(source / f"normalized/{day}-model-paths.parquet")
    paths = paths.drop(columns=["temp_850hpa_c"], errors="ignore").merge(
        pressure, on=["model", "model_run_at", "valid_at"], how="left", validate="many_to_one"
    )
    write_parquet(root / f"normalized/{day}-model-paths.parquet", paths)
    observations = pd.read_parquet(source / f"normalized/{day}-metar.parquet")
    old_taf = pd.read_parquet(source / f"normalized/{day}-taf.parquet")
    # Prefer richer AWC decoding only for IDENTICAL raw bulletin identity. Different revisions remain distinct.
    aviation = pd.concat([tafs, old_taf], ignore_index=True).drop_duplicates(
        ["issue_time", "content_hash"], keep="last"
    )
    aviation = aviation[
        (
            pd.to_datetime(aviation.issue_time, utc=True)
            >= pd.Timestamp(day - timedelta(days=2), tz="UTC")
        )
        & (
            pd.to_datetime(aviation.issue_time, utc=True)
            < pd.Timestamp(day + timedelta(days=1), tz="UTC")
        )
    ]
    write_parquet(root / f"normalized/{day}-taf.parquet", aviation)
    day_rows = []
    day_sensitivity = []
    prior_terminal = None
    snapshot_history = pd.DataFrame(
        [
            {
                "target_date": r["target_date"],
                "captured_at": r["checkpoint_at"],
                "final_forecast_c": r["champion_center_c"],
                "temp_anchor_adjustment_c": r.get("temp_anchor_adjustment_c"),
                "features_json": json.dumps(r.get("features_json", {})),
                "hours_to_peak": r.get("features_json", {}).get("hours_to_peak"),
                "day_phase": r.get("terminal_state", {}).get("phase"),
            }
            for r in rows
            if r.get("champion_center_c") is not None
        ]
    )
    for label, cutoff in schedule(day):
        kwargs = dict(
            airport=airport,
            target=day,
            label=label,
            checkpoint=cutoff,
            paths=paths,
            observations=observations,
            tafs=aviation,
            actuals=actuals,
            timing_policy="conservative",
            source_errors=[],
            history_forecasts=history,
            build_challengers=True,
            snapshot_history=snapshot_history,
            prior_terminal_status=prior_terminal,
        )
        baseline = build_checkpoint(**kwargs)
        baseline["replay_engine_version"] = ENRICHMENT_VERSION
        baseline["full_policy_forecasts"] = {}
        for name in policy_names(label):
            params = policy_parameters(label, name)
            if all(value == 1 for value in params.values()):
                candidate = baseline
            else:
                candidate = build_checkpoint(
                    **{
                        **kwargs,
                        "build_challengers": False,
                        "nowcast_builder": policy_engine(json.dumps(params, sort_keys=True)),
                    }
                )
            baseline["full_policy_forecasts"][name] = compact(candidate)
        # Verify the isolated identity hook, independently, on the three registered dry-run days.
        if day_string in {"2026-04-04", "2026-07-15", "2026-09-10"}:
            identity = build_checkpoint(
                **{**kwargs, "build_challengers": False, "nowcast_builder": policy_engine("{}")}
            )
            if identity.get("champion_probabilities") != baseline.get(
                "champion_probabilities"
            ) or identity.get("champion_center_c") != baseline.get("champion_center_c"):
                raise ArchiveError("Isolated identity engine differs from protected baseline")
        scenarios = {
            "publication_extra_60m": {"paths": shift_availability(paths, 60)},
            "publication_extra_120m": {"paths": shift_availability(paths, 120)},
            "metar_taf_extra_10m": {
                "observations": shift_availability(observations, 10),
                "tafs": shift_availability(aviation, 10),
            },
            "complete_single_run_only": {"include_stitched": False},
        }
        # Alternative lead48 warmup is explicitly separate and only assessed during the initially short warmup period.
        if day < date(2026, 7, 3):
            scenarios["alternative_lead48_warmup"] = {
                "history_forecasts": pd.concat([previous, history], ignore_index=True)
            }
        for name, changes in scenarios.items():
            candidate = build_checkpoint(**{**kwargs, "build_challengers": False, **changes})
            day_sensitivity.append(
                {
                    "scenario": name,
                    **compact(candidate),
                    "paired_warmup": candidate["warmup"],
                    "baseline_center_c": baseline.get("champion_center_c"),
                    "baseline_modal_bucket": baseline.get("champion_modal_bucket"),
                }
            )
        day_rows.append(baseline)
        if baseline.get("terminal_state"):
            prior_terminal = DayStatus(**baseline["terminal_state"])
    atomic_json(saved, clean({"records": day_rows, "sensitivity": day_sensitivity}))
    print(f"Replay {index}/{len(targets)} {day}: full engine policies + sensitivity", flush=True)
    return day_rows, day_sensitivity


def rebuild_memory_day(day_string):
    root, source, airport, history, actuals, snapshots = _REPLAY_CONTEXT
    day = date.fromisoformat(day_string)
    paths = pd.read_parquet(root / f"normalized/{day}-model-paths.parquet")
    observations = pd.read_parquet(source / f"normalized/{day}-metar.parquet")
    tafs = pd.read_parquet(root / f"normalized/{day}-taf.parquet")
    output = []
    terminal = None
    for label, cutoff in schedule(day):
        row = build_checkpoint(
            airport=airport,
            target=day,
            label=label,
            checkpoint=cutoff,
            paths=paths,
            observations=observations,
            tafs=tafs,
            actuals=actuals,
            timing_policy="conservative",
            source_errors=[],
            history_forecasts=history,
            build_challengers=True,
            snapshot_history=snapshots,
            prior_terminal_status=terminal,
        )
        output.append(row)
        if row.get("terminal_state"):
            terminal = DayStatus(**row["terminal_state"])
    print(f"Rebuilt shadow memory {day}", flush=True)
    return output


def replay(root, source, airport, dates):
    tafs = pd.read_parquet(root / "normalized/ogimet-tafs.parquet")
    pressure = pd.concat(
        [pd.read_parquet(p) for p in sorted((root / "normalized/pressure").glob("*.parquet"))],
        ignore_index=True,
    )
    history_files = sorted((source / "normalized/warmup").glob("*.parquet"))
    history = pd.concat([pd.read_parquet(p) for p in history_files], ignore_index=True)
    previous = pd.read_parquet(root / "normalized/previous-lead48.parquet")
    actuals = pd.read_parquet(source / "normalized/2026-04-01-actuals.parquet")
    # Last archive day has the same global Actual query; verify coverage before reuse.
    old = json.loads((source / "derived/checkpoints.json").read_text())
    targets = sorted({r["target_date"] for r in old})
    if dates:
        targets = [d for d in targets if d in dates]
    fingerprint = digest(
        json.dumps(engine_fingerprint(Path(__file__).resolve().parents[1]), sort_keys=True).encode()
        + Path(__file__).read_bytes()
    )
    input_files = [
        root / "normalized/ogimet-tafs.parquet",
        root / "normalized/previous-lead48.parquet",
        source / "derived/checkpoints.json",
        *sorted((root / "normalized/pressure").glob("*.parquet")),
        *history_files,
    ]
    input_hashes = {str(path): digest(path.read_bytes()) for path in input_files}
    fingerprint = digest(
        fingerprint.encode()
        + json.dumps(input_hashes, sort_keys=True).encode()
        + json.dumps(targets).encode()
    )
    atomic_json(
        root / "results/replay-input-manifest.json",
        {"fingerprint": fingerprint, "sha256": input_hashes},
    )
    directory = root / "derived" / fingerprint / "days"
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    sensitivities = []
    global _REPLAY_CONTEXT
    _REPLAY_CONTEXT = (
        root,
        source,
        airport,
        pressure,
        history,
        previous,
        actuals,
        tafs,
        directory,
        targets,
    )
    with ProcessPoolExecutor(max_workers=8, mp_context=multiprocessing.get_context("fork")) as pool:
        for day_rows, day_sensitivity in pool.map(replay_one_day, enumerate(targets, 1)):
            rows.extend(day_rows)
            sensitivities.extend(day_sensitivity)
    # Memory is shadow-only. Rebuild from the updated first-pass forecasts, never current labels.
    memory_snapshots = pd.DataFrame(
        [
            {
                "target_date": r["target_date"],
                "captured_at": r["checkpoint_at"],
                "final_forecast_c": r["champion_center_c"],
                "temp_anchor_adjustment_c": r.get("temp_anchor_adjustment_c"),
                "features_json": json.dumps(r.get("features_json", {})),
                "hours_to_peak": r.get("features_json", {}).get("hours_to_peak"),
                "day_phase": r.get("terminal_state", {}).get("phase"),
            }
            for r in rows
            if r.get("champion_center_c") is not None
        ]
    )
    _REPLAY_CONTEXT = (root, source, airport, history, actuals, memory_snapshots)
    with ProcessPoolExecutor(max_workers=8, mp_context=multiprocessing.get_context("fork")) as pool:
        rebuilt = list(pool.map(rebuild_memory_day, targets))
    rebuilt_map = {(r["target_date"], r["checkpoint_label"]): r for day in rebuilt for r in day}
    for row in rows:
        enriched = rebuilt_map[(row["target_date"], row["checkpoint_label"])]
        if row.get("champion_center_c") != enriched.get("champion_center_c") or row.get(
            "champion_probabilities"
        ) != enriched.get("champion_probabilities"):
            raise ArchiveError("Shadow-only reconstructed memory changed the champion")
        for key in (
            "features_json",
            "regime_raw_inputs",
            "engine_challenger_variants",
            "reconstructed_memory_snapshot_rows",
        ):
            if key in enriched:
                row[key] = enriched[key]
    atomic_json(root / "derived/checkpoints.json", clean(rows))
    atomic_json(root / "derived/sensitivity-records.json", clean(sensitivities))
    save_records(root, rows)
    report = summary(rows)
    report.update(
        {
            "research_version": ENRICHMENT_VERSION,
            "old_analysis_superseded_for_engine_decisions": True,
            "certified_oos": False,
            "complete_actual_target_days": len(
                {r["target_date"] for r in rows if r.get("actual_complete")}
            ),
            "taf_unique_bulletins": len(tafs),
            "taf_decoder_failure_count": int(tafs.decode_error.notna().sum()),
            "pressure_non_null_rows": int(pressure.temp_850hpa_c.notna().sum()),
            "engine_sha256": engine_fingerprint(Path(__file__).resolve().parents[1]),
        }
    )
    atomic_json(root / "results/enriched-replay-summary.json", clean(report))
    old_map = {(r["target_date"], r["checkpoint_label"]): r for r in old}
    comparison = []
    for r in rows:
        before = old_map[(r["target_date"], r["checkpoint_label"])]
        comparison.append(
            {
                "target_date": r["target_date"],
                "checkpoint": r["checkpoint_label"],
                "old_center": before.get("champion_center_c"),
                "new_center": r.get("champion_center_c"),
                "old_modal": before.get("champion_modal_bucket"),
                "new_modal": r.get("champion_modal_bucket"),
            }
        )
    atomic_json(root / "results/old-vs-enriched.json", comparison)
    sensitivity_summary = []
    by_key = {(r["target_date"], r["checkpoint_label"]): r for r in rows}
    for name in sorted({s["scenario"] for s in sensitivities}):
        for label, _, _ in __import__(
            "weatherman.historical_replay", fromlist=["CHECKPOINTS"]
        ).CHECKPOINTS:
            cases = [
                s for s in sensitivities if s["scenario"] == name and s["checkpoint_label"] == label
            ]
            paired = [
                s
                for s in cases
                if s.get("champion_center_c") is not None
                and by_key[(s["target_date"], s["checkpoint_label"])].get("champion_center_c")
                is not None
            ]
            matched = [by_key[(s["target_date"], s["checkpoint_label"])] for s in paired]
            sensitivity_summary.append(
                {
                    "scenario": name,
                    "checkpoint": label,
                    "requested": len(cases),
                    "unavailable": sum(s.get("champion_center_c") is None for s in cases),
                    "matched_forecasts": len(paired),
                    "modal_changed": sum(
                        s.get("champion_modal_bucket") != s["baseline_modal_bucket"] for s in paired
                    ),
                    "mean_absolute_center_change_c": sum(
                        abs(s["champion_center_c"] - s["baseline_center_c"]) for s in paired
                    )
                    / len(paired)
                    if paired
                    else None,
                    "matched_baseline": metrics(matched),
                    "matched_scenario": metrics(paired),
                }
            )
    atomic_json(root / "results/sensitivity-summary.json", clean(sensitivity_summary))
    if not dates:
        plan = json.loads((source / "results/research-stage-plan.json").read_text())
        policy_report, records = study(
            rows, plan["holdout_start"], set(plan["excluded_dry_run_dates"])
        )
        atomic_json(root / "results/full-engine-policy-study.json", clean(policy_report))
        atomic_json(root / "derived/full-policy-walk-forward.json", clean(records))
    print(
        json.dumps(
            {
                "days": len(targets),
                "checkpoints": len(rows),
                "status": "completed_partial_enrichment",
            },
            indent=2,
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--mode", choices=["acquire", "warmup", "replay", "all"], default="all")
    parser.add_argument("--max-requests", type=int, default=1500)
    parser.add_argument("--dates", help="Optional registered diagnostic sample, comma separated")
    args = parser.parse_args()
    source = args.source.resolve()
    root = args.archive.resolve()
    repo = Path(__file__).resolve().parents[1]
    if root == source or root == repo or repo in root.parents:
        parser.error("Update must use a separate archive outside repository")
    root.mkdir(parents=True, exist_ok=True)
    (root / "results").mkdir(exist_ok=True)
    # Preserve immutable source payloads while the update owns its separate request ledger.
    raw = root / "raw"
    raw.mkdir(exist_ok=True)
    for p in (source / "raw").iterdir():
        if not (raw / p.name).exists():
            os.link(p, raw / p.name)
    if not (root / "backfill_state.json").exists():
        atomic_json(
            root / "backfill_state.json", json.loads((source / "backfill_state.json").read_text())
        )
    airport = json.loads((repo / "src/weatherman/data/airports.json").read_text())["LEMD"]
    atomic_json(
        root / "results/update-design.json",
        {
            "research_only": True,
            "automatic_promotion": False,
            "source_archive": str(source),
            "old_results_preserved": True,
            "prior_evaluation_period_already_viewed": True,
            "fixed_sensitivities": [
                "publication_extra_60m",
                "publication_extra_120m",
                "metar_taf_extra_10m",
                "complete_single_run_only",
                "alternative_lead48_warmup",
            ],
            "warmup_lead48_not_mixed_into_primary": True,
            "public_challenger_contract_unchanged": True,
            "production_reads": 0,
            "production_writes": 0,
            "protected_engine_sha256": engine_fingerprint(repo),
        },
    )
    if args.mode == "warmup":
        independent = root / "warmup-source"
        independent.mkdir(exist_ok=True)
        cache = RawCache(independent, max_requests=5)
        try:
            acquire_previous(cache, root, airport)
            for path in (independent / "raw").glob("*"):
                if not (raw / path.name).exists():
                    os.link(path, raw / path.name)
        finally:
            cache.close()
    if args.mode in {"acquire", "all"}:
        acquire(root, source, airport, args.max_requests)
    if args.mode in {"replay", "all"}:
        replay(root, source, airport, set(args.dates.split(",")) if args.dates else None)


if __name__ == "__main__":
    main()
