"""Reproduce v11 entirely from local immutable files; never contacts production."""
import argparse
import json
from pathlib import Path

from weatherman.v11.evidence import FEATURES, normalize, read_rows, sha, write_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep", type=Path, help="Unpacked Deep Seasonal archive")
    parser.add_argument("--daily", type=Path, help="Unpacked release-review inputs directory")
    parser.add_argument("--rows", type=Path, help="Previously normalized rows.jsonl.gz")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--normalize-only", action="store_true")
    args = parser.parse_args()
    if args.rows:
        if args.deep or args.daily:
            parser.error("Choose --rows or --deep/--daily")
        rows = read_rows(args.rows)
    else:
        if not args.deep or not args.daily:
            parser.error("Both --deep and --daily required")
        rows, inventory = normalize(args.deep, args.daily)
        args.output.mkdir(parents=True, exist_ok=True)
        write_rows(args.output / "rows.jsonl.gz", rows)
        inventory["normalized_sha256"] = sha(args.output / "rows.jsonl.gz")
        (args.output / "input_manifest.json").write_text(json.dumps(inventory, indent=2) + "\n")
        (args.output / "features.json").write_text(json.dumps({
            "version": "v11_features_1", "features": FEATURES,
            "target": "final stored METAR integer maximum; never physical AEMET or official market resolution",
            "temperature_and_direction_units": "Celsius / degrees clockwise from north",
            "wind_unit": "km/h", "time_unit": "minutes unless feature name heating (C/hour)",
            "model_radiation": "modeled W/m2, not measured energy",
            "missing": "null; imputation fit on discovery only; indicators preserved",
            "future_peak_and_final_actual": "excluded from model features",
            "solar_vs_nonsolar": "wind/moisture and radiation ablations; no inferred physical causal attribution",
        }, indent=2) + "\n")
    if not args.normalize_only:
        from weatherman.v11.bakeoff import run
        report = run(rows, args.output)
        print(json.dumps({"status": report["status"], "selected": report["frozen_selection"],
                          "skipped": report["skipped"]}, indent=2))


if __name__ == "__main__":
    main()
