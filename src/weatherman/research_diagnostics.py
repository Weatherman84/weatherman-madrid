from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select

from . import __version__
from .db import (
    DailyActual,
    Forecast,
    ForecastSnapshot,
    MarketSnapshot,
    Observation,
    RegimeMemorySnapshot,
    Session,
    TafReport,
)
from .history import DEFAULT_ARCHIVE_DIRECTORY, read_archive_live
from .settings import trading_airports


DEFAULT_RESEARCH_DIRECTORY = Path("artifacts/research")


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _utc_series(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, utc=True, errors="coerce")


def analyze_peak_lock_candidates(
    snapshots: pd.DataFrame,
    observations: pd.DataFrame,
    timezone_by_airport: dict[str, str],
) -> dict[str, object]:
    """Evaluate a radiation-gate ablation without changing Day-Lock logic."""
    if snapshots.empty or observations.empty:
        return {
            "candidate_airport_days": 0,
            "later_production_locks": 0,
            "no_stored_production_lock": 0,
            "false_higher_bucket_locks": 0,
            "cases": [],
        }
    frame = snapshots.copy()
    frame["captured_at"] = _utc_series(frame.captured_at)
    frame["latest_metar_at"] = _utc_series(frame.latest_metar_at)
    frame["target_date"] = pd.to_datetime(frame.target_date).dt.date
    if "checkpoint_label" in frame:
        frame = frame[frame.checkpoint_label.isna()].copy()
    obs = observations.copy()
    obs["observed_at"] = _utc_series(obs.observed_at)
    obs = obs.dropna(subset=["observed_at", "temp_c"])
    candidates: dict[tuple[str, object], dict[str, object]] = {}
    locks: dict[tuple[str, object], pd.Timestamp] = {}
    for row in frame.sort_values("captured_at").itertuples():
        code = str(row.airport)
        timezone_name = timezone_by_airport.get(code)
        if not timezone_name or pd.isna(row.captured_at):
            continue
        local_at = row.captured_at.tz_convert(timezone_name)
        if local_at.date() != row.target_date:
            continue
        try:
            peak = json.loads(row.peak_lock_json or "{}")
            features = json.loads(row.features_json or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        key = (code, row.target_date)
        if peak.get("phase") == "locked":
            locks.setdefault(key, row.captured_at)
        if peak.get("phase") != "active" or local_at.hour < 16:
            continue
        remaining = peak.get("remaining_model_rise_c")
        heating_rate = features.get("observed_heating_rate_cph")
        observed_max = row.observed_max_c
        latest_metar = (
            pd.Timestamp(row.latest_metar_at)
            if not pd.isna(row.latest_metar_at)
            else row.latest_metar_at
        )
        if any(
            value is None or bool(pd.isna(value))
            for value in (remaining, heating_rate, observed_max, latest_metar)
        ):
            continue
        if float(remaining) > 0.4 or float(heating_rate) > 0.2:
            continue
        observation_age_hours = (
            row.captured_at - latest_metar
        ).total_seconds() / 3600
        if not 0 <= observation_age_hours <= 2:
            continue
        latest = obs[
            (obs.airport.astype(str) == code)
            & (obs.observed_at <= latest_metar)
            & (
                obs.observed_at
                >= latest_metar.to_pydatetime() - timedelta(seconds=90)
            )
        ].sort_values("observed_at")
        if latest.empty:
            continue
        latest_temp = float(latest.iloc[-1].temp_c)
        if latest_temp > float(observed_max) - 0.5:
            continue
        candidates.setdefault(
            key,
            {
                "airport": code,
                "target_date": row.target_date.isoformat(),
                "alternative_lock_at": row.captured_at.isoformat(),
                "alternative_lock_local": local_at.isoformat(),
                "observed_max_c": float(observed_max),
                "latest_observed_temp_c": latest_temp,
                "remaining_model_rise_c": float(remaining),
                "heating_rate_cph": float(heating_rate),
                "future_radiation_max_wm2": peak.get("future_radiation_max_wm2"),
            },
        )
    delays: list[float] = []
    false_locks = 0
    for key, case in candidates.items():
        code, target = key
        zone = ZoneInfo(timezone_by_airport[code])
        day = obs[obs.airport.astype(str) == code].copy()
        day["local_date"] = day.observed_at.dt.tz_convert(zone).dt.date
        day = day[day.local_date == target]
        final_max = float(day.temp_c.max()) if not day.empty else None
        case["confirmed_final_max_c"] = final_max
        initial_bucket = math.floor(float(case["observed_max_c"]) + 0.5)
        final_bucket = math.floor(final_max + 0.5) if final_max is not None else None
        false_lock = final_bucket is not None and final_bucket > initial_bucket
        case["false_higher_bucket_lock"] = false_lock
        false_locks += int(false_lock)
        production_lock = locks.get(key)
        if production_lock is None:
            case["production_lock_at"] = None
            case["production_lock_delay_minutes"] = None
        else:
            alternative = pd.Timestamp(case["alternative_lock_at"])
            delay = max(0.0, (production_lock - alternative).total_seconds() / 60)
            delays.append(delay)
            case["production_lock_at"] = production_lock.isoformat()
            case["production_lock_delay_minutes"] = delay
    cases = sorted(candidates.values(), key=lambda item: (item["target_date"], item["airport"]))
    return {
        "candidate_rule": (
            "Current production gates except the residual-radiation gate; requires "
            "fresh METAR, local time >=16:00, heating <=0.2 C/h, remaining model "
            "rise <=0.4 C and cooling >=0.5 C from the observed maximum."
        ),
        "candidate_airport_days": len(cases),
        "later_production_locks": len(delays),
        "no_stored_production_lock": len(cases) - len(delays),
        "median_production_lock_delay_minutes": (
            float(pd.Series(delays).median()) if delays else None
        ),
        "false_higher_bucket_locks": false_locks,
        "production_logic_changed": False,
        "cases": cases,
    }


def peak_lock_research_report(
    report_path: Path = DEFAULT_RESEARCH_DIRECTORY / "peak-lock-diagnostic.json",
) -> dict[str, object]:
    """Read archive plus live data and write a research-only diagnostic artifact."""
    catalog = trading_airports()
    with Session() as session:
        snapshots = read_archive_live(ForecastSnapshot, session.bind)
        observations = read_archive_live(Observation, session.bind)
    result = analyze_peak_lock_candidates(
        snapshots,
        observations,
        {code: details["timezone"] for code, details in catalog.items()},
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": __version__,
        "classification": "RESEARCH ONLY",
        "writes_production_database": False,
        **result,
    }
    _atomic_text(report_path, json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def _coverage_row(
    frame: pd.DataFrame,
    *,
    airport: str,
    data_type: str,
    time_column: str,
) -> dict[str, object]:
    selected = frame[frame.airport.astype(str) == airport].copy() if not frame.empty else frame
    timestamps = (
        _utc_series(selected[time_column]).dropna()
        if not selected.empty and time_column in selected
        else pd.Series(dtype="datetime64[ns, UTC]")
    )
    return {
        "airport": airport,
        "data_type": data_type,
        "rows": int(len(selected)),
        "first_at": timestamps.min().isoformat() if not timestamps.empty else None,
        "last_at": timestamps.max().isoformat() if not timestamps.empty else None,
        "evidence_class": "historical-causal" if not selected.empty else "unavailable",
    }


def replay_readiness_report(
    report_path: Path = DEFAULT_RESEARCH_DIRECTORY / "replay-readiness.json",
) -> dict[str, object]:
    """Build a provider/coverage matrix without mutating any operational state."""
    catalog = trading_airports()
    with Session() as session:
        forecasts = read_archive_live(Forecast, session.bind)
        observations = read_archive_live(Observation, session.bind)
        tafs = read_archive_live(TafReport, session.bind)
        actuals = read_archive_live(DailyActual, session.bind)
        snapshots = read_archive_live(ForecastSnapshot, session.bind)
        markets = read_archive_live(MarketSnapshot, session.bind)
        regimes = read_archive_live(RegimeMemorySnapshot, session.bind)
        # Execute an explicit read statement so tests can assert this command never
        # depends on ORM flushes or implicit writes.
        session.execute(select(Forecast.id).limit(1)).all()

    provider_matrix: list[dict[str, object]] = []
    if not forecasts.empty:
        forecasts["target_date"] = pd.to_datetime(forecasts.target_date).dt.date
        for (airport, source, model, timing), group in forecasts.groupby(
            ["airport", "source", "model", "horizon"], dropna=False
        ):
            available = pd.to_datetime(
                group.get("available_at"), utc=True, errors="coerce"
            )
            fetched = pd.to_datetime(group.get("fetched_at"), utc=True, errors="coerce")
            source_name = str(source)
            evidence = (
                "reconstructed-research"
                if source_name in {"previous-runs", "historical-forecast"}
                else "historical-causal"
                if available.notna().any()
                else "unavailable"
            )
            provider_matrix.append(
                {
                    "airport": str(airport),
                    "source": source_name,
                    "model": str(model),
                    "timing": str(timing),
                    "rows": int(len(group)),
                    "target_days": int(group.target_date.nunique()),
                    "first_target": min(group.target_date).isoformat(),
                    "last_target": max(group.target_date).isoformat(),
                    "available_at_rows": int(available.notna().sum()),
                    "fetched_at_rows": int(fetched.notna().sum()),
                    "evidence_class": evidence,
                }
            )

    source_matrix: list[dict[str, object]] = []
    for code in catalog:
        source_matrix.extend(
            [
                _coverage_row(
                    observations,
                    airport=code,
                    data_type="METAR",
                    time_column="observed_at",
                ),
                _coverage_row(
                    tafs,
                    airport=code,
                    data_type="TAF",
                    time_column="issue_time",
                ),
                _coverage_row(
                    snapshots,
                    airport=code,
                    data_type="forecast-snapshot",
                    time_column="captured_at",
                ),
                _coverage_row(
                    markets,
                    airport=code,
                    data_type="market",
                    time_column="captured_at",
                ),
                _coverage_row(
                    regimes,
                    airport=code,
                    data_type="analog-regime",
                    time_column="captured_at",
                ),
            ]
        )
        airport_actuals = (
            actuals[actuals.airport.astype(str) == code].copy()
            if not actuals.empty
            else actuals
        )
        final_station_days = (
            int(
                airport_actuals.source.fillna("")
                .astype(str)
                .eq("stored-metar-station")
                .sum()
            )
            if not airport_actuals.empty and "source" in airport_actuals
            else 0
        )
        source_matrix.append(
            {
                "airport": code,
                "data_type": "Actual",
                "rows": int(len(airport_actuals)),
                "final_station_days": final_station_days,
                "evidence_class": (
                    "mixed-station-and-reanalysis"
                    if final_station_days and final_station_days < len(airport_actuals)
                    else "historical-causal"
                    if final_station_days
                    else "reconstructed-research"
                ),
            }
        )

    checkpoint_matrix: list[dict[str, object]] = []
    if not snapshots.empty and "checkpoint_label" in snapshots:
        checkpoint_rows = snapshots[snapshots.checkpoint_label.notna()].copy()
        for (airport, label, status), group in checkpoint_rows.groupby(
            ["airport", "checkpoint_label", "checkpoint_status"], dropna=False
        ):
            age_values = pd.to_numeric(
                group.get(
                    "source_age_at_checkpoint_minutes",
                    group.get("checkpoint_gap_minutes"),
                ),
                errors="coerce",
            ).dropna()
            checkpoint_matrix.append(
                {
                    "airport": str(airport),
                    "checkpoint": str(label),
                    "status": str(status),
                    "rows": int(len(group)),
                    "median_source_age_minutes": (
                        float(age_values.median()) if not age_values.empty else None
                    ),
                }
            )

    manifest_path = DEFAULT_ARCHIVE_DIRECTORY / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {}
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": __version__,
        "classification": "RESEARCH ONLY",
        "writes_production_database": False,
        "provider_matrix": provider_matrix,
        "source_matrix": source_matrix,
        "checkpoint_matrix": checkpoint_matrix,
        "archive_partition_count": int(manifest.get("partition_count", 0)),
        "future_leakage_controls": [
            "Use provider available_at for historical eligibility.",
            "Never relabel a later fetch as an original scheduled snapshot.",
            "Label previous-runs and historical-forecast as reconstructed-research.",
            "Treat missing inputs as unavailable; do not impute production evidence.",
            "Run replay outputs outside the production database and promotion counters.",
        ],
    }
    _atomic_text(report_path, json.dumps(report, indent=2, sort_keys=True) + "\n")
    matrix_path = report_path.with_suffix(".providers.csv")
    _atomic_text(matrix_path, pd.DataFrame(provider_matrix).to_csv(index=False))
    return report
