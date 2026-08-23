from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

import pandas as pd

from .analytics import DayStatus, market_edges


@dataclass(frozen=True)
class FillEstimate:
    best_ask: float | None
    average_fill_price: float | None
    fee_per_share: float | None
    all_in_price: float | None
    slippage: float | None
    estimated_fee_usdc: float
    shares: float
    total_cost_usdc: float
    available_depth_usdc: float
    depth_at_best_usdc: float
    fully_fillable: bool


@dataclass(frozen=True)
class ShadowBasket:
    market_ids: tuple[str, ...]
    bucket_labels: tuple[str, ...]
    fair_probability: float
    total_cost: float
    net_edge: float
    top_model_bucket: str
    top_model_included: bool
    middle_bucket_excluded: bool
    status: str
    warnings: tuple[str, ...]


def taker_fee_per_share(price: float, fee_rate: float = 0.05) -> float:
    """Return Polymarket's dynamic taker fee for one share at ``price``."""
    probability = max(0.0, min(1.0, float(price)))
    return probability * max(0.0, float(fee_rate)) * (1.0 - probability)


def estimate_market_buy(
    asks: Iterable[dict],
    *,
    budget_usdc: float = 10.0,
    fee_rate: float = 0.05,
) -> FillEstimate:
    """Walk the YES ask book and estimate an all-in, immediately executable buy."""
    levels: list[tuple[float, float]] = []
    for level in asks:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < price < 1 and size > 0:
            levels.append((price, size))
    levels.sort(key=lambda item: item[0])
    requested_budget = max(0.0, float(budget_usdc))
    if not levels or requested_budget <= 0:
        return FillEstimate(
            best_ask=None,
            average_fill_price=None,
            fee_per_share=None,
            all_in_price=None,
            slippage=None,
            estimated_fee_usdc=0.0,
            shares=0.0,
            total_cost_usdc=0.0,
            available_depth_usdc=0.0,
            depth_at_best_usdc=0.0,
            fully_fillable=False,
        )

    best_ask = levels[0][0]
    available_depth = sum(
        size * (price + taker_fee_per_share(price, fee_rate))
        for price, size in levels
    )
    depth_at_best = sum(
        size * (price + taker_fee_per_share(price, fee_rate))
        for price, size in levels
        if abs(price - best_ask) < 1e-9
    )
    remaining = requested_budget
    shares = 0.0
    notional = 0.0
    fee = 0.0
    for price, size in levels:
        per_share_fee = taker_fee_per_share(price, fee_rate)
        all_in_per_share = price + per_share_fee
        level_shares = min(size, remaining / all_in_per_share)
        if level_shares <= 0:
            continue
        shares += level_shares
        notional += level_shares * price
        fee += level_shares * per_share_fee
        remaining -= level_shares * all_in_per_share
        if remaining <= 1e-7:
            remaining = 0.0
            break

    total_cost = notional + fee
    average_price = notional / shares if shares > 0 else None
    fee_per_share = fee / shares if shares > 0 else None
    all_in_price = total_cost / shares if shares > 0 else None
    return FillEstimate(
        best_ask=best_ask,
        average_fill_price=average_price,
        fee_per_share=fee_per_share,
        all_in_price=all_in_price,
        slippage=average_price - best_ask if average_price is not None else None,
        estimated_fee_usdc=fee,
        shares=shares,
        total_cost_usdc=total_cost,
        available_depth_usdc=available_depth,
        depth_at_best_usdc=depth_at_best,
        fully_fillable=remaining <= 1e-7,
    )


def _book_age_seconds(book: dict, captured_at: datetime) -> float | None:
    value = book.get("observed_at")
    if value is None:
        return None
    observed = pd.Timestamp(value)
    observed = observed.tz_localize("UTC") if observed.tzinfo is None else observed.tz_convert("UTC")
    captured = pd.Timestamp(captured_at)
    captured = (
        captured.tz_localize("UTC") if captured.tzinfo is None else captured.tz_convert("UTC")
    )
    return max(0.0, (captured - observed).total_seconds())


