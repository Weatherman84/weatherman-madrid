from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

import pandas as pd

from .analytics import DayStatus, market_edges


@dataclass(frozen=True)
class TradeDecision:
    status: str
    bucket_label: str | None
    fair_probability: float | None
    buy_price: float | None
    edge: float | None
    probability_change: float | None
    confidence: int
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    basket: EdgeBasket | None = None


@dataclass(frozen=True)
class EdgeBasket:
    bucket_labels: tuple[str, ...]
    market_ids: tuple[str, ...]
    fair_probability: float
    total_cost: float
    edge: float
    top_model_bucket: str
    top_model_included: bool
    middle_bucket_excluded: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class HedgePlan:
    primary_bucket: str
    hedge_bucket: str
    primary_stake: float
    primary_price: float
    balanced_hedge_stake: float
    hedge_price: float
    total_cost: float
    covered_payout: float
    covered_result: float
    uncovered_result: float


def latest_prior_probabilities(
    snapshots: pd.DataFrame,
    target: date,
) -> dict[str, float]:
    """Return the latest complete stored probability view before the current rerun."""
    if snapshots.empty:
        return {}
    frame = snapshots.copy()
    frame = frame[pd.to_datetime(frame.target_date).dt.date == target]
    if frame.empty:
        return {}
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True)
    captures = frame.captured_at.dropna().drop_duplicates().sort_values()
    if captures.empty:
        return {}
    latest = captures.iloc[-1]
    selected = frame[frame.captured_at == latest]
    return {
        str(row.bucket_label): float(row.model_probability)
        for row in selected.itertuples()
        if pd.notna(row.model_probability)
    }


def build_edge_basket(
    probabilities: dict[int, float],
    markets: pd.DataFrame,
    *,
    minimum_individual_edge: float = 0.04,
) -> EdgeBasket | None:
    """Evaluate simultaneous positive-edge buckets as one mutually exclusive event."""
    comparison = market_edges(probabilities, markets)
    if comparison.empty:
        return None
    actionable = comparison[comparison.best_ask.notna()].copy()
    if "closed" in actionable:
        actionable = actionable[~actionable.closed.fillna(False).astype(bool)]
    selected = actionable[actionable.edge >= float(minimum_individual_edge)].copy()
    if len(selected) < 2:
        return None

    def order_value(row: pd.Series) -> float:
        if pd.notna(row.get("bucket_low_c")):
            return float(row.bucket_low_c)
        if pd.notna(row.get("bucket_high_c")):
            return float(row.bucket_high_c) - 1000.0
        return 0.0

    ordered = comparison.copy()
    ordered["_order"] = ordered.apply(order_value, axis=1)
    ordered = ordered.sort_values(["_order", "bucket_label"]).reset_index(drop=True)
    selected_ids = set(selected.market_id.astype(str))
    positions = [
        index
        for index, market_id in enumerate(ordered.market_id.astype(str))
        if market_id in selected_ids
    ]
    selected_ordered = ordered[
        ordered.market_id.astype(str).isin(selected_ids)
    ]
    middle_bucket_excluded = any(
        str(ordered.iloc[index].market_id) not in selected_ids
        for index in range(min(positions), max(positions) + 1)
    )
    top = comparison.sort_values("model_probability", ascending=False).iloc[0]
    top_label = str(top.bucket_label)
    top_included = str(top.market_id) in selected_ids
    warnings: list[str] = []
    if not top_included:
        warnings.append("Most likely bucket excluded")
    if middle_bucket_excluded:
        warnings.append("Middle bucket excluded")
    fair_probability = float(selected.model_probability.sum())
    total_cost = float(selected.buy_price.sum())
    if total_cost >= 1.0:
        warnings.append("Basket costs at least the maximum $1 payout")
    return EdgeBasket(
        bucket_labels=tuple(selected_ordered.bucket_label.astype(str)),
        market_ids=tuple(selected_ordered.market_id.astype(str)),
        fair_probability=fair_probability,
        total_cost=total_cost,
        edge=fair_probability - total_cost,
        top_model_bucket=top_label,
        top_model_included=top_included,
        middle_bucket_excluded=middle_bucket_excluded,
        warnings=tuple(warnings),
    )


