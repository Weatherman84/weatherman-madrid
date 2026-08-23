from __future__ import annotations

import math
from datetime import timedelta

import pandas as pd

from .analytics import forecast_ladder_metrics


def filter_target_window(frame: pd.DataFrame, window_days: int) -> pd.DataFrame:
    """Keep a trailing target-day window while preserving an empty-frame schema."""
    if frame.empty or "target_date" not in frame:
        return frame
    result = frame.copy()
    result["target_date"] = pd.to_datetime(result.target_date).dt.date
    latest = max(result.target_date)
    cutoff = latest - timedelta(days=window_days - 1)
    return result[result.target_date >= cutoff].copy()


def market_bucket_hits(
    scored: pd.DataFrame,
    catalog: dict[str, dict],
) -> pd.DataFrame:
    """Add unit-aware Polymarket bucket hits without mixing C and F markets."""
    if scored.empty:
        return scored
    result = scored.copy()

    def bucket(value_c: float, airport: str) -> int:
        details = catalog.get(str(airport), {})
        unit = details.get("market_unit", "C")
        width = max(1, int(details.get("market_bucket_width", 1)))
        value = value_c * 9 / 5 + 32 if unit == "F" else value_c
        reported_integer = math.floor(value + 0.5)
        return math.floor(reported_integer / width)

    result["market_bucket_hit"] = result.apply(
        lambda row: bucket(float(row.forecast_c), str(row.airport))
        == bucket(float(row.max_temp_c), str(row.airport)),
        axis=1,
    )
    return result


def market_timing_metrics(
    scored: pd.DataFrame,
    catalog: dict[str, dict],
) -> pd.DataFrame:
    """Return weather and market-bucket accuracy for comparable information sets."""
    if scored.empty:
        return pd.DataFrame()
    frame = market_bucket_hits(scored, catalog)
    base = forecast_ladder_metrics(frame)
    market_hits = (
        frame.groupby(["airport", "timing", "lead_bucket", "stage"], as_index=False)
        .market_bucket_hit.mean()
        .rename(columns={"market_bucket_hit": "market_exact_hit"})
    )
    return base.merge(
        market_hits,
        on=["airport", "timing", "lead_bucket", "stage"],
        how="left",
    )