def evaluate_shadow_markets(
    *,
    airport: str,
    target: date,
    captured_at: datetime,
    timing: str,
    probabilities: dict[int, float],
    markets: pd.DataFrame,
    books: dict[str, dict],
    forecast_confidence: int,
    day_status: DayStatus,
    raw_probabilities: dict[int, float] | None = None,
    metar_pending: bool = False,
    market_model_conflict: bool = False,
    forecast_stale: bool = False,
    stake_usdc: float = 10.0,
    fee_rate: float = 0.05,
    safety_margin: float = 0.02,
    minimum_net_edge: float = 0.05,
    minimum_confidence: int = 65,
    maximum_spread: float = 0.12,
    maximum_book_age_seconds: float = 180.0,
    minimum_buy_price: float = 0.05,
    maximum_model_market_gap: float = 0.15,
    recommendations_enabled: bool = False,
) -> list[dict]:
    """Evaluate every bucket as a paper trade using actual CLOB depth.

    This function only returns journal rows. It has no wallet, authentication, or
    order-placement capability.
    """
    if markets.empty:
        return []
    market_frame = markets.copy()
    for column in ("best_bid", "best_ask", "spread", "volume", "liquidity"):
        if column not in market_frame:
            market_frame[column] = None
    comparison = market_edges(probabilities, market_frame)
    if comparison.empty:
        return []
    raw_comparison = market_edges(raw_probabilities or {}, market_frame)
    raw_by_market = (
        {
            str(row.market_id): float(row.model_probability)
            for row in raw_comparison.itertuples()
        }
        if not raw_comparison.empty
        else {}
    )
    top_market = comparison.sort_values(
        ["model_probability", "edge"],
        ascending=False,
    ).iloc[0]
    top_market_id = str(top_market.market_id)
    captured_at = captured_at.astimezone(timezone.utc)
    rows: list[dict] = []
    for market in comparison.itertuples():
        token_id = (
            str(market.token_id)
            if hasattr(market, "token_id") and pd.notna(market.token_id)
            else None
        )
        book = books.get(token_id or "", {})
        fill = estimate_market_buy(
            book.get("asks") or [],
            budget_usdc=stake_usdc,
            fee_rate=fee_rate,
        )
        book_bids = []
        for level in book.get("bids") or []:
            try:
                book_bids.append(float(level["price"]))
            except (KeyError, TypeError, ValueError):
                continue
        best_bid = max(book_bids) if book_bids else None
        spread = (
            fill.best_ask - best_bid
            if fill.best_ask is not None and best_bid is not None
            else None
        )
        gross_edge = (
            float(market.model_probability) - float(fill.average_fill_price)
            if fill.average_fill_price is not None
            else None
        )
        net_edge = (
            float(market.model_probability) - float(fill.all_in_price) - safety_margin
            if fill.all_in_price is not None
            else None
        )
        book_age = _book_age_seconds(book, captured_at)
        minimum_order_size = book.get("min_order_size")
        try:
            minimum_order_size = (
                float(minimum_order_size) if minimum_order_size is not None else None
            )
        except (TypeError, ValueError):
            minimum_order_size = None

        hard_blockers: list[str] = []
        soft_blockers: list[str] = []
        if day_status.is_locked:
            hard_blockers.append("The daily maximum is already locked")
        if metar_pending:
            hard_blockers.append(
                "A routine METAR is imminent or due but not yet available"
            )
        if market_model_conflict:
            hard_blockers.append("A near-certain market price conflicts with the weather model")
        if forecast_stale:
            hard_blockers.append("Fewer than two current weather models are available")
        if str(market.market_id) != top_market_id:
            hard_blockers.append(
                "The range is not Weatherman's most likely Polymarket bucket"
            )
        if bool(getattr(market, "closed", False)):
            hard_blockers.append("The market is closed")
        if token_id is None or not book:
            hard_blockers.append("No current CLOB order book is available")
        elif not fill.fully_fillable:
            hard_blockers.append(f"The order book cannot fill the ${stake_usdc:.0f} test stake")
        if (
            minimum_order_size is not None
            and fill.shares > 0
            and fill.shares < minimum_order_size
        ):
            hard_blockers.append("The estimated fill is below the market minimum order size")
        if book_age is not None and book_age > maximum_book_age_seconds:
            hard_blockers.append("The CLOB order book is stale")
        if fill.best_ask is not None and fill.best_ask <= float(minimum_buy_price):
            hard_blockers.append(
                f"YES ask is at or below the {minimum_buy_price:.0%} cheap-tail floor"
            )
        if gross_edge is not None and gross_edge >= float(maximum_model_market_gap):
            hard_blockers.append(
                f"Champion-market gap {gross_edge:.1%} is a conflict, not calibrated edge"
            )
        if not recommendations_enabled:
            soft_blockers.append(
                "Empirical probability calibration has not passed; research only"
            )
        if int(forecast_confidence) < minimum_confidence:
            soft_blockers.append(
                f"Forecast confidence {int(forecast_confidence)}/100 is below "
                f"{minimum_confidence}/100"
            )
        if spread is not None and spread > maximum_spread:
            soft_blockers.append(
                f"Bid-ask spread {spread:.1%} is wider than {maximum_spread:.0%}"
            )

        blockers = [*hard_blockers, *soft_blockers]
        if hard_blockers or net_edge is None or net_edge < 0:
            status = "NO BET"
        elif not recommendations_enabled:
            status = "RESEARCH ONLY"
        elif net_edge >= minimum_net_edge and not soft_blockers:
            status = "SHADOW BET"
        else:
            status = "WATCH"
        reasons = [
            f"Champion probability {float(market.model_probability):.1%}",
            (
                f"Average executable fill {fill.average_fill_price:.1%}"
                if fill.average_fill_price is not None
                else "No executable fill"
            ),
            (
                f"Estimated taker fee {fill.fee_per_share:.2%} per share"
                if fill.fee_per_share is not None
                else "Taker fee unavailable"
            ),
            (
                f"Uncalibrated gap after fee, slippage and safety margin {net_edge:+.1%}"
                if net_edge is not None
                else "Net edge unavailable"
            ),
        ]
        rows.append(
            {
                "airport": airport,
                "target_date": target,
                "event_slug": str(market.event_slug),
                "market_id": str(market.market_id),
                "token_id": token_id,
                "bucket_label": str(market.bucket_label),
                "captured_at": captured_at,
                "timing": timing,
                "fair_probability": float(market.model_probability),
                "raw_probability": (
                    raw_by_market[str(market.market_id)]
                    if str(market.market_id) in raw_by_market
                    else None
                ),
                "forecast_snapshot_at": captured_at,
                "displayed_probability": float(market.yes_price),
                "best_bid": best_bid,
                "best_ask": fill.best_ask,
                "average_fill_price": fill.average_fill_price,
                "fee_per_share": fill.fee_per_share,
                "all_in_price": fill.all_in_price,
                "slippage": fill.slippage,
                "fee_rate": float(fee_rate),
                "estimated_fee_usdc": fill.estimated_fee_usdc,
                "stake_usdc": float(stake_usdc),
                "shares": fill.shares,
                "total_cost_usdc": fill.total_cost_usdc,
                "available_depth_usdc": fill.available_depth_usdc,
                "depth_at_best_usdc": fill.depth_at_best_usdc,
                "fully_fillable": fill.fully_fillable,
                "gross_edge": gross_edge,
                "net_edge": net_edge,
                "safety_margin": float(safety_margin),
                "forecast_confidence": int(forecast_confidence),
                "day_phase": day_status.phase,
                "book_hash": str(book.get("hash")) if book.get("hash") else None,
                "book_age_seconds": book_age,
                "status": status,
                "blockers_json": json.dumps(blockers, separators=(",", ":")),
                "reasons_json": json.dumps(reasons, separators=(",", ":")),
            }
        )
    provisional_basket = build_shadow_basket(
        rows,
        markets,
        minimum_individual_net_edge=minimum_net_edge,
    )
    if provisional_basket is not None and (
        not provisional_basket.top_model_included
        or provisional_basket.middle_bucket_excluded
    ):
        warning = "Basket integrity guard: " + "; ".join(
            provisional_basket.warnings
        )
        selected_ids = set(provisional_basket.market_ids)
        for row in rows:
            if str(row["market_id"]) not in selected_ids:
                continue
            blockers = json.loads(str(row["blockers_json"]) or "[]")
            if warning not in blockers:
                blockers.append(warning)
            row["blockers_json"] = json.dumps(blockers, separators=(",", ":"))
            row["status"] = "NO BET"
    return rows


