"""Cycle orchestration and read models for the six-airport shadow portfolio."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .opportunity_scanner import (
    checkpoint_schedule, evaluate_trigger, expected_peak_time, load_airport_config,
    load_policy, make_market_snapshot, make_shadow_trade, matching_rules, no_signal_record,
)
from .opportunity_store import OpportunityStore


def _market_for(
    markets: Iterable[Mapping[str, Any]], airport: str, target_date: str, bucket: int,
) -> dict[str, Any] | None:
    candidates = [dict(row) for row in markets if row.get("airport") == airport
                  and str(row.get("target_date"))[:10] == target_date
                  and row.get("bucket") is not None and int(row["bucket"]) == bucket]
    return max(candidates, key=lambda row: str(row.get("snapshot_at") or ""), default=None)


def _causal_warnings(forecast: dict[str, Any], peak: Mapping[str, Any]) -> list[str]:
    warnings = set(str(item) for item in forecast.get("data_quality_warnings", []))
    required_models = set(load_airport_config()["common_models"])
    if set(forecast.get("models", [])) != required_models:
        warnings.add("missing_model")
    if not peak.get("available"):
        warnings.add("insufficient_calibration_history")
    source_age = forecast.get("weather_source_age_minutes")
    if source_age is None or float(source_age) > 90:
        warnings.add("stale_weather_data")
    if peak.get("available"):
        expected = {row["checkpoint"]: datetime.fromisoformat(row["checkpoint_utc"])
                    for row in checkpoint_schedule(peak["expected_peak_local"])}
        generated = datetime.fromisoformat(str(forecast["generated_at"]).replace("Z", "+00:00"))
        scheduled = expected.get(forecast["checkpoint"])
        if scheduled is None or abs((generated - scheduled).total_seconds()) > 15 * 60:
            warnings.add("checkpoint_missed")
    return sorted(warnings)


def run_scanner_cycle(
    *, forecasts: Iterable[Mapping[str, Any]], peak_history: Mapping[str, list[Mapping[str, Any]]],
    markets: Iterable[Mapping[str, Any]], store: OpportunityStore,
) -> list[dict[str, Any]]:
    """Persist newly-triggered immutable events and return current airport states."""
    airports = load_airport_config()["airports"]
    policy = load_policy()
    market_rows = list(markets)
    states: list[dict[str, Any]] = []
    for raw in forecasts:
        forecast = dict(raw)
        airport = str(forecast["airport"])
        if airport not in airports:
            raise ValueError(f"Airport is outside the frozen scanner set: {airport}")
        target = str(forecast["target_date"])[:10]
        peak = expected_peak_time(peak_history.get(airport, []), target, airports[airport]["timezone"])
        forecast["expected_peak"] = peak
        forecast["data_quality_warnings"] = _causal_warnings(forecast, peak)
        rules = matching_rules(forecast)
        if not rules:
            states.append({**no_signal_record(forecast), "forecast": forecast,
                           "next_checkpoint": _next_checkpoint(peak)})
            continue
        triggered = []
        for rule in rules:
            market = _market_for(market_rows, airport, target, int(forecast["modal_bucket"]))
            signal = evaluate_trigger(forecast, rule, market, policy=policy)
            inserted = store.append_signal(signal)
            market_snapshot = None
            trade = None
            if market and inserted:
                market_snapshot = make_market_snapshot(signal, market)
                store.append_market_snapshot(market_snapshot)
                trade = make_shadow_trade(signal, market_snapshot["market_snapshot_id"])
                if trade:
                    store.append_trade(trade)
            triggered.append({**signal, "market_snapshot": market_snapshot, "shadow_trade": trade})
        triggered.sort(key=lambda row: (row["hc_class"] != "HC-A",
                                        -row["historical_rule_evidence"]["validation_accuracy"]))
        states.append({**triggered[0], "triggered_rules": triggered,
                       "next_checkpoint": _next_checkpoint(peak)})
    return states


def _next_checkpoint(peak: Mapping[str, Any], now: datetime | None = None) -> str | None:
    if not peak.get("available"):
        return None
    current = now or datetime.now(timezone.utc)
    future = [row for row in checkpoint_schedule(peak["expected_peak_local"])
              if datetime.fromisoformat(row["checkpoint_utc"]) >= current]
    return future[0]["checkpoint_utc"] if future else None


def forward_rule_metrics(journal: Mapping[str, Any]) -> list[dict[str, Any]]:
    trades = {row["shadow_trade_id"]: row for row in journal.get("shadow_trades", [])}
    resolutions = {row["shadow_trade_id"]: row for row in journal.get("shadow_resolutions", [])}
    grouped: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for trade_id, resolution in resolutions.items():
        if trade_id in trades:
            grouped[trades[trade_id]["rule_id"]].append((trades[trade_id], resolution))
    rows = []
    for rule_id, pairs in grouped.items():
        pnls = [float(resolution["hypothetical_pnl_usd"]) for _, resolution in pairs]
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in pnls:
            cumulative += pnl
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
        rows.append({
            "rule_id": rule_id, "forward_n": len(pairs),
            "wins": sum(bool(resolution["win"]) for _, resolution in pairs),
            "losses": sum(not bool(resolution["win"]) for _, resolution in pairs),
            "accuracy": sum(bool(resolution["win"]) for _, resolution in pairs) / len(pairs),
            "average_entry_price": sum(float(trade["entry_price"]) for trade, _ in pairs) / len(pairs),
            "average_edge": sum(float(trade["estimated_edge"]) for trade, _ in pairs) / len(pairs),
            "pnl_usd": sum(pnls), "roi": sum(pnls) / len(pairs), "max_drawdown_usd": max_drawdown,
            "evidence_class": "sequential_oos_shadow",
        })
    return sorted(rows, key=lambda row: row["rule_id"])


def portfolio_metrics(journal: Mapping[str, Any], *, as_of: datetime | None = None) -> dict[str, Any]:
    now = as_of or datetime.now(timezone.utc)
    signals = journal.get("opportunity_signals", [])
    trades = journal.get("shadow_trades", [])
    active_days = {row["target_date"] for row in signals}
    high_days = {row["target_date"] for row in signals if row["opportunity_status"] == "HIGH INTEREST"}
    per_day = Counter(row["target_date"] for row in signals)
    per_airport_days: dict[str, set[str]] = defaultdict(set)
    day_airports: dict[str, set[str]] = defaultdict(set)
    for row in signals:
        per_airport_days[row["airport"]].add(row["target_date"])
        day_airports[row["target_date"]].add(row["airport"])
    cutoff30 = (now.date() - timedelta(days=29)).isoformat()
    cutoff100 = (now.date() - timedelta(days=99)).isoformat()
    return {
        "days_with_opportunity": len(active_days), "days_with_high_interest": len(high_days),
        "days_with_two_or_more_opportunities": sum(count >= 2 for count in per_day.values()),
        "opportunity_days_30": sum(day >= cutoff30 for day in active_days),
        "opportunity_days_100": sum(day >= cutoff100 for day in active_days),
        "trades_30": sum(row["target_date"] >= cutoff30 for row in trades),
        "trades_100": sum(row["target_date"] >= cutoff100 for row in trades),
        "airport_contribution": [{
            "airport": airport, "opportunity_days": len(days),
            "unique_opportunity_days": sum(day_airports[day] == {airport} for day in days),
            "overlapping_days": sum(len(day_airports[day]) > 1 for day in days),
            "portfolio_days_lost_without_airport": sum(day_airports[day] == {airport} for day in days),
        } for airport, days in sorted(per_airport_days.items())],
    }


def evidence_funnel(journal: Mapping[str, Any]) -> dict[str, Any]:
    signals = journal.get("opportunity_signals", [])
    markets = {row["signal_id"] for row in journal.get("market_snapshots", [])}
    trades = journal.get("shadow_trades", [])
    resolved = journal.get("shadow_resolutions", [])
    counts = {
        "hc_ab_triggers": len(signals),
        "market_matches": sum(row["signal_id"] in markets for row in signals),
        "fresh_quotes": sum(row.get("market_freshness") in {"fresh", "acceptable"} for row in signals),
        "positive_edge": sum((row.get("conservative_rule_edge") or -1) > 0 for row in signals),
        "shadow_trades": len(trades), "resolved": len(resolved),
        "wins": sum(bool(row["win"]) for row in resolved),
    }
    today = datetime.now(timezone.utc).date().isoformat()
    cutoff30 = (datetime.now(timezone.utc).date() - timedelta(days=29)).isoformat()
    counts["today"] = _funnel_period(journal, lambda day: day == today)
    counts["last_30_days"] = _funnel_period(journal, lambda day: day >= cutoff30)
    counts["all"] = dict(counts)
    counts["all"].pop("today", None)
    counts["all"].pop("last_30_days", None)
    return counts


def _funnel_period(journal: Mapping[str, Any], includes) -> dict[str, int]:
    signals = [row for row in journal.get("opportunity_signals", []) if includes(row["target_date"])]
    signal_ids = {row["signal_id"] for row in signals}
    markets = [row for row in journal.get("market_snapshots", []) if row["signal_id"] in signal_ids]
    trades = [row for row in journal.get("shadow_trades", []) if row["signal_id"] in signal_ids]
    trade_ids = {row["shadow_trade_id"] for row in trades}
    resolved = [row for row in journal.get("shadow_resolutions", []) if row["shadow_trade_id"] in trade_ids]
    return {"forecasts": len({(row["airport"], row["target_date"], row["checkpoint"]) for row in signals}),
            "hc_ab_triggers": len(signals), "market_matches": len(markets),
            "fresh_quotes": sum(row.get("freshness") in {"fresh", "acceptable"} for row in markets),
            "positive_edge": sum((row.get("conservative_rule_edge") or -1) > 0 for row in signals),
            "shadow_trades": len(trades), "resolved": len(resolved),
            "wins": sum(bool(row["win"]) for row in resolved)}
