"""Two corrections that improve real-world accuracy.

1. regime_offset: the recent warm/cool anomaly (observed minus climatology over
   the last ~30 days). Heat persists, so this nudges the climatology branch
   toward the current regime, decayed as lead time grows.

2. station_offset: a STATIC additive calibration (deg C) mapping the model grid
   point to the exact station the market resolves against (e.g. an airport METAR
   that reads consistently warmer than the ERA5 grid cell). Defaults to 0; set it
   in config once you've compared a few days of model output to the official
   resolution source. This is the single highest-leverage knob for market accuracy.
"""
from __future__ import annotations

from datetime import date, datetime

import numpy as np

from .climatology import Climatology


def regime_offset(recent: dict, clim: Climatology) -> float:
    """Mean (observed - climatology) over recent days. Robust to gaps/NaNs."""
    daily = recent["daily"]
    times = daily["time"]
    tmax = daily["temperature_2m_max"]
    diffs = []
    for t, obs in zip(times, tmax):
        if obs is None:
            continue
        d = datetime.fromisoformat(t).date()
        diffs.append(obs - clim.mean(d))
    if not diffs:
        return 0.0
    return float(np.median(diffs))  # median resists one-off outliers


def regime_decay(lead: int, halflife_days: float = 5.0) -> float:
    """Weight on the regime offset, decaying with forecast lead time."""
    lead = max(lead, 0)
    return float(0.5 ** (lead / halflife_days))
