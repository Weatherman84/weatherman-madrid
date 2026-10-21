"""Append-only result collection and deduplicated Shadow portfolio read models.

This module evaluates immutable scanner signals after the fact.  It never changes
selection rules or entry evidence and has no connection to the Madrid database.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from .opportunity_scanner import canonical_hash, load_airport_config
from .providers import (
    POLYMARKET_GAMMA_URL,
    _get,
    _json_array,
    _number,
    _temperature_range,
    polymarket_event_slug,
    recent_metars,
)


EVALUATION_VERSION = "opportunity_result_evaluation_v1"
PORTFOLIO_VIEW_VERSION = "deduplicated_opportunity_portfolio_v1"
FINAL_METAR_STATUS = "METAR_COMPLETE"
FINAL_MARKET_STATUS = "OFFICIALLY_RESOLVED"


def _utc(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_id(prefix: str, value: Mapping[str, Any]) -> str:
    return f"{prefix}_{canonical_hash(value)[:24]}"


def bucket_contains(value: float, low: object, high: object) -> bool:
    lower = float(low) if low is not None else None
    upper = float(high) if high is not None else None
    return (lower is None or value >= lower) and (upper is None or value <= upper)


def _snapshot_identity(snapshot: Mapping[str, Any], trade: Mapping[str, Any]) -> dict[str, Any]:
    market_id = snapshot.get("market_id")
    if market_id:
        return {"market_id": str(market_id)}
    return {
        "event": snapshot.get("event"),
        "bucket_low_c": snapshot.get("bucket_low_c", trade.get("bucket")),
        "bucket_high_c": snapshot.get("bucket_high_c", trade.get("bucket")),
        "bucket_label": snapshot.get("bucket_label"),
    }


def build_portfolio_opportunities(journal: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Group overlapping rules into one tradeable opportunity per causal event."""
    signals = {str(row["signal_id"]): dict(row) for row in journal.get("opportunity_signals", [])}
    snapshots = {
        str(row["market_snapshot_id"]): dict(row)
        for row in journal.get("market_snapshots", [])
    }
    snapshots_by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshots.values():
        snapshots_by_signal[str(row.get("signal_id"))].append(row)
    trades_by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in journal.get("shadow_trades", []):
        trades_by_signal[str(row.get("signal_id"))].append(dict(row))

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal_id, signal in signals.items():
        trade_candidates = sorted(
            trades_by_signal.get(signal_id, []),
            key=lambda row: (str(row.get("generated_at") or ""), str(row.get("shadow_trade_id"))),
        )
        trade = trade_candidates[0] if trade_candidates else {}
        snapshot = snapshots.get(str(trade.get("entry_snapshot_id")))
        if snapshot is None:
            snapshot = max(
                snapshots_by_signal.get(signal_id, []),
                key=lambda row: str(row.get("snapshot_at") or ""),
                default={},
            )
        market_identity = _snapshot_identity(snapshot, {**signal, **trade})
        identity = {
            "view_version": PORTFOLIO_VIEW_VERSION,
            "airport": signal["airport"],
            "target_date": signal["target_date"],
            "checkpoint": signal["checkpoint"],
            "market_outcome": market_identity,
        }
        opportunity_id = _event_id("opportunity", identity)
        grouped[opportunity_id].append(
            {"signal": signal, "trade": trade, "market_snapshot": snapshot}
        )

    output: list[dict[str, Any]] = []
    status_rank = {"HIGH INTEREST": 3, "WATCH": 2, "NO TRADE": 1}
    for opportunity_id, members in grouped.items():
        members.sort(
            key=lambda item: (
                str(item["signal"].get("triggered_at") or ""),
                str(item["signal"].get("signal_id")),
            )
        )
        representative = next((item for item in members if item["trade"]), members[0])
        signal, trade, snapshot = (
            representative["signal"], representative["trade"], representative["market_snapshot"]
        )
        statuses = [str(item["signal"].get("opportunity_status") or "") for item in members]
        status = max(statuses, key=lambda value: status_rank.get(value, 0), default="")
        low = snapshot.get("bucket_low_c")
        high = snapshot.get("bucket_high_c")
        if low is None and high is None:
            low = high = trade.get("bucket", signal.get("bucket"))
        output.append(
            {
                "opportunity_id": opportunity_id,
                "portfolio_view_version": PORTFOLIO_VIEW_VERSION,
                "airport": signal["airport"],
                "target_date": signal["target_date"],
                "checkpoint": signal["checkpoint"],
                "triggered_at": signal.get("triggered_at"),
                "opportunity_status": status,
                "hc_classes": sorted({str(item["signal"].get("hc_class")) for item in members}),
                "rule_ids": sorted({str(item["signal"]["rule_id"]) for item in members}),
                "signal_ids": sorted(str(item["signal"]["signal_id"]) for item in members),
                "shadow_trade_ids": sorted(
                    str(item["trade"]["shadow_trade_id"])
                    for item in members if item["trade"].get("shadow_trade_id")
                ),
                "overlapping_rule_count": len(members),
                "market_id": snapshot.get("market_id"),
                "event": snapshot.get("event"),
                "market_slug": snapshot.get("market_slug"),
                "bucket_label": snapshot.get("bucket_label") or str(signal.get("bucket")),
                "bucket_low_c": low,
                "bucket_high_c": high,
                "predicted_bucket": signal.get("bucket"),
                "model_probability": trade.get("model_probability", (signal.get("forecast") or {}).get("top1_probability")),
                "entry_price": trade.get("entry_price"),
                "estimated_edge": trade.get("estimated_edge", signal.get("conservative_rule_edge")),
                "stake_usd": 1.0 if trade else None,
                "representative_shadow_trade_id": trade.get("shadow_trade_id"),
                "entry_selection_method": "earliest_immutable_trade_in_opportunity",
            }
        )
    return sorted(
        output,
        key=lambda row: (row["target_date"], row["airport"], str(row["triggered_at"]), row["opportunity_id"]),
    )


