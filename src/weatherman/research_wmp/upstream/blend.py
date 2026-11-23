"""Combine the ensemble and climatology branches into a single Monte-Carlo
predictive distribution, then convert it into market-bucket probabilities and
betting edge.

Lead-time weighting: ensemble forecasts have real skill out to ~10-14 days, then
decay to climatology. w_ens is high near-term and fades with lead time. The
climatology branch is shifted by the (decayed) recent-regime offset. A static
station_offset is applied to everything to match the market's resolution source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np

from .models.bias_correct import regime_decay
from .models.climatology import Climatology

N_SAMPLES = 60_000


@dataclass
class Bucket:
    label: str
    low: float = float("-inf")    # inclusive lower bound (deg)
    high: float = float("inf")    # exclusive upper bound (deg)
    price: Optional[float] = None  # market-implied probability 0..1 (mid), if known
    bid: Optional[float] = None    # yes bid (0..1), if known
    ask: Optional[float] = None    # yes ask (0..1), if known


@dataclass
class Prediction:
    location: str
    target: date
    lead_days: int
    samples: np.ndarray
    w_ensemble: float
    station_offset: float
    regime: float
    buckets: list = field(default_factory=list)  # list[dict]
    mean: float = 0.0
    median: float = 0.0
    p10: float = 0.0
    p90: float = 0.0


def auto_buckets(center: float) -> list["Bucket"]:
    """Seven 1-degree buckets centered near `center`, with open ends."""
    lo = int(round(center)) - 3
    out = [Bucket(f"<= {lo}C", float("-inf"), float(lo))]
    for t in range(lo, lo + 6):
        out.append(Bucket(f"{t}-{t+1}C", float(t), float(t + 1)))
    out.append(Bucket(f">= {lo+6}C", float(lo + 6), float("inf")))
    return out


def ensemble_weight(lead: int, midpoint: float = 9.0, steepness: float = 2.0,
                    floor: float = 0.05, ceil: float = 0.95) -> float:
    """Logistic decay of ensemble trust as lead time grows."""
    w = 1.0 / (1.0 + np.exp((lead - midpoint) / steepness))
    return float(np.clip(w, floor, ceil))


def build_distribution(
    clim: Climatology,
    target: date,
    lead: int,
    ensemble_samples: Optional[np.ndarray],
    regime: float,
    station_offset: float,
    rng: np.random.Generator,
    n: int = N_SAMPLES,
    spread_inflation: float = 1.0,
) -> tuple[np.ndarray, float]:
    """Returns (samples, w_ensemble).

    spread_inflation widens the distribution around its own mean to fix
    overconfidence (calibrate it with backtest.walk_forward, which solves for the
    value that yields ~80% interval coverage). 1.0 = no change.
    """
    regime_shift = regime * regime_decay(lead)
    clim_samples = clim.sample(target, n, rng) + regime_shift

    if ensemble_samples is None or ensemble_samples.size == 0:
        w = 0.0
        samples = clim_samples
    else:
        w = ensemble_weight(lead)
        n_ens = int(round(w * n))
        ens = rng.choice(ensemble_samples, size=n_ens, replace=True)
        clim_part = clim_samples[: n - n_ens]
        samples = np.concatenate([ens, clim_part])

    if spread_inflation != 1.0:
        mu = samples.mean()
        samples = mu + (samples - mu) * spread_inflation

    return samples + station_offset, w


def summarize(samples: np.ndarray, buckets: list[Bucket]) -> dict:
    # de-vig: market mid-prices across mutually-exclusive buckets sum to >1 due to
    # the spread/vig; normalize so the comparison to model probs is apples-to-apples.
    vig_total = sum(b.price for b in buckets if b.price is not None)
    devig = vig_total if vig_total and vig_total > 0 else 1.0

    out_buckets = []
    for b in buckets:
        p = float(np.mean((samples >= b.low) & (samples < b.high)))
        row = {
            "label": b.label,
            "low": None if b.low == float("-inf") else b.low,
            "high": None if b.high == float("inf") else b.high,
            "model_p": p,
        }
        if b.price is not None:
            fair = b.price / devig                       # de-vigged market prob
            spread = (b.ask - b.bid) if (b.ask is not None and b.bid is not None) else None
            # buy at the ask if available (what you'd actually pay), else mid
            cost = b.ask if b.ask else b.price
            # trust the price if we have no book info (manual markets) or the
            # book is reasonably tight; distrust a wide live spread (illiquid)
            liquid = spread is None or spread <= 0.15
            row["market_p"] = round(b.price, 4)
            row["market_p_fair"] = round(fair, 4)
            row["edge"] = p - fair
            row["kelly"] = kelly_fraction(p, cost)
            if spread is not None:
                row["spread"] = round(spread, 4)
            row["liquid"] = bool(liquid)
            # only a real signal if the book is liquid AND edge clears the spread
            row["value"] = bool(liquid and row["edge"] > max((spread or 0) / 2, 0.03))
        out_buckets.append(row)
    return {
        "buckets": out_buckets,
        "vig": round(devig, 4),
        "mean": float(np.mean(samples)),
        "median": float(np.median(samples)),
        "p10": float(np.percentile(samples, 10)),
        "p90": float(np.percentile(samples, 90)),
    }


def kelly_fraction(model_p: float, market_p: float) -> float:
    """Kelly stake for a YES bet priced at market_p when our prob is model_p.
    Payout odds b = (1/price - 1). f* = (p*(b+1) - 1)/b. Clipped to [0, 0.25]."""
    if market_p <= 0 or market_p >= 1:
        return 0.0
    b = (1.0 / market_p) - 1.0
    f = (model_p * (b + 1.0) - 1.0) / b
    return float(np.clip(f, 0.0, 0.25))
