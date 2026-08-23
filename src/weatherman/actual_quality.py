from __future__ import annotations

import pandas as pd


PROVENANCE_COLUMNS = ("source_actual", "actual_source", "source")


def _provenance(frame: pd.DataFrame) -> pd.Series | None:
    for column in PROVENANCE_COLUMNS:
        if column in frame:
            return frame[column].fillna("").astype(str).str.strip().str.casefold()
    return None


def settlement_grade_actuals(actuals: pd.DataFrame) -> pd.DataFrame:
    """Return final station/official Actuals suitable for OOS evidence.

    Legacy test/research frames without a provenance column remain readable. In the
    production schema provenance is present, so provisional and gridded fallback
    values cannot increment OOS or promotion evidence.
    """
    if actuals.empty:
        return actuals.copy()
    source = _provenance(actuals)
    if source is None:
        return actuals.copy()
    allowed = (
        source.eq("stored-metar-station")
        | source.eq("airport metar")
        | (
            source.str.contains("metar|station", regex=True)
            & ~source.str.contains("provisional", regex=False)
        )
        | source.str.contains("official|manual", regex=True)
    )
    return actuals[allowed].copy()


def nonprovisional_actuals(actuals: pd.DataFrame) -> pd.DataFrame:
    """Exclude rolling provisional values from any calibration fallback."""
    if actuals.empty:
        return actuals.copy()
    source = _provenance(actuals)
    if source is None:
        return actuals.copy()
    return actuals[~source.str.contains("provisional", regex=False)].copy()
