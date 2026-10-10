"""Public point archives for an isolated replay. Never imports database/runtime services.

Publication schedules are estimates, NOT historical availability evidence. Strict
replays reject them. Conservative mode exists only for partial-fidelity diagnostics.
"""

from __future__ import annotations

import calendar
import hashlib
import io
import json
import re
import time
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

VERSION = "historical_backfill_v0.2.0"
ZONE = "Europe/Madrid"
SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
METAR_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
TAF_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/taf.py"
AWC_TAF_URL = "https://aviationweather.gov/api/data/taf"
HOURLY_FIELDS = {
    "temperature_2m": "temp_c",
    "dew_point_2m": "dewpoint_c",
    "cloud_cover": "cloud_cover",
    "wind_speed_10m": "wind_kph",
    "wind_direction_10m": "wind_direction",
    "shortwave_radiation": "radiation_wm2",
}
# Deliberately conservative candidate schedules, not empirical availability.
# Native archive errors are retained per model; no substitutions or seamless fallback.
RUN_HOURS = {
    "ecmwf_ifs025": (0, 6, 12, 18),
    "gfs_global": (0, 6, 12, 18),
    "icon_global": (0, 6, 12, 18),
    "icon_eu": tuple(range(0, 24, 3)),
    "ukmo_global_deterministic_10km": (0, 6, 12, 18),
    "meteofrance_arpege_europe": (0, 6, 12, 18),
    "meteofrance_arome_france": tuple(range(0, 24, 3)),
    "meteofrance_arome_france_hd": tuple(range(0, 24, 3)),
}


class ArchiveError(RuntimeError):
    pass


class BudgetExceeded(ArchiveError):
    pass


def utc(value: object) -> pd.Timestamp:
    parsed = pd.Timestamp(value)
    if pd.isna(parsed) or parsed.tzinfo is None:
        raise ArchiveError(f"Missing or naive provenance timestamp: {value!r}")
    return parsed.tz_convert("UTC")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + "\n")
    temporary.replace(path)


