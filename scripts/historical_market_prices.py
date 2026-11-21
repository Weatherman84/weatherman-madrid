"""Public historical price-series archive. No order-book execution or trading claims."""

from __future__ import annotations
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
import pandas as pd
from weatherman.historical_archive import RawCache, ArchiveError, atomic_json, utc
from weatherman.historical_replay import clean, schedule


def historical_point(history, cutoff, max_age_minutes=30):
    eligible = [p for p in history if int(p["t"]) <= int(utc(cutoff).timestamp())]
    if not eligible:
        return None
    point = max(eligible, key=lambda p: int(p["t"]))
    age = (utc(cutoff).timestamp() - int(point["t"])) / 60
    return {
        "series_price": float(point["p"]),
        "price_at": pd.to_datetime(point["t"], unit="s", utc=True).isoformat(),
        "age_minutes": age,
        "fresh": age <= max_age_minutes,
        "executable_quote": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--start", type=date.fromisoformat, default=date(2026, 4, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 8))
    a = p.parse_args()
    root = a.archive.resolve() / "markets"
    root.mkdir(parents=True, exist_ok=True)
    cache = RawCache(root, max_requests=500)
    days = [a.start + timedelta(days=i) for i in range((a.end - a.start).days + 1)]
    records = []
    errors = []
    events = []

    def one(day):
        destination = root / "days" / f"{day}.json"
        if destination.exists():
            return json.loads(destination.read_text())
        slug = f"highest-temperature-in-madrid-on-{day.strftime('%B').lower()}-{day.day}-{day.year}"
        try:
            data, meta = cache.get(
                "https://gamma-api.polymarket.com/events/slug/" + slug, {}, kind="market_json"
            )
            event = json.loads(data)
            markets = event.get("markets", [])
            if not markets:
                raise ArchiveError("No event markets")
            yes_tokens = []
            selected = []
            for market in markets:
                outcomes = market.get("outcomes", [])
                outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                tokens = market.get("clobTokenIds", [])
                tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
                if "Yes" not in outcomes or len(outcomes) != len(tokens):
                    continue
                index = outcomes.index("Yes")
                token = tokens[index]
                yes_tokens.append(token)
                outcome_prices = market.get("outcomePrices", [])
                outcome_prices = (
                    json.loads(outcome_prices)
                    if isinstance(outcome_prices, str)
                    else outcome_prices
                )
                selected.append(
                    {
                        "token_id": token,
                        "bucket_label": market.get("groupItemTitle"),
                        "condition_id": market.get("conditionId"),
                        "resolved_yes": bool(
                            market.get("closed")
                            and len(outcome_prices) > index
                            and float(outcome_prices[index]) == 1
                        ),
                        "resolution_status": market.get("umaResolutionStatus"),
                        "resolution_source": market.get("resolutionSource")
                        or event.get("resolutionSource"),
                    }
                )
            start = int(pd.Timestamp(day - timedelta(days=2), tz="Europe/Madrid").timestamp())
            stop = int(pd.Timestamp(day + timedelta(days=1), tz="Europe/Madrid").timestamp())
            data, hmeta = cache.get(
                "https://clob.polymarket.com/batch-prices-history",
                {"markets": yes_tokens, "start_ts": start, "end_ts": stop, "fidelity": 5},
                kind="market_json",
                read_only_post=True,
            )
            payload = json.loads(data)
            histories = payload.get("history", {})
            if not isinstance(histories, dict):
                raise ArchiveError("Unexpected batch price history")
            output = []
            for market in selected:
                series = histories.get(market["token_id"], [])
                for checkpoint, cutoff in schedule(day):
                    output.append(
                        {
                            "target_date": str(day),
                            "checkpoint_label": checkpoint,
                            "checkpoint_at": utc(cutoff).isoformat(),
                            **market,
                            "historical_point": historical_point(series, cutoff),
                            "series_points": len(series),
                            "source_sha256": hmeta["sha256"],
                            "event_source_sha256": meta["sha256"],
                            "research_only": True,
                            "automatic_promotion": False,
                        }
                    )
            value = {
                "target_date": str(day),
                "event_slug": slug,
                "markets": len(selected),
                "price_points": sum(len(v) for v in histories.values()),
                "records": output,
                "error": None,
            }
        except ArchiveError as exc:
            value = {"target_date": str(day), "records": [], "error": str(exc)}
        atomic_json(destination, value)
        return value

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for i, value in enumerate(pool.map(one, days), 1):
                events.append({k: v for k, v in value.items() if k != "records"})
                records.extend(value["records"])
                if value.get("error"):
                    errors.append({"date": value["target_date"], "error": value["error"]})
                if i % 20 == 0:
                    print(f"Market archive {i}/{len(days)}", flush=True)
        atomic_json(root / "checkpoint-price-series.json", clean(records))
        atomic_json(
            root / "coverage.json",
            {
                "days_requested": len(days),
                "events_available": sum(not e.get("error") for e in events),
                "events": events,
                "errors": errors,
                "bucket_checkpoint_rows": len(records),
                "points_at_or_before_checkpoint": sum(
                    r["historical_point"] is not None for r in records
                ),
                "fresh_points_at_or_before_checkpoint": sum(
                    r["historical_point"] is not None and r["historical_point"]["fresh"]
                    for r in records
                ),
                "price_series_not_executable_quotes": True,
                "pnl_status": "not_calculated_no_historical_executable_bid_ask_depth",
                "requests": cache.requests,
                "bytes": cache.download_bytes,
                "production_reads": 0,
                "production_writes": 0,
            },
        )
    finally:
        cache.close()


if __name__ == "__main__":
    main()