def build_trade_decision(
    *,
    probabilities: dict[int, float],
    markets: pd.DataFrame,
    forecast_confidence: int,
    day_status: DayStatus,
    metar_pending: bool = False,
    market_model_conflict: bool = False,
    forecast_stale: bool = False,
    previous_probabilities: dict[str, float] | None = None,
    live_signals: Iterable[str] = (),
    bet_edge: float = 0.08,
    watch_edge: float = 0.04,
    minimum_confidence: int = 65,
    maximum_spread: float = 0.12,
    minimum_buy_price: float = 0.05,
    maximum_model_market_gap: float = 0.15,
    recommendations_enabled: bool = False,
) -> TradeDecision:
    """Compare weather and market values without treating raw gaps as calibrated edge."""
    confidence = int(max(0, min(100, forecast_confidence)))
    blockers: list[str] = []
    if day_status.is_locked:
        blockers.append("The daily maximum is already locked")
    if metar_pending:
        blockers.append("A routine METAR is imminent or due but not yet available")
    if market_model_conflict:
        blockers.append("A near-certain market price conflicts with the weather model")
    if forecast_stale:
        blockers.append("Fewer than two current weather models are available")
    if not recommendations_enabled:
        blockers.append(
            "Empirical probability calibration has not passed; recommendations are research-only"
        )
    if markets.empty:
        blockers.append("No matching Polymarket market is stored")
        return TradeDecision(
            "NO BET",
            None,
            None,
            None,
            None,
            None,
            confidence,
            tuple(live_signals),
            tuple(blockers),
        )
    if "closed" in markets and markets.closed.fillna(False).astype(bool).all():
        blockers.append("The market is closed")

    comparison = market_edges(probabilities, markets)
    basket = build_edge_basket(
        probabilities,
        markets,
        minimum_individual_edge=watch_edge,
    )
    if comparison.empty:
        blockers.append("The market buckets could not be matched to Celsius outcomes")
        return TradeDecision(
            "NO BET",
            None,
            None,
            None,
            None,
            None,
            confidence,
            tuple(live_signals),
            tuple(blockers),
        )

    actionable = comparison[comparison.best_ask.notna()].copy()
    if "closed" in actionable:
        actionable = actionable[~actionable.closed.fillna(False).astype(bool)]
    if actionable.empty:
        blockers.append("No executable YES ask is available")
        best = comparison.iloc[0]
    else:
        best = actionable.sort_values("edge", ascending=False).iloc[0]

    top_market_bucket = comparison.sort_values(
        ["model_probability", "edge"],
        ascending=False,
    ).iloc[0]

    label = str(best.bucket_label)
    fair_probability = float(best.model_probability)
    buy_price = float(best.buy_price) if pd.notna(best.buy_price) else None
    edge = float(best.edge) if pd.notna(best.edge) else None
    prior = (previous_probabilities or {}).get(label)
    probability_change = fair_probability - float(prior) if prior is not None else None
    spread = float(best.spread) if "spread" in best and pd.notna(best.spread) else None
    selected_is_top = str(best.market_id) == str(top_market_bucket.market_id)
    if not selected_is_top:
        blockers.append(
            "The selected range is not Weatherman's most likely Polymarket bucket"
        )
    if buy_price is not None and buy_price <= float(minimum_buy_price):
        blockers.append(
            f"YES ask {buy_price:.1%} is at or below the {minimum_buy_price:.0%} cheap-tail floor"
        )
    if edge is not None and edge >= float(maximum_model_market_gap):
        blockers.append(
            f"Champion-market gap {edge:.1%} is a conflict, not calibrated edge"
        )
    if confidence < minimum_confidence:
        blockers.append(f"Forecast confidence {confidence}/100 is below {minimum_confidence}/100")
    if spread is not None and spread > maximum_spread:
        blockers.append(f"Bid-ask spread {spread:.1%} is wider than the {maximum_spread:.0%} limit")
    if basket is not None:
        blockers.extend(f"Basket warning: {warning}" for warning in basket.warnings)
    basket_integrity_block = bool(
        basket is not None
        and (not basket.top_model_included or basket.middle_bucket_excluded)
    )

    reasons = [
        (
            f"{label} raw model probability {fair_probability:.1%} versus YES ask {buy_price:.1%}"
            if buy_price is not None
            else f"{label} raw model probability {fair_probability:.1%}"
        ),
        (
            f"Uncalibrated model-market gap {edge:+.1%}"
            if edge is not None
            else "No model-market comparison"
        ),
        f"Forecast confidence {confidence}/100",
    ]
    if probability_change is not None:
        reasons.append(f"Fair probability changed {probability_change:+.1%}")
    if basket is not None:
        reasons.append(
            f"Event basket {', '.join(basket.bucket_labels)}: "
            f"fair probability {basket.fair_probability:.1%}, total ask "
            f"{basket.total_cost:.1%}, combined edge {basket.edge:+.1%}"
        )
    reasons.extend(str(signal) for signal in live_signals if signal)

    hard_block = (
        day_status.is_locked
        or metar_pending
        or market_model_conflict
        or forecast_stale
        or basket_integrity_block
        or ("closed" in markets and markets.closed.fillna(False).astype(bool).all())
        or actionable.empty
        or not selected_is_top
        or (buy_price is not None and buy_price <= float(minimum_buy_price))
        or (edge is not None and edge >= float(maximum_model_market_gap))
    )
    if hard_block or edge is None or edge < watch_edge:
        status = "NO BET"
    elif not recommendations_enabled:
        status = "RESEARCH ONLY"
    elif edge >= bet_edge and not blockers:
        status = "BET"
    else:
        status = "WATCH"
    return TradeDecision(
        status,
        label,
        fair_probability,
        buy_price,
        edge,
        probability_change,
        confidence,
        tuple(reasons),
        tuple(blockers),
        basket,
    )