def evaluate_metar_day(
    rows: Iterable[Mapping[str, Any]], target_date: str, zone_name: str, *, now: datetime,
) -> dict[str, Any]:
    target = date.fromisoformat(str(target_date)[:10])
    zone = ZoneInfo(zone_name)
    current_local_date = _utc(now).astimezone(zone).date()
    deduplicated: dict[str, dict[str, Any]] = {}
    for source in rows:
        if source.get("observed_at") is None or source.get("temp_c") is None:
            continue
        observed = _utc(source["observed_at"])
        if observed.astimezone(zone).date() == target:
            key = observed.isoformat()
            candidate = dict(source)
            prior = deduplicated.get(key)
            candidate_is_correction = " COR " in f" {str(candidate.get('raw') or '').upper()} "
            prior_is_correction = " COR " in f" {str((prior or {}).get('raw') or '').upper()} "
            if prior is None or candidate_is_correction or not prior_is_correction:
                deduplicated[key] = candidate
    selected = sorted(deduplicated.values(), key=lambda row: _utc(row["observed_at"]))
    if not selected:
        return {
            "status": "OPEN" if target >= current_local_date else "METAR_INCOMPLETE",
            "maximum_c": None,
            "source": "aviationweather.gov METAR",
            "coverage": {"accepted_observations": 0},
            "quality_notes": ["no_accepted_metar_observations"],
        }
    times = [_utc(row["observed_at"]).astimezone(zone) for row in selected]
    gaps = [
        (later - earlier).total_seconds() / 60
        for earlier, later in zip(times, times[1:])
    ]
    first_minute = times[0].hour * 60 + times[0].minute
    last_minute = times[-1].hour * 60 + times[-1].minute
    max_gap = max(gaps, default=0.0)
    complete = (
        target < current_local_date
        and first_minute <= 60
        and last_minute >= 23 * 60
        and len(selected) >= 24
        and max_gap <= 120
    )
    status = (
        FINAL_METAR_STATUS if complete
        else "PROVISIONAL_METAR" if target == current_local_date
        else "OPEN" if target > current_local_date
        else "METAR_INCOMPLETE"
    )
    notes = []
    if target < current_local_date and not complete:
        notes.append("local_day_coverage_incomplete")
    return {
        "status": status,
        "maximum_c": max(float(row["temp_c"]) for row in selected),
        "source": "aviationweather.gov METAR",
        "coverage": {
            "accepted_observations": len(selected),
            "first_observed_local": times[0].isoformat(),
            "last_observed_local": times[-1].isoformat(),
            "maximum_gap_minutes": max_gap,
            "timezone": zone_name,
        },
        "quality_notes": notes,
    }


