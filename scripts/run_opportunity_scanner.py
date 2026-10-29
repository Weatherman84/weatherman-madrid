#!/usr/bin/env python3
"""Run one feed-driven scanner cycle without touching Production Neon."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

from weatherman.opportunity_portfolio import (
    evidence_funnel, forward_rule_metrics, portfolio_metrics, run_scanner_cycle,
)
from weatherman.opportunity_scanner import load_display_rules, load_rule_pack, resolve_shadow_trade
from weatherman.opportunity_results import (
    build_performance_views,
    collect_result_observations,
)
from weatherman.opportunity_store import OpportunityStore


def _fetch_existing_journal(
    url: str,
    *,
    attempts: int = 3,
    timeout_seconds: float = 20,
) -> dict:
    """Fetch the immutable journal with bounded retries for transient read errors."""
    last_error: httpx.HTTPError | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.get(
                url,
                timeout=timeout_seconds,
                follow_redirects=True,
            )
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            return response.json()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code < 500:
                raise
        if attempt < attempts:
            time.sleep(2 ** (attempt - 1))
    raise RuntimeError(
        f"Existing Opportunity Scanner journal remained unavailable after "
        f"{attempts} attempts"
    ) from last_error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True,
                        help="Compact causal feed JSON; never a Production DB URL")
    parser.add_argument("--database", type=Path, default=Path("data/opportunity_scanner.db"))
    parser.add_argument("--output", type=Path, default=Path("opportunity-scanner-journal.json"))
    parser.add_argument("--existing-url")
    parser.add_argument("--activation-date")
    parser.add_argument("--resolution-input", type=Path)
    args = parser.parse_args()
    if args.database.name == "weatherman.db":
        raise SystemExit("Refusing to use the Madrid Production database")
    feed = json.loads(args.input.read_text(encoding="utf-8"))
    if feed.get("schema_version") != "opportunity_scanner_feed_v1":
        raise SystemExit("Unsupported scanner feed schema")
    if feed.get("contains_outcomes") is True:
        raise SystemExit("Live selector feed must not contain outcome fields")
    if args.activation_date:
        before_activation = [row for row in feed.get("forecasts", [])
                             if str(row.get("target_date", ""))[:10] < args.activation_date]
        if before_activation:
            raise SystemExit("Automatic backfill is forbidden before the activation date")
    store = OpportunityStore(args.database)
    if args.existing_url:
        existing = _fetch_existing_journal(args.existing_url)
        if existing:
            store.import_journal(existing)
    states = run_scanner_cycle(
        forecasts=feed.get("forecasts", []), peak_history=feed.get("peak_history", {}),
        markets=feed.get("markets", []), store=store,
    )
    if args.resolution_input:
        resolution_feed = json.loads(args.resolution_input.read_text(encoding="utf-8"))
        if resolution_feed.get("schema_version") != "opportunity_scanner_resolution_feed_v1":
            raise SystemExit("Unsupported resolution feed schema")
        trades = {row["shadow_trade_id"]: row for row in store.records("shadow_trade")}
        for row in resolution_feed.get("resolutions", []):
            trade = trades.get(row.get("shadow_trade_id"))
            if trade is None:
                raise SystemExit(f"Resolution references unknown trade: {row.get('shadow_trade_id')}")
            resolution = resolve_shadow_trade(
                trade, row["official_resolved_bucket"], row["settlement_source"],
                resolved_at=row["resolved_at"], metar_actual_bucket=row.get("metar_actual_bucket"),
            )
            store.append_resolution(resolution)
    journal = store.journal()
    result_updates = collect_result_observations(journal)
    for result in result_updates:
        store.append_result_observation(result)
    journal = store.journal()
    rule_pack = load_rule_pack()
    performance = build_performance_views(journal)
    journal.update({
        "airport_states": states,
        "rule_set_sha256": rule_pack["canonical_rules_sha256"],
        "active_rule_count": len(rule_pack["rules"]),
        "display_only_rules": load_display_rules(),
        "forward_rule_metrics": forward_rule_metrics(journal),
        "portfolio_metrics": portfolio_metrics(journal),
        "evidence_funnel": evidence_funnel(journal),
        "performance": performance,
        "result_collection": {
            "evaluation_version": "opportunity_result_evaluation_v1",
            "new_result_observations": len(result_updates),
            "automatic_retry": True,
        },
        "automatic_backfill": False,
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": "built", "output": str(args.output),
                      "signals": len(journal["opportunity_signals"]),
                      "shadow_trades": len(journal["shadow_trades"]),
                      "result_observations": len(journal["result_observations"]),
                      "writes_production_database": False}, separators=(",", ":")))


if __name__ == "__main__":
    main()
