"""Offline-only Historical Replay Analysis v1; never acquires source data."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import socket
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

from weatherman.historical_analysis import (
    FLAGS,
    SCENARIOS,
    VERSION,
    _RUNTIME,
    analysis_engine,
    context,
    no_parameter_effect,
    prediction,
    prepared_builders,
    registry,
)
from weatherman.historical_archive import ArchiveError, atomic_json, digest
from weatherman.historical_enrichment import shift_availability
from weatherman.historical_replay import clean, schedule
from weatherman.decision import DayStatus

_CONTEXT = None


def prohibit_network():
    original = socket.socket.connect

    def local_only(instance, address):
        if instance.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("Offline analysis: network connections are prohibited")
        return original(instance, address)

    socket.socket.connect = local_only
    original_ex = socket.socket.connect_ex

    def local_only_ex(instance, address):
        if instance.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("Offline analysis: network connections are prohibited")
        return original_ex(instance, address)

    socket.socket.connect_ex = local_only_ex


def frozen_snapshots(archive, rows):
    manifest = json.loads((archive / "results/replay-input-manifest.json").read_text())
    directory = archive / "derived" / manifest["fingerprint"] / "days"
    if directory.is_dir():
        originals = [
            r
            for path in sorted(directory.glob("*.json"))
            for r in json.loads(path.read_text())["records"]
        ]
    else:
        # Meteorological snapshot values are unchanged by shadow-only enrichment.
        originals = rows
    return [
        {
            "target_date": r["target_date"],
            "captured_at": r["checkpoint_at"],
            "final_forecast_c": r["champion_center_c"],
            "temp_anchor_adjustment_c": r.get("temp_anchor_adjustment_c"),
            "features_json": json.dumps(r.get("features_json", {})),
            "hours_to_peak": r.get("features_json", {}).get("hours_to_peak"),
            "day_phase": r.get("terminal_state", {}).get("phase"),
        }
        for r in originals
        if r.get("champion_center_c") is not None
    ]


def _cached_main(gold, parameters):
    if len(parameters) != 1:
        return None
    factor, value = next(iter(parameters.items()))
    if gold["checkpoint_label"].startswith("D0") and factor == "temperature_anchor":
        names = {
            0: "anchor_disabled",
            0.25: "anchor_25pct",
            0.5: "anchor_50pct",
            0.75: "anchor_75pct",
            None: "anchor_capped_0_10c",
        }
        return gold.get("full_policy_forecasts", {}).get(names.get(value))
    if gold["checkpoint_label"].startswith("Late") and factor in {
        "clear_sky_override",
        "taf",
        "late_dry_mixing",
    }:
        weights = {"clear_sky_override": 100, "taf": 100, "late_dry_mixing": 100}
        weights[factor] = round(value * 100)
        name = f"clear_{weights['clear_sky_override']}_taf_{weights['taf']}_dry_{weights['late_dry_mixing']}"
        return gold.get("full_policy_forecasts", {}).get(name)
    return None


def one_day(day_string):
    (
        archive,
        source,
        directory,
        airport,
        actuals,
        history,
        previous,
        snapshots,
        gold_map,
        specs,
        validate,
    ) = _CONTEXT
    saved = directory / f"{day_string}.json"
    if saved.exists():
        return json.loads(saved.read_text())
    started = time.monotonic()
    day = date.fromisoformat(day_string)
    paths = pd.read_parquet(archive / f"normalized/{day}-model-paths.parquet")
    observations = pd.read_parquet(source / f"normalized/{day}-metar.parquet")
    tafs = pd.read_parquet(archive / f"normalized/{day}-taf.parquet")
    previous_history = pd.concat([previous, history], ignore_index=True)
    variants = {
        "main": {},
        "alternative_lead48_warmup": {"history_forecasts": previous_history},
        "complete_single_run_only": {"include_stitched": False},
        "metar_taf_extra_10m": {
            "observations": shift_availability(observations, 10),
            "tafs": shift_availability(tafs, 10),
        },
        "publication_extra_60m": {"paths": shift_availability(paths, 60)},
        "publication_extra_120m": {"paths": shift_availability(paths, 120)},
    }
    output = []
    calls = skips = reused = 0
    prior_terminal = None
    for label, cutoff in schedule(day):
        gold = gold_map[(day_string, label)]
        for scenario in SCENARIOS:
            build, candidate_build = prepared_builders()
            kwargs = dict(
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
                snapshot_history=snapshots,
                prior_terminal_status=prior_terminal,
                nowcast_builder=analysis_engine("{}"),
                # Native variants are already cached for the main cohort. They
                # are not part of this parameter/sensitivity forecast matrix.
                build_challengers=False,
            )
            kwargs.update(variants[scenario])
            computed = build(**kwargs)
            calls += 1
            capture = dict(_RUNTIME["capture"])
            if scenario == "main":
                if computed.get("champion_center_c") != gold.get(
                    "champion_center_c"
                ) or computed.get("champion_probabilities") != gold.get("champion_probabilities"):
                    raise ArchiveError(f"v0.3 protected baseline changed at {day}/{label}")
                baseline = context(gold)
            else:
                baseline = context(computed)
            policies = {}
            for spec in specs[label]:
                if spec["kind"] != "engine":
                    continue
                name, params = spec["id"], spec["parameters"]
                if baseline.get("champion_center_c") is None or no_parameter_effect(
                    params, capture
                ):
                    selected = baseline
                    skips += 1
                else:
                    cached = _cached_main(gold, params) if scenario == "main" else None
                    if cached is not None:
                        selected = cached
                        reused += 1
                    else:
                        selected = candidate_build(
                            **{
                                **kwargs,
                                "build_challengers": False,
                                "nowcast_builder": analysis_engine(
                                    json.dumps(params, sort_keys=True)
                                ),
                            }
                        )
                        calls += 1
                policies[name] = prediction(
                    selected,
                    selected.get("champion_center_c"),
                    selected.get("champion_probabilities", {}),
                    policy=name,
                )
            # Identity and cached prefix checks on all four CPs and six scenarios of registered samples.
            if validate and baseline.get("champion_center_c") is not None:
                _RUNTIME["enabled"] = False
                direct = candidate_build(**{**kwargs, "build_challengers": False})
                _RUNTIME["enabled"] = True
                if direct.get("champion_center_c") != computed.get(
                    "champion_center_c"
                ) or direct.get("champion_probabilities") != computed.get("champion_probabilities"):
                    raise ArchiveError("Memoized calibration prefix differs from uncached engine")
            output.append(
                {
                    "scenario": scenario,
                    "baseline": clean(baseline),
                    "policy_forecasts": clean(policies),
                    "raw_weight_contributions": capture,
                }
            )
        if gold.get("terminal_state"):
            prior_terminal = DayStatus(**gold["terminal_state"])
    result = {
        "target_date": day_string,
        "rows": output,
        "full_engine_calls": calls,
        "exact_zero_effect_reuses": skips,
        "v03_cached_policy_reuses": reused,
        "elapsed_seconds": time.monotonic() - started,
    }
    atomic_json(saved, clean(result))
    print(
        f"Cross {day}: {calls} engine calls, {skips} exact reuses, {time.monotonic() - started:.1f}s",
        flush=True,
    )
    return result


def acquire_context(args, targets, rows, specs):
    archive, source, output = args.archive.resolve(), args.source.resolve(), args.output.resolve()
    repo = Path(__file__).resolve().parents[1]
    snapshot_path = output / "inputs/v03-memory-snapshots.json"
    if not snapshot_path.exists():
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(snapshot_path, frozen_snapshots(archive, rows))
    snapshots = pd.DataFrame(json.loads(snapshot_path.read_text()))
    history_paths = sorted((source / "normalized/warmup").glob("*.parquet"))
    inputs = [
        archive / "derived/checkpoints.json",
        archive / "normalized/previous-lead48.parquet",
        archive / "markets/checkpoint-price-series.json",
        snapshot_path,
        source / "normalized/2026-04-01-actuals.parquet",
        *history_paths,
    ]
    for day in targets:
        inputs.extend(
            [
                archive / f"normalized/{day}-model-paths.parquet",
                archive / f"normalized/{day}-taf.parquet",
                source / f"normalized/{day}-metar.parquet",
            ]
        )
    hashes = {str(p): digest(p.read_bytes()) for p in inputs}
    original = json.loads((archive / "results/enriched-replay-summary.json").read_text())[
        "engine_sha256"
    ]
    code = {name: digest((repo / name).read_bytes()) for name in original}
    code.update(
        {
            "src/weatherman/historical_analysis.py": digest(
                Path(
                    __import__("weatherman.historical_analysis", fromlist=["VERSION"]).__file__
                ).read_bytes()
            ),
            "scripts/historical_replay_analysis.py": digest(Path(__file__).read_bytes()),
        }
    )
    fingerprint = digest(
        json.dumps(
            {"inputs": hashes, "code": code, "targets": targets, "specs": specs}, sort_keys=True
        ).encode()
    )
    directory = output / "derived" / fingerprint / "days"
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": VERSION,
        "fingerprint": fingerprint,
        "inputs": hashes,
        "code": code,
        "source_commit": "4dea9232869eecf85b209fc0ee1d35410549efb4",
        "targets": targets,
        "registry": specs,
        "network_requests": 0,
        "production_reads": 0,
        "production_writes": 0,
        **FLAGS,
        "source_status_counts": dict(__import__("collections").Counter(r["status"] for r in rows)),
        "state_policy": "fixed_v03_prior_terminal_and_snapshot_state; no altered prior decisions",
    }
    atomic_json(output / "analysis-input-manifest.json", manifest)
    airport = json.loads((repo / "src/weatherman/data/airports.json").read_text())["LEMD"]
    history = pd.concat([pd.read_parquet(p) for p in history_paths], ignore_index=True)
    actuals = pd.read_parquet(source / "normalized/2026-04-01-actuals.parquet")
    previous = pd.read_parquet(archive / "normalized/previous-lead48.parquet")
    return (
        archive,
        source,
        directory,
        airport,
        actuals,
        history,
        previous,
        snapshots,
        {(r["target_date"], r["checkpoint_label"]): r for r in rows},
        specs,
        bool(args.dates),
    ), manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("cross", "analyze", "all"), default="all")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dates", help="Registered diagnostic sample; not a new holdout")
    args = parser.parse_args()
    root = args.output.resolve()
    repo = Path(__file__).resolve().parents[1]
    if root in {args.archive.resolve(), args.source.resolve(), repo} or repo in root.parents:
        parser.error("Analysis output must be separate from source archives and repository")
    root.mkdir(parents=True, exist_ok=True)
    prohibit_network()
    if args.mode in {"cross", "all"}:
        rows = json.loads((args.archive / "derived/checkpoints.json").read_text())
        targets = sorted({r["target_date"] for r in rows})
        if args.dates:
            selected = set(args.dates.split(","))
            targets = [d for d in targets if d in selected]
        specs = registry()
        global _CONTEXT
        _CONTEXT, manifest = acquire_context(args, targets, rows, specs)
        with ProcessPoolExecutor(
            max_workers=args.workers, mp_context=multiprocessing.get_context("fork")
        ) as pool:
            days = list(pool.map(one_day, targets))
        records = [r for d in days for r in d["rows"]]
        atomic_json(root / "derived/cross-check-records.json", clean(records))
        for path, expected in manifest["inputs"].items():
            if digest(Path(path).read_bytes()) != expected:
                raise ArchiveError(f"Source input changed during analysis: {path}")
        atomic_json(
            root / "cross-execution-summary.json",
            {
                "days": len(days),
                "rows": len(records),
                "source_immutability_verified": True,
                "network_requests": 0,
                "full_engine_calls": sum(d["full_engine_calls"] for d in days),
                "exact_zero_effect_reuses": sum(d["exact_zero_effect_reuses"] for d in days),
                "cached_v03_policy_reuses": sum(d["v03_cached_policy_reuses"] for d in days),
                **FLAGS,
            },
        )
    if args.mode in {"analyze", "all"}:
        from weatherman.historical_analysis_reports import analyze

        analyze(root, args.archive.resolve())


if __name__ == "__main__":
    main()