def balanced_hedge_plan(
    *,
    primary_bucket: str,
    primary_stake: float,
    primary_price: float,
    hedge_bucket: str,
    hedge_price: float,
) -> HedgePlan:
    """Balance gross payout across two selected, mutually exclusive YES buckets."""
    stake = max(0.0, float(primary_stake))
    price = float(primary_price)
    hedge_ask = float(hedge_price)
    if not 0 < price <= 1 or not 0 < hedge_ask <= 1:
        raise ValueError("Entry prices must be greater than zero and at most one")
    if primary_bucket == hedge_bucket:
        raise ValueError("Primary and hedge buckets must be different")
    primary_shares = stake / price
    hedge_stake = primary_shares * hedge_ask
    total_cost = stake + hedge_stake
    covered_payout = primary_shares
    covered_result = covered_payout - total_cost
    return HedgePlan(
        primary_bucket=primary_bucket,
        hedge_bucket=hedge_bucket,
        primary_stake=stake,
        primary_price=price,
        balanced_hedge_stake=hedge_stake,
        hedge_price=hedge_ask,
        total_cost=total_cost,
        covered_payout=covered_payout,
        covered_result=covered_result,
        uncovered_result=-total_cost,
    )


def hedge_outcome_table(
    *,
    outcome_buckets: Iterable[str],
    primary_bucket: str,
    primary_stake: float,
    primary_price: float,
    hedge_bucket: str,
    hedge_stake: float,
    hedge_price: float,
) -> list[dict[str, float | str]]:
    """Calculate net P/L for each mutually exclusive market outcome."""
    stake = max(0.0, float(primary_stake))
    hedge = max(0.0, float(hedge_stake))
    price = float(primary_price)
    hedge_ask = float(hedge_price)
    if not 0 < price <= 1 or not 0 < hedge_ask <= 1:
        raise ValueError("Entry prices must be greater than zero and at most one")
    total_cost = stake + hedge
    primary_shares = stake / price
    hedge_shares = hedge / hedge_ask
    rows = []
    for outcome in dict.fromkeys(str(value) for value in outcome_buckets):
        payout = 0.0
        if outcome == primary_bucket:
            payout += primary_shares
        if outcome == hedge_bucket:
            payout += hedge_shares
        rows.append(
            {
                "Outcome": outcome,
                "Payout": payout,
                "Net P/L": payout - total_cost,
            }
        )
    return rows
