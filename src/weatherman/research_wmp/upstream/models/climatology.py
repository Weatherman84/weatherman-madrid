"""Climatological baseline: harmonic regression of daily Tmax with a warming trend,
plus an empirical (non-Gaussian) anomaly distribution for any day of year.

T_clim(d, y) = a0 + trend*y + sum_k [ a_k sin(2pi k d/365.25) + b_k cos(...) ]

We fit the seasonal+trend mean by ordinary least squares, then keep the residuals
(observed - fitted). Spread for a target date is sampled from residuals in a
+/-window around its day-of-year, so seasonal heteroscedasticity is preserved.
"""
from __future__ import annotations

from datetime import date
from dataclasses import dataclass

import numpy as np
import pandas as pd

N_HARMONICS = 3
DAYS_PER_YEAR = 365.25


def _design(doy: np.ndarray, year_frac: np.ndarray) -> np.ndarray:
    cols = [np.ones_like(doy, dtype=float), year_frac]
    for k in range(1, N_HARMONICS + 1):
        ang = 2 * np.pi * k * doy / DAYS_PER_YEAR
        cols.append(np.sin(ang))
        cols.append(np.cos(ang))
    return np.column_stack(cols)


@dataclass
class Climatology:
    coeffs: np.ndarray
    resid: np.ndarray          # residuals aligned with resid_doy
    resid_doy: np.ndarray
    ref_year: float
    trend_per_year: float

    def mean(self, d: date) -> float:
        doy = np.array([d.timetuple().tm_yday], dtype=float)
        yf = np.array([d.year - self.ref_year], dtype=float)
        return float((_design(doy, yf) @ self.coeffs)[0])

    def anomaly_samples(self, d: date, window: int = 15) -> np.ndarray:
        """Residuals from years past whose day-of-year is within +/-window."""
        target = d.timetuple().tm_yday
        diff = np.abs(self.resid_doy - target)
        diff = np.minimum(diff, DAYS_PER_YEAR - diff)  # wrap around new year
        mask = diff <= window
        sample = self.resid[mask]
        return sample if sample.size >= 30 else self.resid

    def sample(self, d: date, n: int, rng: np.random.Generator,
               inflation: float = 1.0) -> np.ndarray:
        base = self.mean(d)
        anom = self.anomaly_samples(d)
        draws = rng.choice(anom, size=n, replace=True)
        if inflation != 1.0:
            mu = anom.mean()
            draws = mu + (draws - mu) * inflation
        return base + draws


def fit_climatology(archive: dict) -> Climatology:
    daily = archive["daily"]
    df = pd.DataFrame({"time": pd.to_datetime(daily["time"]),
                       "tmax": daily["temperature_2m_max"]}).dropna()
    doy = df["time"].dt.dayofyear.to_numpy(dtype=float)
    years = df["time"].dt.year.to_numpy(dtype=float)
    ref_year = float(np.median(years))
    year_frac = years - ref_year
    X = _design(doy, year_frac)
    y = df["tmax"].to_numpy(dtype=float)
    coeffs, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coeffs
    return Climatology(
        coeffs=coeffs,
        resid=resid,
        resid_doy=doy,
        ref_year=ref_year,
        trend_per_year=float(coeffs[1]),
    )
