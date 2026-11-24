"""Turn the multi-model ensemble response into a sample of daily-Tmax outcomes
for a target date. Each ensemble member is one plausible future, so the set of
members IS a (small) probability distribution. We smooth it with a little kernel
noise so downstream bucket probabilities aren't jagged.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np


def ensemble_members(ens: dict, target: date) -> Optional[np.ndarray]:
    """All member Tmax values for `target`, or None if outside the horizon."""
    daily = ens["daily"]
    times = daily["time"]
    iso = target.isoformat()
    if iso not in times:
        return None
    idx = times.index(iso)
    vals = []
    for key, series in daily.items():
        if not key.startswith("temperature_2m_max"):
            continue
        v = series[idx]
        if v is not None:
            vals.append(float(v))
    arr = np.array(vals, dtype=float)
    return arr if arr.size else None


def lead_days(target: date, today: date) -> int:
    return (target - today).days


def sample(ens: dict, target: date, n: int, rng: np.random.Generator,
           kernel_sd: float = 0.6) -> Optional[np.ndarray]:
    members = ensemble_members(ens, target)
    if members is None:
        return None
    picks = rng.choice(members, size=n, replace=True)
    return picks + rng.normal(0.0, kernel_sd, size=n)