def _explicit_yes_winner(market: Mapping[str, Any]) -> bool | None:
    outcomes = _json_array(market.get("outcomes"))
    prices = [_number(value) for value in _json_array(market.get("outcomePrices"))]
    try:
        yes_index = [str(value).casefold() for value in outcomes].index("yes")
    except ValueError:
        return None
    winner = market.get("winner")
    if isinstance(winner, bool):
        return winner
    winning_outcome = str(market.get("winningOutcome") or market.get("resolvedOutcome") or "").casefold()
    if winning_outcome:
        return winning_outcome == "yes"
    # Exact terminal payouts are accepted only together with an explicit resolved status.
    if len(prices) == len(outcomes) and all(value is not None for value in prices):
        if abs(float(prices[yes_index]) - 1.0) < 1e-12 and all(
            index == yes_index or abs(float(value)) < 1e-12
            for index, value in enumerate(prices)
        ):
            return True
        if abs(float(prices[yes_index])) < 1e-12 and any(
            index != yes_index and abs(float(value) - 1.0) < 1e-12
            for index, value in enumerate(prices)
        ):
            return False
    return None


def normalize_official_market_results(event: Mapping[str, Any], *, source_url: str) -> list[dict[str, Any]]:
    """Accept a winner only with an explicit resolved status, never from price alone."""
    rows: list[dict[str, Any]] = []
    for market in event.get("markets", []):
        label = str(market.get("groupItemTitle") or market.get("question") or "")
        low, high = _temperature_range(label)
        if low is None and high is None:
            continue
        status_text = str(
            market.get("umaResolutionStatus")
            or market.get("resolutionStatus")
            or market.get("status")
            or ""
        ).casefold()
        explicitly_resolved = bool(market.get("resolved") is True or status_text in {"resolved", "settled", "final"})
        winner = _explicit_yes_winner(market) if explicitly_resolved else None
        rows.append(
            {
                "market_id": str(market.get("id") or ""),
                "market_slug": str(market.get("slug") or ""),
                "condition_id": market.get("conditionId"),
                "bucket_label": label,
                "bucket_low_c": low,
                "bucket_high_c": high,
                "closed": bool(market.get("closed")),
                "resolution_status": status_text or None,
                "officially_resolved": explicitly_resolved and winner is not None,
                "yes_won": winner,
                "source": source_url,
                "resolution_source": event.get("resolutionSource"),
            }
        )
    return rows


def fetch_official_market_results(airport: Mapping[str, Any], target: date) -> list[dict[str, Any]]:
    slug = polymarket_event_slug(dict(airport), target)
    if not slug:
        return []
    source_url = f"{POLYMARKET_GAMMA_URL}/events/slug/{slug}"
    event = _get(source_url, attempts=3, timeout=10)
    return normalize_official_market_results(event if isinstance(event, dict) else {}, source_url=source_url)