def build_shadow_basket(
    rows: list[dict],
    markets: pd.DataFrame,
    *,
    minimum_individual_net_edge: float = 0.05,
) -> ShadowBasket | None:
    """Combine simultaneous executable bucket edges into one event-level paper basket."""
    if not rows or markets.empty:
        return None
    frame = pd.DataFrame(rows)
    integrity_blocked = frame.get(
        "blockers_json",
        pd.Series("", index=frame.index, dtype=object),
    ).fillna("").astype(str).str.contains("Basket integrity guard:", regex=False)
    candidates = frame[
        frame.fully_fillable.fillna(False).astype(bool)
        & frame.all_in_price.notna()
        & frame.net_edge.notna()
        & (frame.net_edge >= float(minimum_individual_net_edge))
        & ((frame.status == "SHADOW BET") | integrity_blocked)
    ].copy()
    if len(candidates) < 2:
        return None
    market_order = markets.copy()

    def order_value(row: pd.Series) -> float:
        if pd.notna(row.get("bucket_low_c")):
            return float(row.bucket_low_c)
        if pd.notna(row.get("bucket_high_c")):
            return float(row.bucket_high_c) - 1000.0
        return 0.0

    market_order["_order"] = market_order.apply(order_value, axis=1)
    market_order = market_order.sort_values(["_order", "bucket_label"]).reset_index(drop=True)
    selected_ids = set(candidates.market_id.astype(str))
    positions = [
        index
        for index, market_id in enumerate(market_order.market_id.astype(str))
        if market_id in selected_ids
    ]
    middle_bucket_excluded = any(
        str(market_order.iloc[index].market_id) not in selected_ids
        for index in range(min(positions), max(positions) + 1)
    )
    top = frame.sort_values("fair_probability", ascending=False).iloc[0]
    top_label = str(top.bucket_label)
    top_included = str(top.market_id) in selected_ids
    warnings: list[str] = []
    if not top_included:
        warnings.append("Most likely bucket excluded")
    if middle_bucket_excluded:
        warnings.append("Middle bucket excluded")
    fair_probability = float(candidates.fair_probability.sum())
    total_cost = float(candidates.all_in_price.sum())
    safety_total = float(
        pd.to_numeric(candidates.safety_margin, errors="coerce").fillna(0.0).sum()
    )
    net_edge = fair_probability - total_cost - safety_total
    if total_cost >= 1.0:
        warnings.append("Basket costs at least the maximum $1 payout")
    return ShadowBasket(
        market_ids=tuple(candidates.market_id.astype(str)),
        bucket_labels=tuple(candidates.bucket_label.astype(str)),
        fair_probability=fair_probability,
        total_cost=total_cost,
        net_edge=net_edge,
        top_model_bucket=top_label,
        top_model_included=top_included,
        middle_bucket_excluded=middle_bucket_excluded,
        status="SHADOW BASKET" if not warnings and net_edge >= 0.05 else "BASKET WATCH",
        warnings=tuple(warnings),
    )
