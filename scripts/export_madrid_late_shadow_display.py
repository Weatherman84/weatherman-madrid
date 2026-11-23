"""Export immutable research probabilities for the read-only app display."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path

from weatherman.madrid_late_shadow_display import VERSION, load_bundle


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--study", type=Path, default=ROOT / "research/v11_madrid_curve_feature_v1/artifacts"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "src/weatherman/data/madrid_late_curve_shadow_v1.json"
    )
    args = parser.parse_args()
    expected = json.loads((args.study / "hashes.json").read_text())
    if any(digest(args.study / n) != d for n, d in expected.items()):
        raise ValueError("Source research artifacts changed")

    def rows(name):
        return [
            json.loads(s)
            for s in gzip.decompress((args.study / name).read_bytes()).decode().splitlines()
        ]

    source = {(r["target_date"], r["checkpoint"]): r for r in rows("rows.jsonl.gz")}
    vectors = rows("predictions.jsonl.gz")
    by_key = {}
    for v in vectors:
        key = v["day"], v["checkpoint"], v["candidate"]
        if key in by_key:
            raise ValueError("Duplicate research vector")
        by_key[key] = v
    cases = []
    for day, cp in sorted({(v["day"], v["checkpoint"]) for v in vectors}):
        if cp != "Late Live @16:00":
            raise ValueError("Only Late research display permitted")
        r = source[day, cp]
        cases.append(
            {
                "target_date": day,
                "cutoff": r["checkpoint_at"],
                "actual": r["actual"],
                "known_max": r["observed_max"],
                "source_evidence": r["evidence_class"],
                "curve_available": r["curve_available"],
                "candidate": by_key[day, cp, "plus_curve"]["probabilities"],
                "reference": by_key[day, cp, "reference_TX"]["probabilities"],
                "champion": r["champion"],
            }
        )
    results = json.loads((args.study / "results.json").read_text())
    summaries = {
        r["candidate"]: r
        for r in results["summaries"]
        if r["phase"] == "historical_validation" and r["dimension"] == "all"
    }
    bundle = {
        "schema": "madrid-late-shadow-display-1",
        "candidate_id": VERSION,
        "airport": "LEMD",
        "research_only": True,
        "automatic_promotion": False,
        "prospective_OOS_days": 0,
        "parameter_sha256": results["new_parameter_sha256"],
        "source_predictions_sha256": digest(args.study / "predictions.jsonl.gz"),
        "source_freeze_sha256": digest(args.study / "frozen_models.json"),
        "benchmark": {
            "candidate": {
                k: summaries["plus_curve"][k] for k in ["n", "top1", "brier", "log_loss", "mae"]
            },
            "reference": {
                k: summaries["reference_TX"][k] for k in ["n", "top1", "brier", "log_loss", "mae"]
            },
        },
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(bundle, separators=(",", ":"), allow_nan=False) + "\n")
    args.output.with_suffix(".sha256").write_text(
        digest(args.output) + "  " + args.output.name + "\n"
    )
    load_bundle(args.output)
    print(
        json.dumps(
            {
                "cases": len(cases),
                "curve_cases": sum(r["curve_available"] for r in cases),
                "fallback_cases": sum(not r["curve_available"] for r in cases),
                "bytes": args.output.stat().st_size,
                "sha256": digest(args.output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