def _market_result(opportunity: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    market_id = str(opportunity.get("market_id") or "")
    matched = next((dict(row) for row in rows if market_id and str(row.get("market_id")) == market_id), None)
    if matched is None and not market_id:
        matched = next(
            (
                dict(row) for row in rows
                if bucket_contains(
                    float(opportunity["predicted_bucket"]), row.get("bucket_low_c"), row.get("bucket_high_c")
                )
            ),
            None,
        )
    winner = next((dict(row) for row in rows if row.get("officially_resolved") and row.get("yes_won") is True), None)
    if matched is None:
        return {
            "status": "MAPPING_UNAVAILABLE",
            "market_id": market_id or None,
            "win": None,
            "quality_notes": ["assigned_market_not_found"],
        }
    if not matched.get("officially_resolved"):
        return {
            "status": "AWAITING_OFFICIAL_RESOLUTION",
            "market_id": matched.get("market_id"),
            "win": None,
            "source": matched.get("source"),
            "quality_notes": ["closed_or_prices_do_not_prove_official_resolution"],
        }
    return {
        "status": FINAL_MARKET_STATUS,
        "market_id": matched.get("market_id"),
        "win": bool(matched.get("yes_won")),
        "source": matched.get("source"),
        "resolution_source": matched.get("resolution_source"),
        "winner_market_id": winner.get("market_id") if winner else None,
        "winner_bucket_label": winner.get("bucket_label") if winner else None,
        "winner_bucket_low_c": winner.get("bucket_low_c") if winner else None,
        "winner_bucket_high_c": winner.get("bucket_high_c") if winner else None,
        "quality_notes": [],
    }


def collect_result_observations(
    journal: Mapping[str, Any], *, now: datetime | None = None,
    metar_fetcher: Callable[..., list[dict[str, Any]]] = recent_metars,
    market_fetcher: Callable[[Mapping[str, Any], date], list[dict[str, Any]]] = fetch_official_market_results,
) -> list[dict[str, Any]]:
    """Return only changed immutable result states; provider failures stay isolated."""
    recorded_at = _utc(now or datetime.now(timezone.utc))
    opportunities = build_portfolio_opportunities(journal)
    existing = journal.get("result_observations", [])
    existing_ids = {str(row.get("result_observation_id")) for row in existing}
    latest = latest_result_observations(existing)
    config = load_airport_config()["airports"]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for opportunity in opportunities:
        prior = latest.get(opportunity["opportunity_id"], {})
        if (
            (prior.get("meteorological") or {}).get("status") == FINAL_METAR_STATUS
            and (prior.get("official_market") or {}).get("status") == FINAL_MARKET_STATUS
        ):
            continue
        groups[(opportunity["airport"], opportunity["target_date"])].append(opportunity)

    output: list[dict[str, Any]] = []
    for (airport, target_text), members in groups.items():
        airport_config = config[airport]
        target = date.fromisoformat(target_text)
        age_days = max(0, (recorded_at.astimezone(ZoneInfo(airport_config["timezone"])).date() - target).days)
        hours = min(168, max(48, age_days * 24 + 30))
        prior_metars = [
            (latest.get(member["opportunity_id"], {}).get("meteorological") or {})
            for member in members
        ]
        reusable_final = next(
            (row for row in prior_metars if row.get("status") == FINAL_METAR_STATUS), None
        )
        if reusable_final is not None and age_days > 2:
            metar = reusable_final
        else:
            try:
                metar = evaluate_metar_day(
                    metar_fetcher(airport, hours=hours), target_text,
                    airport_config["timezone"], now=recorded_at,
                )
            except Exception as exc:
                metar = {
                    "status": "PROVIDER_ERROR", "maximum_c": None,
                    "source": "aviationweather.gov METAR",
                    "coverage": {}, "quality_notes": [f"metar_{type(exc).__name__}"],
                }
        try:
            official_rows = market_fetcher(airport_config, target)
            market_error = None
        except Exception as exc:
            official_rows = []
            market_error = f"market_{type(exc).__name__}"
        for opportunity in members:
            official = (
                _market_result(opportunity, official_rows)
                if market_error is None
                else {"status": "PROVIDER_ERROR", "win": None, "quality_notes": [market_error]}
            )
            prior = latest.get(opportunity["opportunity_id"])
            if prior:
                prior_metar = prior.get("meteorological") or {}
                prior_official = prior.get("official_market") or {}
                if (
                    prior_metar.get("status") == FINAL_METAR_STATUS
                    and metar.get("status") != FINAL_METAR_STATUS
                ):
                    metar = prior_metar
                elif metar.get("status") == "PROVIDER_ERROR" and prior_metar.get("maximum_c") is not None:
                    metar = prior_metar
                if (
                    prior_official.get("status") == FINAL_MARKET_STATUS
                    and official.get("status") != FINAL_MARKET_STATUS
                ):
                    official = prior_official
                elif official.get("status") == "PROVIDER_ERROR" and prior_official:
                    official = prior_official
            payload_state = {
                "opportunity_id": opportunity["opportunity_id"],
                "meteorological": metar,
                "official_market": official,
            }
            if prior and canonical_hash(payload_state) == canonical_hash(
                {
                    "opportunity_id": prior.get("opportunity_id"),
                    "meteorological": prior.get("meteorological") or {},
                    "official_market": prior.get("official_market") or {},
                }
            ):
                continue
            identity = {
                "evaluation_version": EVALUATION_VERSION,
                "supersedes": prior.get("result_observation_id") if prior else None,
                **payload_state,
            }
            result_id = _event_id("result", identity)
            if result_id in existing_ids:
                continue
            output.append(
                {
                    "result_observation_id": result_id,
                    "opportunity_id": opportunity["opportunity_id"],
                    "airport": airport,
                    "target_date": target_text,
                    "signal_ids": opportunity["signal_ids"],
                    "shadow_trade_ids": opportunity["shadow_trade_ids"],
                    "rule_ids": opportunity["rule_ids"],
                    "recorded_at": recorded_at.isoformat(),
                    "evaluation_version": EVALUATION_VERSION,
                    "supersedes_result_observation_id": prior.get("result_observation_id") if prior else None,
                    **payload_state,
                }
            )
    return output


def latest_result_observations(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for source in rows:
        row = dict(source)
        key = str(row.get("opportunity_id"))
        prior = latest.get(key)
        if prior is None or (str(row.get("recorded_at")), str(row.get("result_observation_id"))) > (
            str(prior.get("recorded_at")), str(prior.get("result_observation_id"))
        ):
            latest[key] = row
    return latest


def _gross_position(opportunity: Mapping[str, Any], official: Mapping[str, Any]) -> dict[str, Any]:
    if opportunity.get("opportunity_status") != "HIGH INTEREST" or not opportunity.get("shadow_trade_ids"):
        return {"eligible": False, "reason": "not_a_high_interest_shadow_position"}
    try:
        price = float(opportunity["entry_price"])
    except (TypeError, ValueError):
        return {"eligible": False, "reason": "missing_entry_price"}
    if not 0 < price <= 1:
        return {"eligible": False, "reason": "invalid_entry_price"}
    if official.get("status") != FINAL_MARKET_STATUS or official.get("win") is None:
        return {"eligible": True, "settled": False, "stake_usd": 1.0}
    win = bool(official["win"])
    payout = 1.0 / price if win else 0.0
    return {
        "eligible": True, "settled": True, "stake_usd": 1.0,
        "shares": 1.0 / price, "payout_usd": payout,
        "gross_pnl_usd": payout - 1.0, "win": win,
        "fees_included": False, "execution_confirmed": False,
    }


def build_performance_views(journal: Mapping[str, Any]) -> dict[str, Any]:
    opportunities = build_portfolio_opportunities(journal)
    latest = latest_result_observations(journal.get("result_observations", []))
    details: list[dict[str, Any]] = []
    for opportunity in opportunities:
        result = latest.get(opportunity["opportunity_id"], {})
        metar = result.get("meteorological") or {}
        official = result.get("official_market") or {}
        actual = metar.get("maximum_c")
        metar_hit = (
            bucket_contains(float(actual), opportunity.get("bucket_low_c"), opportunity.get("bucket_high_c"))
            if actual is not None else None
        )
        position = _gross_position(opportunity, official)
        meteorological_position = _gross_position(
            opportunity,
            {"status": FINAL_MARKET_STATUS, "win": metar_hit}
            if metar.get("status") == FINAL_METAR_STATUS and metar_hit is not None
            else {},
        )
        details.append(
            {
                **opportunity,
                "result_observation_id": result.get("result_observation_id"),
                "result_recorded_at": result.get("recorded_at"),
                "meteorological_status": metar.get("status", "OPEN"),
                "metar_maximum_c": actual,
                "metar_hit": metar_hit,
                "metar_source": metar.get("source"),
                "metar_quality_notes": metar.get("quality_notes", []),
                "official_status": official.get("status", "OPEN"),
                "official_win": official.get("win"),
                "official_winner_bucket": official.get("winner_bucket_label"),
                "official_source": official.get("source"),
                "official_quality_notes": official.get("quality_notes", []),
                **{f"position_{key}": value for key, value in position.items()},
                **{
                    f"meteorological_position_{key}": value
                    for key, value in meteorological_position.items()
                },
            }
        )

    high = [row for row in details if row["opportunity_status"] == "HIGH INTEREST"]
    metar_final = [row for row in high if row["meteorological_status"] == FINAL_METAR_STATUS]
    official_final = [row for row in high if row["official_status"] == FINAL_MARKET_STATUS and row.get("position_settled")]
    pnl = sum(float(row["position_gross_pnl_usd"]) for row in official_final)
    stake = sum(float(row["position_stake_usd"]) for row in official_final)
    open_stake = sum(
        float(row.get("position_stake_usd") or 0)
        for row in high if row.get("position_eligible") and not row.get("position_settled")
    )
    metar_positions = [row for row in metar_final if row.get("meteorological_position_settled")]
    metar_stake = sum(float(row["meteorological_position_stake_usd"]) for row in metar_positions)
    metar_pnl = sum(float(row["meteorological_position_gross_pnl_usd"]) for row in metar_positions)
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    curve = []
    for row in sorted(official_final, key=lambda item: (item["target_date"], str(item["triggered_at"]), item["opportunity_id"])):
        cumulative += float(row["position_gross_pnl_usd"])
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
        curve.append(
            {
                "target_date": row["target_date"], "airport": row["airport"],
                "opportunity_id": row["opportunity_id"], "gross_pnl_usd": row["position_gross_pnl_usd"],
                "cumulative_gross_pnl_usd": cumulative,
            }
        )

    signal_to_detail = {
        signal_id: row for row in details for signal_id in row.get("signal_ids", [])
    }
    rule_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in journal.get("opportunity_signals", []):
        detail = signal_to_detail.get(signal.get("signal_id"))
        if detail:
            rule_rows[str(signal["rule_id"])].append(detail)
    rule_diagnostics = []
    for rule_id, rows in sorted(rule_rows.items()):
        final = [row for row in rows if row["meteorological_status"] == FINAL_METAR_STATUS]
        rule_diagnostics.append(
            {
                "rule_id": rule_id, "signals": len(rows), "meteorologically_completed": len(final),
                "metar_hits": sum(row.get("metar_hit") is True for row in final),
                "metar_accuracy": (sum(row.get("metar_hit") is True for row in final) / len(final)) if final else None,
                "overlapping_signals": sum(row.get("overlapping_rule_count", 1) > 1 for row in rows),
            }
        )

    per_event: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in high:
        per_event[(row["airport"], row["target_date"])].append(row)
    daily = []
    for (airport, target), rows in sorted(per_event.items()):
        settled = [row for row in rows if row.get("position_settled")]
        daily.append(
            {
                "airport": airport, "target_date": target, "positions": len(rows),
                "checkpoints": sorted({row["checkpoint"] for row in rows}),
                "buckets": [row.get("bucket_label") for row in rows],
                "total_stake_usd": sum(float(row.get("position_stake_usd") or 0) for row in rows),
                "settled_stake_usd": sum(float(row.get("position_stake_usd") or 0) for row in settled),
                "payout_usd": sum(float(row.get("position_payout_usd") or 0) for row in settled),
                "gross_pnl_usd": sum(float(row.get("position_gross_pnl_usd") or 0) for row in settled),
                "open_positions": sum(not bool(row.get("position_settled")) for row in rows),
            }
        )

    def price_band(value: object) -> str:
        number = float(value)
        if number < 0.10:
            return "<10 %"
        if number < 0.25:
            return "10–<25 %"
        if number < 0.50:
            return "25–<50 %"
        return "≥50 %"

    def edge_band(value: object) -> str:
        number = float(value)
        if number < 0.10:
            return "<10 pp"
        if number < 0.20:
            return "10–<20 pp"
        return "≥20 pp"

    def probability_band(value: object) -> str:
        number = float(value)
        if number < 0.40:
            return "<40 %"
        if number < 0.50:
            return "40–<50 %"
        if number < 0.60:
            return "50–<60 %"
        return "≥60 %"

    for row in high:
        row["hc_class_group"] = "+".join(row.get("hc_classes") or [])
        row["entry_price_band"] = price_band(row["entry_price"]) if row.get("entry_price") is not None else "Fehlt"
        row["edge_band"] = edge_band(row["estimated_edge"]) if row.get("estimated_edge") is not None else "Fehlt"
        row["probability_band"] = probability_band(row["model_probability"]) if row.get("model_probability") is not None else "Fehlt"

    def breakdown(field: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in high:
            grouped[str(row.get(field) or "Fehlt")].append(row)
        result = []
        for label, rows in sorted(grouped.items()):
            metar_rows = [row for row in rows if row["meteorological_status"] == FINAL_METAR_STATUS]
            official_rows = [row for row in rows if row.get("position_settled")]
            official_stake = sum(float(row.get("position_stake_usd") or 0) for row in official_rows)
            official_pnl = sum(float(row.get("position_gross_pnl_usd") or 0) for row in official_rows)
            result.append(
                {
                    "group": label,
                    "opportunities": len(rows),
                    "metar_completed": len(metar_rows),
                    "metar_hits": sum(row.get("metar_hit") is True for row in metar_rows),
                    "metar_accuracy": (
                        sum(row.get("metar_hit") is True for row in metar_rows) / len(metar_rows)
                        if metar_rows else None
                    ),
                    "officially_settled": len(official_rows),
                    "official_wins": sum(row.get("official_win") is True for row in official_rows),
                    "official_win_rate": (
                        sum(row.get("official_win") is True for row in official_rows) / len(official_rows)
                        if official_rows else None
                    ),
                    "gross_pnl_usd": official_pnl,
                    "gross_roi": official_pnl / official_stake if official_stake else None,
                }
            )
        return result

    both_final = [
        row for row in high
        if row["meteorological_status"] == FINAL_METAR_STATUS
        and row["official_status"] == FINAL_MARKET_STATUS
    ]

    return {
        "portfolio_view_version": PORTFOLIO_VIEW_VERSION,
        "pnl_basis": "gross_shadow_unconfirmed_execution",
        "opportunities": details,
        "summary": {
            "unique_high_interest_opportunities": len(high),
            "meteorologically_completed": len(metar_final),
            "meteorological_hits": sum(row.get("metar_hit") is True for row in metar_final),
            "meteorological_accuracy": (
                sum(row.get("metar_hit") is True for row in metar_final) / len(metar_final)
                if metar_final else None
            ),
            "meteorological_what_if_stake_usd": metar_stake,
            "meteorological_what_if_gross_pnl_usd": metar_pnl,
            "meteorological_what_if_gross_roi": metar_pnl / metar_stake if metar_stake else None,
            "officially_settled": len(official_final),
            "official_wins": sum(row.get("official_win") is True for row in official_final),
            "official_win_rate": (
                sum(row.get("official_win") is True for row in official_final) / len(official_final)
                if official_final else None
            ),
            "settled_stake_usd": stake,
            "gross_pnl_usd": pnl,
            "gross_roi": pnl / stake if stake else None,
            "open_stake_usd": open_stake,
            "maximum_drawdown_usd": drawdown if official_final else None,
        },
        "pnl_curve": curve,
        "rule_diagnostics": rule_diagnostics,
        "daily_airport_portfolio": daily,
        "descriptive_breakdowns": {
            "airport": breakdown("airport"),
            "checkpoint": breakdown("checkpoint"),
            "hc_class": breakdown("hc_class_group"),
            "entry_price": breakdown("entry_price_band"),
            "estimated_edge": breakdown("edge_band"),
            "model_probability": breakdown("probability_band"),
        },
        "metar_official_comparison": {
            "both_final": len(both_final),
            "different_outcomes": sum(
                bool(row.get("metar_hit")) != bool(row.get("official_win")) for row in both_final
            ),
        },
    }
