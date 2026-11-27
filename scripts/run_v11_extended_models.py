"""Separate eight-model April–October experiment; never pool with the long replay."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
from statistics import mean, pstdev

from weatherman.v11.contracts import distribution, instant, normal_buckets, require_training_labels, score
from weatherman.v11.evidence import MODELS, read_rows, sha, write_rows


EXTENDED = MODELS + ("meteofrance_arome_france", "meteofrance_arome_france_hd")
SPLITS = {"discovery": ["2026-04-01", "2026-06-30"],
          "calibration": ["2026-07-01", "2026-07-31"],
          "historical_validation": ["2026-08-01", "2026-10-08"]}


def normalize_extended(path):
    source_sha = sha(path)
    rows, inventory = [], defaultdict(lambda: {"rows": 0, "variables": defaultdict(int)})
    for x in json.loads(path.read_text()):
        if not x.get("actual_complete") or not x.get("champion_probabilities"):
            continue
        cutoff = instant(x["checkpoint_at"])
        selected = {}
        for m in x.get("model_inputs", []):
            if m.get("max_temp_c") is None or not m.get("model_run_at") or not m.get("available_at"):
                continue
            if not instant(m["model_run_at"]) <= instant(m["available_at"]) <= cutoff:
                continue
            key = m["model"]
            if key not in selected or instant(selected[key]["model_run_at"]) < instant(m["model_run_at"]):
                selected[key] = m
        for model, m in selected.items():
            inventory[model]["rows"] += 1
            for var in ("temp_c", "dewpoint_c", "wind_kph", "wind_direction", "radiation_wm2", "temp_850hpa_c"):
                inventory[model]["variables"][var] += int(m.get(var) is not None)
        rows.append({"target_date": x["target_date"], "checkpoint": x["checkpoint_label"],
                     "checkpoint_at": x["checkpoint_at"], "actual": x["actual_bucket"],
                     "actual_available_at": x["actual_available_at"],
                     "models": {k: m["max_temp_c"] for k, m in selected.items()},
                     "champion": x["champion_probabilities"], "champion_center": x["champion_center_c"],
                     "evidence_class": "historical_reduced_fidelity", "source_sha256": source_sha})
    return rows, dict(inventory)


def experiment(rows, output):
    from weatherman.v11.bakeoff import summarize
    train = [r for r in rows if SPLITS["discovery"][0] <= r["target_date"] <= SPLITS["discovery"][1]
             and all(m in r["models"] for m in EXTENDED)]
    groups, frozen, predictions = defaultdict(list), {}, []
    for cp in sorted({r["checkpoint"] for r in train}):
        part = [r for r in train if r["checkpoint"] == cp]
        if len(part) < 60:
            continue
        require_training_labels(part, "2026-07-01T00:00:00Z")
        residuals = {m: [r["actual"] - r["models"][m] for r in part] for m in EXTENDED}
        weights = {m: 1 / max(.05, mean(abs(x) for x in e)) for m, e in residuals.items()}
        total = sum(weights.values())
        weights = {m: v / total for m, v in weights.items()}
        best = min(EXTENDED, key=lambda m: mean(abs(x) for x in residuals[m]))
        params = {m: {"bias": mean(e), "sigma": max(.35, pstdev(e))} for m, e in residuals.items()}
        def raw(row, weighting):
            return (mean(row["models"][m] for m in EXTENDED) if weighting == "equal" else
                    sum(weights[m] * row["models"][m] for m in EXTENDED))
        mix = {mode: {"bias": mean(r["actual"] - raw(r, mode) for r in part),
                      "sigma": max(.35, pstdev([r["actual"] - raw(r, mode) for r in part]))} for mode in ("equal", "inverse_mae")}
        frozen[cp] = {"n_train": len(part), "models": params, "weights": weights,
                      "best_training_single": best, "mixture_residuals": mix}
        for row in rows:
            if row["checkpoint"] != cp or row["target_date"] < SPLITS["calibration"][0] or not all(m in row["models"] for m in EXTENDED):
                continue
            phase = "calibration" if row["target_date"] <= SPLITS["calibration"][1] else "historical_validation"
            predicted = {"champion": distribution(row["champion"])}
            for m in EXTENDED:
                predicted[m] = normal_buckets(row["models"][m] + params[m]["bias"], params[m]["sigma"])
                predicted[m + ":raw"] = normal_buckets(row["models"][m], params[m]["sigma"])
            predicted["best_training_single"] = predicted[best]
            for mode in mix:
                predicted[mode + ":residual"] = normal_buckets(raw(row, mode) + mix[mode]["bias"], mix[mode]["sigma"])
            for name, probs in predicted.items():
                groups[phase, cp, name].append(score(probs, row["actual"]))
                predictions.append({"day": row["target_date"], "checkpoint": cp, "candidate": name,
                                    "phase": phase, "probabilities": probs})
    result = {"status": "NO RELEASE CANDIDATE", "splits": SPLITS,
              "frozen_models": frozen,
              "limitations": ["Different period/fidelity from deep cohort; do not pool",
                              "Exact initialization preserved, publication time estimated",
                              "No Meteoblue historical product or true ensemble members"],
              "summaries": [{"phase": phase, "checkpoint": cp, "candidate": name, **summarize(records)}
                            for (phase, cp, name), records in sorted(groups.items())]}
    output.mkdir(parents=True, exist_ok=True)
    (output / "extended_results.json").write_text(json.dumps(result, indent=2) + "\n")
    write_rows(output / "extended_predictions.jsonl.gz", predictions)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoints", type=Path)
    parser.add_argument("--rows", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.rows) == bool(args.checkpoints):
        parser.error("Choose exactly one of --rows / --checkpoints")
    if args.rows:
        rows = read_rows(args.rows)
    else:
        rows, inventory = normalize_extended(args.checkpoints)
        args.output.mkdir(parents=True, exist_ok=True)
        write_rows(args.output / "extended_rows.jsonl.gz", rows)
        (args.output / "extended_manifest.json").write_text(json.dumps(inventory, indent=2) + "\n")
    result = experiment(rows, args.output)
    print(json.dumps({"status": result["status"], "train_counts": {k: v["n_train"] for k, v in result["frozen_models"].items()}}))


if __name__ == "__main__":
    main()
