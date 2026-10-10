"""Causality and DST checks for the explicitly reduced research adapter."""

import importlib.util
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from weatherman.historical_archive import ArchiveError, eligible
from weatherman.model_freshness import assess_model_freshness

spec = importlib.util.spec_from_file_location(
    "seasonal", Path(__file__).parents[1] / "scripts/deep_seasonal_replay.py"
)
seasonal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seasonal)


def path(day, lead=2):
    times = pd.date_range(
        pd.Timestamp(day, tz="Europe/Madrid"),
        pd.Timestamp(day, tz="Europe/Madrid") + pd.DateOffset(days=1),
        freq="h",
        inclusive="left",
    ).tz_convert("UTC")
    return pd.DataFrame(
        dict(
            valid_at=times,
            model="gfs_global",
            lead_days=lead,
            target_date=day,
            temp_c=10.0,
            model_run_upper_bound_at=times - pd.Timedelta(days=lead),
            model_run_at=pd.NaT,
            source="open-meteo",
            airport="LEMD",
        )
    )


@pytest.mark.parametrize(
    "day,hours", [(date(2025, 3, 30), 23), (date(2025, 10, 26), 25), (date(2025, 11, 1), 24)]
)
def test_fixed_lead_local_day_is_complete_and_no_initialization_is_invented(day, hours):
    p, d = seasonal.fixed_daily(path(day))
    assert len(p) == hours and len(d) == 1
    assert d.model_run_at.isna().all()
    assert d.available_at.iloc[0] == p.model_run_upper_bound_at.max() + pd.Timedelta(
        hours=6, minutes=10
    )


def test_incomplete_temperature_day_is_never_scored_as_complete():
    f = path(date(2025, 11, 1)).iloc[:-1]
    with pytest.raises(ValueError, match="No objects to concatenate"):
        seasonal.fixed_daily(f)


def test_future_24h_day_is_not_backfilled_at_d1_checkpoint():
    p, d = seasonal.fixed_daily(path(date(2025, 11, 1), lead=1))
    cutoff = pd.Timestamp("2025-10-31T19:00:00Z")
    selected = seasonal.deep_eligible(p, cutoff, policy="conservative")
    assert selected.empty
    assert eligible(p.drop(columns="model_run_at"), cutoff, policy="conservative").empty


def test_fixed_lead_bound_is_required_even_in_reduced_fidelity():
    p, d = seasonal.fixed_daily(path(date(2025, 11, 1)))
    p.loc[p.index[0], "model_run_upper_bound_at"] = p.available_at.iloc[0] + pd.Timedelta(hours=1)
    with pytest.raises(ArchiveError, match="contributing-run bound"):
        seasonal.deep_eligible(p, pd.Timestamp("2025-11-01T08:00:00Z"), policy="conservative")


def test_daily_research_cadence_does_not_change_public_freshness():
    as_of = pd.Timestamp("2025-11-01T08:00:00Z")
    available = as_of - pd.Timedelta(hours=15)
    original = assess_model_freshness("gfs_global", as_of=as_of, available_at=available)
    reduced = seasonal.seasonal_freshness()("gfs_global", as_of=as_of, available_at=available)
    assert original.usable is False and reduced.usable is True
    assert assess_model_freshness("gfs_global", as_of=as_of, available_at=available) == original


def test_season_classification_needs_both_halves():
    assert seasonal.classification(9, 7, 60, 2) == "insufficient_evidence"
    assert seasonal.classification(9, -2, 60, 60) == "season_dependent"
    assert seasonal.classification(9, 7, 60, 60, drift=True) == "fragile"