class RawCache:
    """Content-addressed, checked cache; bounded retries and resumable failure ledger."""

    def __init__(self, root: Path, *, offline=False, max_requests=250, retry_failed=False):
        self.root = root
        self.offline = offline
        self.max_requests = max_requests
        self.retry_failed = retry_failed
        self.requests = self.download_bytes = self.hits = 0
        self.last_request = 0.0
        self.rate_limited = False
        self.lock = threading.Lock()
        ledger = root / "backfill_state.json"
        self.state = json.loads(ledger.read_text()) if ledger.exists() else {"requests": {}}
        self.client = httpx.Client(timeout=25, follow_redirects=True)

    def close(self):
        self.client.close()

    def is_cached(self, url: str, params: dict, *, kind: str) -> bool:
        key = digest(
            json.dumps({"url": url, "params": params, "kind": kind}, sort_keys=True).encode()
        )
        return self.state["requests"].get(key, {}).get("status") == "success"

    def get(self, url: str, params: dict, *, kind: str, read_only_post=False) -> tuple[bytes, dict]:
        request = {"url": url, "params": params, "kind": kind}
        if read_only_post:
            if url != "https://clob.polymarket.com/batch-prices-history" or kind != "market_json":
                raise ArchiveError("Only the read-only batch price-history POST is permitted")
            request["method"] = "POST"
        key = digest(json.dumps(request, sort_keys=True).encode())
        previous = self.state["requests"].get(key)
        if previous and previous["status"] == "success":
            data = (self.root / previous["path"]).read_bytes()
            if digest(data) != previous["sha256"]:
                raise ArchiveError("Immutable cache checksum mismatch")
            self.hits += 1
            return data, previous
        if self.offline or (previous and not self.retry_failed):
            raise ArchiveError("Offline cache miss" if not previous else previous["error"])
        error = "request failed"
        for attempt in range(2):
            with self.lock:
                if self.rate_limited:
                    raise BudgetExceeded("Provider rate limit reached; stop and resume later")
                if self.requests >= self.max_requests:
                    raise BudgetExceeded("API request budget exhausted; resume with existing cache")
                interval = (
                    21.0
                    if kind == "ogimet_taf"
                    else (2.0 if params.get("hourly") == "temperature_850hPa" else 1.1)
                )
                time.sleep(max(0.0, interval - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
                self.requests += 1
            try:
                response = (
                    self.client.post(url, json=params)
                    if read_only_post
                    else self.client.get(url, params=params)
                )
                with self.lock:
                    self.download_bytes += len(response.content)
                response.raise_for_status()
                data = response.content
                if kind == "awc_taf":
                    # Validate documented empty 204s without rewriting raw response bytes.
                    if not isinstance(json.loads(data or b"[]"), list):
                        raise ArchiveError("Unexpected AWC TAF schema")
                elif kind == "ogimet_taf":
                    if data.strip() and not data.startswith(b"LEMD,"):
                        raise ArchiveError("Unexpected OGIMET TAF content")
                elif kind == "market_json":
                    if not isinstance(json.loads(data), (dict, list)):
                        raise ArchiveError("Unexpected market JSON")
                elif kind == "json":
                    payload = json.loads(data)
                    if payload.get("error") or not payload.get("hourly", {}).get("time"):
                        raise ArchiveError("Missing archived hourly forecast")
                else:
                    expected = b"station,valid,metar" if kind == "metar" else b"station,valid,"
                    if not data.startswith(expected):
                        raise ArchiveError("Unexpected archive CSV schema/content")
                sha = digest(data)
                relative = (
                    f"raw/{sha}.{'json' if kind in {'json', 'awc_taf', 'market_json'} else 'csv'}"
                )
                destination = self.root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    destination.write_bytes(data)
                metadata = {
                    **request,
                    "status": "success",
                    "path": relative,
                    "sha256": sha,
                    "bytes": len(data),
                    "attempts": attempt + 1,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
                with self.lock:
                    self.state["requests"][key] = metadata
                    atomic_json(self.root / "backfill_state.json", self.state)
                return data, metadata
            except (httpx.HTTPError, ValueError, ArchiveError) as exc:
                error = str(exc)
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status and status < 500 and status != 429:
                    break
                if attempt == 0:
                    # Rate-limit stops avoid repeatedly hitting an unavailable provider.
                    if status == 429:
                        with self.lock:
                            self.rate_limited = True
                        break
                    time.sleep(2)
        with self.lock:
            self.state["requests"][key] = {
                **request,
                "status": "failed",
                "error": error,
                "attempts": attempt + 1,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_json(self.root / "backfill_state.json", self.state)
        if self.rate_limited:
            raise BudgetExceeded("Provider rate limit reached; stop and resume later")
        raise ArchiveError(error)


def candidate_runs(model: str, checkpoint: datetime, *, count=2) -> list[datetime]:
    delay = (
        3 if model in {"icon_eu", "meteofrance_arome_france", "meteofrance_arome_france_hd"} else 6
    )
    cutoff = utc(checkpoint).to_pydatetime() - timedelta(hours=delay, minutes=10)
    candidates = [
        datetime.combine(
            cutoff.date() - timedelta(days=day), datetime.min.time(), tzinfo=timezone.utc
        ).replace(hour=hour)
        for day in range(3)
        for hour in RUN_HOURS[model]
    ]
    return sorted((run for run in candidates if run <= cutoff), reverse=True)[:count]


def model_run(cache: RawCache, airport: dict, model: str, run: datetime) -> pd.DataFrame:
    return model_runs(cache, airport, [model], run)


def model_runs(cache: RawCache, airport: dict, models: list[str], run: datetime) -> pd.DataFrame:
    """Batch identical initializations; never blend model output or change provenance."""
    stored, missing = [], []
    directory = cache.root / "normalized/model-runs" / VERSION
    for model in models:
        path = directory / model / f"{run:%Y%m%dT%H%M}.parquet"
        checksum = path.with_suffix(".sha256")
        if path.exists() and checksum.exists():
            if digest(path.read_bytes()) != checksum.read_text().strip():
                raise ArchiveError("Normalized model cache checksum mismatch")
            frame = pd.read_parquet(path)
            raw = cache.root / "raw" / f"{frame.source_sha256.iloc[0]}.json"
            if not raw.exists() or digest(raw.read_bytes()) != frame.source_sha256.iloc[0]:
                raise ArchiveError("Normalized model provenance checksum mismatch")
            stored.append(frame)
            with cache.lock:
                cache.hits += 1
        else:
            missing.append(model)
    if not missing:
        return pd.concat(stored, ignore_index=True)
    data, meta = cache.get(
        SINGLE_RUNS_URL,
        {
            "latitude": airport["latitude"],
            "longitude": airport["longitude"],
            "models": ",".join(sorted(missing)),
            "run": run.strftime("%Y-%m-%dT%H:%M"),
            "hourly": ",".join(HOURLY_FIELDS),
            "timezone": "UTC",
            "forecast_days": 3,
        },
        kind="json",
    )
    hourly = json.loads(data)["hourly"]
    frames = stored
    for model in missing:
        suffix = f"_{model}" if len(missing) > 1 else ""
        frame = pd.DataFrame(
            {
                column: hourly.get(variable + suffix, [None] * len(hourly["time"]))
                for variable, column in HOURLY_FIELDS.items()
            }
        )
        frame["valid_at"] = pd.to_datetime(hourly["time"], utc=True)
        frame["model"] = model
        frame["model_run_at"] = run
        delay = 3 if len(RUN_HOURS[model]) == 8 else 6
        frame["available_at"] = run + timedelta(hours=delay, minutes=10)
        frame["availability_basis"] = "estimated_conservative_schedule"
        frame["fetched_at"] = pd.Timestamp(meta["fetched_at"])
        frame["source_sha256"] = meta["sha256"]
        frame["source"] = "open-meteo"
        frame["airport"] = "LEMD"
        frame["temp_850hpa_c"] = None
        frame = frame.dropna(subset=["temp_c"])
        if not frame.empty:
            path = directory / model / f"{run:%Y%m%dT%H%M}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp.parquet")
            frame.to_parquet(temporary, index=False)
            temporary.replace(path)
            path.with_suffix(".sha256").write_text(digest(path.read_bytes()) + "\n")
            frames.append(frame)
    if not frames:
        raise ArchiveError("No archived values for requested models")
    return pd.concat(frames, ignore_index=True)


def _temperature(token: str) -> float:
    return -float(token[1:]) if token.startswith("M") else float(token)


def metars(cache: RawCache, start: date, end: date) -> pd.DataFrame:
    data, meta = cache.get(
        METAR_URL,
        {
            "station": "LEMD",
            "data": "metar",
            "year1": start.year,
            "month1": start.month,
            "day1": start.day,
            "year2": end.year,
            "month2": end.month,
            "day2": end.day,
            "tz": "Etc/UTC",
            "format": "onlycomma",
            "report_type": [1, 2, 3, 4],
        },
        kind="metar",
    )
    rows = []
    covers = {"FEW": 20, "SCT": 45, "BKN": 75, "OVC": 100, "SKC": 0, "CLR": 0, "NSC": 0, "NCD": 0}
    for record in pd.read_csv(io.BytesIO(data)).to_dict("records"):
        raw = str(record.get("metar", ""))
        match = re.search(r"\b(M?\d{2})/(M?\d{2})\b", raw)
        if not match or record.get("station") != "LEMD":
            continue
        observed = pd.Timestamp(record["valid"], tz="UTC")
        wind = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G\d{2,3})?KT\b", raw)
        layers = re.findall(r"\b(FEW|SCT|BKN|OVC|SKC|CLR|NSC|NCD)(\d{3})?", raw)
        cloud = max([covers[c] for c, _ in layers], default=None)
        rows.append(
            {
                "airport": "LEMD",
                "observed_at": observed,
                "available_at": observed + timedelta(minutes=5),
                "availability_basis": "estimated_observation_plus_5_minutes",
                "temp_c": _temperature(match[1]),
                "dewpoint_c": _temperature(match[2]),
                "wind_kph": float(wind[2]) * 1.852 if wind else None,
                "wind_direction": float(wind[1]) if wind and wind[1] != "VRB" else None,
                "cloud_cover": 0 if "CAVOK" in raw else cloud,
                "cloud_base_ft": min([float(h) * 100 for _, h in layers if h], default=None),
                "raw": raw,
                "source_sha256": meta["sha256"],
                "fetched_at": meta["fetched_at"],
                "is_correction": " COR " in f" {raw} ",
            }
        )
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    # Conflicting revisions cannot be ordered by reception time in this archive.
    frame["revision_conflict"] = frame.groupby("observed_at").raw.transform("nunique") > 1
    return frame.drop_duplicates("observed_at", keep="first").sort_values("observed_at")


def group_time(issue: datetime, day: int, hour: int) -> datetime:
    candidates = []
    for offset in (-1, 0, 1):
        year, month0 = divmod(issue.year * 12 + issue.month - 1 + offset, 12)
        if day <= calendar.monthrange(year, month0 + 1)[1]:
            candidates.append(
                datetime(year, month0 + 1, day, tzinfo=timezone.utc) + timedelta(hours=hour)
            )
    return min(candidates, key=lambda value: abs(value - issue))


def tafs(
    cache: RawCache, start: date, end: date, *, checkpoints=(), iem_range=None
) -> pd.DataFrame:
    query_start, query_end = iem_range or (start, end)
    data, meta = cache.get(
        TAF_URL,
        {
            "station": "LEMD",
            "sts": f"{query_start}T00:00Z",
            "ets": f"{query_end}T00:00Z",
            "fmt": "csv",
            "tz": "UTC",
            "last": 0,
        },
        kind="taf",
    )
    archive = pd.read_csv(io.BytesIO(data))
    if not {"raw", "valid"} <= set(archive.columns):
        raise ArchiveError("TAF archive lacks bulletin/issue fields")
    rows = []
    for record in archive.drop_duplicates(["valid", "raw"]).to_dict("records"):
        if record.get("station") != "LEMD":
            continue
        raw = str(record["raw"]).strip()
        issue = pd.to_datetime(record["valid"], utc=True).to_pydatetime()
        validity = re.search(r"\b(\d{2})(\d{2})/(\d{2})(\d{2})\b", raw)
        if not validity:
            continue
        maximum = re.search(r"\bTX(M?\d{2})/(\d{2})(\d{2})Z\b", raw)
        known = issue + timedelta(minutes=5)
        rows.append(
            {
                "airport": "LEMD",
                "issue_time": issue,
                "available_at": known,
                "valid_from": group_time(issue, int(validity[1]), int(validity[2])),
                "valid_to": group_time(issue, int(validity[3]), int(validity[4])),
                "raw_taf": raw,
                "is_amended": bool(re.search(r"\bAMD\b", raw)),
                "is_corrected": bool(re.search(r"\bCOR\b", raw)),
                "max_temp_c": _temperature(maximum[1]) if maximum else None,
                "max_temp_at": group_time(issue, int(maximum[2]), int(maximum[3]))
                if maximum
                else None,
                # Decoded change groups not implemented: never fabricate empty conditions.
                "periods_json": None,
                "decoded_periods_available": False,
                "first_seen_at": None,
                "collected_at": None,
                "fetched_at": meta["fetched_at"],
                "backfilled": True,
                "availability_basis": "estimated_issue_plus_5_minutes",
                "content_hash": digest(raw.encode()),
                "source_sha256": meta["sha256"],
            }
        )
    rows = [
        row
        for row in rows
        if pd.Timestamp(start, tz="UTC") <= utc(row["issue_time"]) < pd.Timestamp(end, tz="UTC")
    ]
    for checkpoint in checkpoints:
        params = {
            "ids": "LEMD",
            "format": "json",
            "time": "issue",
            "date": utc(checkpoint).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        # The public AWC endpoint documents a 30-day retention window.
        # Cached historical bulletins remain replayable after provider retention expires.
        if utc(checkpoint) < pd.Timestamp.now(tz="UTC") - timedelta(
            days=30
        ) and not cache.is_cached(AWC_TAF_URL, params, kind="awc_taf"):
            continue
        payload, awc_meta = cache.get(
            AWC_TAF_URL,
            params,
            kind="awc_taf",
        )
        for report in json.loads(payload or b"[]"):
            row = awc_taf_row(report, awc_meta)
            if row is not None:
                rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(["issue_time", "content_hash", "available_at"])


def awc_taf_row(report: dict, meta: dict) -> dict | None:
    """Archive decoder, using the existing engine's period field schema."""
    if report.get("icaoId") != "LEMD" or not report.get("rawTAF"):
        return None
    issue = utc(report["issueTime"]).to_pydatetime()
    raw = report["rawTAF"]
    maximum = re.search(r"\bTX(M?\d{2})/(\d{2})(\d{2})Z\b", raw)
    known = utc(report["dbPopTime"]).to_pydatetime() if report.get("dbPopTime") else None
    if known is not None and known < issue:
        raise ArchiveError("TAF ingestion time precedes issue time")

    def epoch(value):
        return pd.to_datetime(value, unit="s", utc=True).isoformat() if value is not None else None

    periods = [
        {
            "time_from": epoch(p.get("timeFrom")),
            "time_to": epoch(p.get("timeTo")),
            "time_bec": epoch(p.get("timeBec")),
            "change": p.get("fcstChange"),
            "probability": p.get("probability"),
            "wind_direction": p.get("wdir"),
            "wind_speed_kt": p.get("wspd"),
            "wind_gust_kt": p.get("wgst"),
            "weather": p.get("wxString"),
            "visibility_sm": p.get("visib"),
            "clouds": p.get("clouds") or [],
        }
        for p in report.get("fcsts") or []
    ]
    return {
        "airport": "LEMD",
        "issue_time": issue,
        "available_at": known or issue + timedelta(minutes=5),
        "valid_from": pd.to_datetime(report["validTimeFrom"], unit="s", utc=True),
        "valid_to": pd.to_datetime(report["validTimeTo"], unit="s", utc=True),
        "raw_taf": raw,
        "is_amended": bool(re.search(r"\bAMD\b", raw)),
        "is_corrected": bool(re.search(r"\bCOR\b", raw)),
        "max_temp_c": _temperature(maximum[1]) if maximum else None,
        "max_temp_at": group_time(issue, int(maximum[2]), int(maximum[3])) if maximum else None,
        "periods_json": json.dumps(periods),
        "decoded_periods_available": bool(periods),
        "first_seen_at": known,
        "archive_ingested_at": known,
        "collected_at": meta["fetched_at"],
        "fetched_at": meta["fetched_at"],
        "backfilled": True,
        "source": "aviationweather.gov",
        "availability_basis": "verified_historical_availability"
        if known
        else "estimated_issue_plus_5_minutes",
        "availability_detail": "AWC archive dbPopTime; not historical application receipt",
        "content_hash": digest(raw.encode()),
        "source_sha256": meta["sha256"],
    }


def eligible(frame: pd.DataFrame, checkpoint: datetime, *, policy: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    cutoff = utc(checkpoint)
    known = pd.to_datetime(frame.available_at, utc=True, errors="coerce")
    mask = known.notna() & (known <= cutoff)
    if policy == "strict":
        mask &= frame.availability_basis.eq("verified_historical_availability")
    elif policy != "conservative":
        raise ArchiveError("Unknown timing policy")
    selected = frame[mask].copy()
    for column in ("observed_at", "issue_time", "model_run_at"):
        if column in selected and not selected.empty:
            times = pd.to_datetime(selected[column], utc=True, errors="coerce")
            bounded = pd.Series(False, index=selected.index)
            if column == "model_run_at" and "model_run_upper_bound_at" in selected:
                bounds = pd.to_datetime(
                    selected.model_run_upper_bound_at, utc=True, errors="coerce"
                )
                declared = selected.get(
                    "calibration_evidence", pd.Series(index=selected.index, dtype=object)
                )
                bounded = (
                    declared.eq("fixed_48h_lead_sensitivity_not_exact_d1_run")
                    & bounds.notna()
                    & (bounds <= cutoff)
                    & (bounds <= pd.to_datetime(selected.available_at, utc=True))
                )
            if (times.isna() & ~bounded).any() or (times > cutoff).any():
                raise ArchiveError(f"Leakage: {column} after checkpoint")
    if "revision_conflict" in selected:
        selected = selected[~selected.revision_conflict & ~selected.is_correction]
    return selected


def actuals_from_metars(observations: pd.DataFrame) -> pd.DataFrame:
    """All scheduled half-hour reports required; hourly maxima stay separate proxies."""
    if observations.empty:
        return pd.DataFrame()
    frame = observations.copy()
    frame["local_at"] = pd.to_datetime(frame.observed_at, utc=True).dt.tz_convert(ZONE)
    rows = []
    for day, group in frame.groupby(frame.local_at.dt.date):
        start = pd.Timestamp(day, tz=ZONE)
        end = pd.Timestamp(day + timedelta(days=1), tz=ZONE)
        expected = pd.date_range(start, end, freq="30min", inclusive="left").tz_convert("UTC")
        observed = pd.DatetimeIndex(pd.to_datetime(group.observed_at, utc=True))
        missing = expected.difference(observed)
        corrections = int(group.is_correction.sum()) if "is_correction" in group else 0
        complete = (
            not len(missing)
            and not group.revision_conflict.any()
            and not corrections
            and group.temp_c.notna().all()
        )
        maximum = float(group.temp_c.max())
        rows.append(
            {
                "airport": "LEMD",
                "target_date": day,
                "archive_metar_max_c": maximum,
                "max_temp_c": maximum if complete else None,
                "source": "stored-metar-compatible-archive" if complete else "archive-metar-proxy",
                "actual_complete": complete,
                "expected_reports": len(expected),
                "actual_reports": len(group),
                "missing_routine_reports": len(missing),
                "unknown_correction_reports": corrections,
                "has_revision_conflict": bool(group.revision_conflict.any()),
                "available_at": end + timedelta(minutes=10),
                "availability_basis": "estimated_day_end_plus_10_minutes",
            }
        )
    return pd.DataFrame(rows)
