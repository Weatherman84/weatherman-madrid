"""Historical price-series proxy benchmark, explicitly not an executable backtest."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re

from weatherman.v11.contracts import distribution, instant, score
from weatherman.v11.evidence import read_rows, sha


def bounds(label):
    numbers = re.findall(r"-?\d+", label)
    if len(numbers) != 1 or "°C" not in label:
        raise ValueError("Unrecognized market bounds: " + label)
    n = int(numbers[0])
    if "below" in label.lower():
        return None, n
    if "higher" in label.lower() or "above" in label.lower():
        return n, None
    return n, n


def index_for(value, intervals):
    matched = [i for i, (lo, hi) in enumerate(intervals) if (lo is None or value >= lo) and
               (hi is None or value <= hi)]
    if len(matched) != 1:
        raise ValueError("Incomplete/overlapping market partition")
    return matched[0]


def benchmark(prices, rows, predictions, selected):
    from weatherman.v11.bakeoff import summarize
    groups = defaultdict(list)
    for q in prices:
        groups[q["target_date"], q["checkpoint_label"]].append(q)
    rowmap = {(r["target_date"], r["checkpoint"]): r for r in rows}
    vectors = {(v["day"], v["checkpoint"]): v for v in predictions if v["candidate"] == selected.get(v["checkpoint"])}
    metrics, excluded, alignment = defaultdict(list), defaultdict(int), defaultdict(int)
    for key, qs in sorted(groups.items()):
        if key not in vectors or key not in rowmap:
            continue
        r, v = rowmap[key], vectors[key]
        if any(q.get("resolution_status") != "resolved" for q in qs):
            excluded["unresolved"] += 1
            continue
        if any(not (q.get("historical_point") or {}).get("fresh") or (q.get("historical_point") or {}).get("series_price") is None for q in qs):
            excluded["missing_or_stale_price"] += 1
            continue
        if any(not 0 <= (instant(r["checkpoint_at"]) - instant(q["historical_point"]["price_at"])).total_seconds() <= 15 * 60 for q in qs):
            excluded["price_after_cutoff_or_stale"] += 1
            continue
        if len({q["condition_id"] for q in qs}) != len(qs):
            excluded["duplicate_condition"] += 1
            continue
        qs = sorted(qs, key=lambda q: float("-inf") if bounds(q["bucket_label"])[0] is None else bounds(q["bucket_label"])[0])
        intervals = [bounds(q["bucket_label"]) for q in qs]
        winners = [i for i, q in enumerate(qs) if q.get("resolved_yes") is True]
        if len(winners) != 1:
            excluded["nonunique_resolution"] += 1
            continue
        try:
            model = {i: 0.0 for i in range(len(qs))}
            for k, p in distribution(v["probabilities"]).items():
                model[index_for(k, intervals)] += p
            stored = index_for(r["actual"], intervals)
        except ValueError:
            excluded["incomplete_partition"] += 1
            continue
        if any(not 0 <= q["historical_point"]["series_price"] <= 1 for q in qs):
            excluded["invalid_price"] += 1
            continue
        total = sum(q["historical_point"]["series_price"] for q in qs)
        if total <= 0:
            excluded["zero_price_sum"] += 1
            continue
        market = {i: q["historical_point"]["series_price"] / total for i, q in enumerate(qs)}
        alignment["same_stored_and_official_category" if stored == winners[0] else "different_stored_and_official_category"] += 1
        for target, actual in (("stored_metar_proxy", stored), ("reported_official_resolution_cross_target", winners[0])):
            for name, probs in (("weather_frozen_selection", model), ("normalized_market_series", market)):
                metrics[v["phase"], r["checkpoint"], target, name].append(score(probs, actual))
    return {"status": "NON_EXECUTABLE_CROSS_TARGET_DIAGNOSTIC", "official_roi": None,
            "trading_edge_proven": False, "target_alignment": dict(alignment), "excluded": dict(excluded),
            "limitations": ["Price series are not best ask or bid/ask books",
                            "No historical fees, depth, or fills; no ROI",
                            "Market WU resolution and stored METAR are distinct targets",
                            "Historical candidate selection was not a contemporary frozen signal"],
            "summaries": [{"phase": p, "checkpoint": cp, "target": target,
                           "candidate": name, **summarize(items)} for (p, cp, target, name), items in sorted(metrics.items())]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    prices = json.loads(args.prices.read_text())
    rows = read_rows(args.artifacts / "rows.jsonl.gz")
    predictions = read_rows(args.artifacts / "predictions.jsonl.gz")
    selected = json.loads((args.artifacts / "results.json").read_text())["frozen_selection"]
    result = benchmark(prices, rows, predictions, selected)
    result["source_sha256"] = sha(args.prices)
    (args.artifacts / "market_proxy.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "alignment": result["target_alignment"]}))


if __name__ == "__main__":
    main()
