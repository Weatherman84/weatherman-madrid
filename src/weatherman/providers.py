from __future__ import annotations

import json
import calendar
import hashlib
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .settings import settings

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_METADATA_URL = "https://api.open-meteo.com/data/{model}/static/meta.json"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
METAR_URL = "https://aviationweather.gov/api/data/metar"
TAF_URL = "https://aviationweather.gov/api/data/taf"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"

MONTH_SLUGS = (
    "",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

# Forecast API model names are not always identical to the storage identifiers
# used by Open-Meteo's authoritative model-update metadata endpoint.
OPEN_METEO_METADATA_MODELS = {
    "ecmwf_ifs025": "ecmwf_ifs025",
    "gfs_global": "ncep_gfs025",
    "icon_global": "dwd_icon",
    "icon_eu": "dwd_icon_eu",
    "ukmo_global_deterministic_10km": "ukmo_global_deterministic_10km",
    "meteofrance_arpege_europe": "meteofrance_arpege_europe",
    "meteofrance_arome_france": "meteofrance_arome_france0025",
    "meteofrance_arome_france_hd": "meteofrance_arome_france_hd",
    "knmi_harmonie_arome_europe": "knmi_harmonie_arome_europe",
    "knmi_harmonie_arome_netherlands": "knmi_harmonie_arome_netherlands",
}


def _get(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    attempts: int = 5,
    timeout: float | None = None,
) -> dict | list:
    last_error: Exception | None = None
    with httpx.Client(
        timeout=settings.timeout if timeout is None else timeout,
        follow_redirects=True,
        headers={"User-Agent": "Weatherman/10.7.10 temperature-market research"},
    ) as client:
        for attempt in range(attempts):
            try:
                response = client.get(url, params=params)
                if response.status_code == 204:
                    return []
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code == 429 or exc.response.status_code >= 500
                )
                if not retryable or attempt == attempts - 1:
                    raise
                retry_after = None
                if isinstance(exc, httpx.HTTPStatusError):
                    retry_after = exc.response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                print(f"WARN temporary API error; retrying in {delay:.0f}s")
                time.sleep(delay)
    if last_error:
        raise last_error
    raise RuntimeError("API request failed without a response")


def _post(
    url: str,
    payload: list[dict] | dict,
    *,
    attempts: int = 3,
    timeout: float | None = None,
) -> dict | list:
    last_error: Exception | None = None
    with httpx.Client(
        timeout=settings.timeout if timeout is None else timeout,
        follow_redirects=True,
        headers={"User-Agent": "Weatherman/10.7.10 temperature-market research"},
    ) as client:
        for attempt in range(attempts):
            try:
                response = client.post(url, json=payload)
                if response.status_code == 204:
                    return []
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code == 429 or exc.response.status_code >= 500
                )
                if not retryable or attempt == attempts - 1:
                    raise
                delay = 2**attempt
                print(f"WARN temporary CLOB API error; retrying in {delay:.0f}s")
                time.sleep(delay)
    if last_error:
        raise last_error
    raise RuntimeError("CLOB API request failed without a response")


def _unix_datetime(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def open_meteo_model_metadata(
    model: str,
    *,
    attempts: int = 2,
    timeout: float = 8,
) -> dict[str, object]:
    """Return authoritative initialization and API-availability metadata."""
    storage_model = OPEN_METEO_METADATA_MODELS.get(model)
    if storage_model is None:
        return {
            "model_run_at": None,
            "available_at": None,
            "provenance_status": "No Open-Meteo metadata mapping",
        }
    try:
        payload = _get(
            OPEN_METEO_METADATA_URL.format(model=storage_model),
            attempts=attempts,
            timeout=timeout,
        )
    except Exception:
        return {
            "model_run_at": None,
            "available_at": None,
            "provenance_status": "Open-Meteo model metadata temporarily unavailable",
        }
    if not isinstance(payload, dict):
        return {
            "model_run_at": None,
            "available_at": None,
            "provenance_status": "Invalid Open-Meteo model metadata",
        }
    model_run_at = _unix_datetime(payload.get("last_run_initialisation_time"))
    available_at = _unix_datetime(payload.get("last_run_availability_time"))
    return {
        "model_run_at": model_run_at,
        "available_at": available_at,
        "provenance_status": (
            "Open-Meteo authoritative model metadata"
            if model_run_at is not None and available_at is not None
            else "Open-Meteo metadata missing run timestamps"
        ),
    }


def open_meteo_forecast(
    airport: dict,
    model: str,
    days: int = 3,
    *,
    attempts: int = 5,
    timeout: float | None = None,
    metadata_attempts: int = 2,
    metadata_timeout: float = 8,
) -> list[dict]:
    payload = _get(
        FORECAST_URL,
        {
            "latitude": airport["latitude"],
            "longitude": airport["longitude"],
            "daily": "temperature_2m_max",
            "timezone": airport["timezone"],
            "forecast_days": days,
            "models": model,
        },
        attempts=attempts,
        timeout=timeout,
    )
    daily = payload.get("daily", {})
    fetched_at = datetime.now(timezone.utc)
    metadata = open_meteo_model_metadata(
        model,
        attempts=metadata_attempts,
        timeout=metadata_timeout,
    )
    model_run_at = metadata["model_run_at"]
    available_at = metadata["available_at"]
    provenance_status = str(metadata["provenance_status"])
    if (
        isinstance(available_at, datetime)
        and fetched_at - available_at < timedelta(minutes=10)
    ):
        provenance_status += " · propagation window under 10 minutes"
    return [
        {
            "model": model,
            "run_at": fetched_at,
            "target_date": date.fromisoformat(day),
            "max_temp_c": float(value),
            "source": "open-meteo",
            "horizon": _forecast_horizon(
                fetched_at, date.fromisoformat(day), airport["timezone"]
            ),
            "model_run_at": model_run_at,
            "available_at": available_at,
            "fetched_at": fetched_at,
            "provenance_status": provenance_status,
        }
        for day, value in zip(daily.get("time", []), daily.get("temperature_2m_max", []))
        if value is not None
    ]


def _forecast_horizon(run_at: datetime, target_date: date, timezone_name: str) -> str:
    local_run = run_at.astimezone(ZoneInfo(timezone_name))
    if target_date == local_run.date() + timedelta(days=1):
        return "D-1"
    if target_date > local_run.date() + timedelta(days=1):
        return "D-2+"
    if target_date == local_run.date() and local_run.hour <= 10:
        return "D0-morning"
    return "Live"


def open_meteo_hourly(
    airport: dict,
    model: str,
    days: int = 3,
    *,
    attempts: int = 5,
    timeout: float | None = None,
) -> list[dict]:
    variables = [
        "temperature_2m",
        "dew_point_2m",
        "cloud_cover",
        "wind_speed_10m",
        "wind_direction_10m",
        "shortwave_radiation",
        "temperature_850hPa",
    ]
    try:
        payload = _get(
            FORECAST_URL,
            {
                "latitude": airport["latitude"],
                "longitude": airport["longitude"],
                "hourly": ",".join(variables),
                "timezone": "UTC",
                "forecast_days": days,
                "models": model,
            },
            attempts=attempts,
            timeout=timeout,
        )
    except httpx.HTTPStatusError:
        # Some regional models do not expose 850 hPa. The surface-based
        # nowcast remains useful and should not be discarded.
        variables = variables[:-1]
        payload = _get(
            FORECAST_URL,
            {
                "latitude": airport["latitude"],
                "longitude": airport["longitude"],
                "hourly": ",".join(variables),
                "timezone": "UTC",
                "forecast_days": days,
                "models": model,
            },
            attempts=attempts,
            timeout=timeout,
        )
    hourly = payload.get("hourly", {})
    run_at = datetime.now(timezone.utc)
    rows = []
    for index, timestamp in enumerate(hourly.get("time", [])):
        temp = hourly.get("temperature_2m", [None])[index]
        if temp is None:
            continue

        def value(name: str) -> float | None:
            values = hourly.get(name)
            item = values[index] if values and index < len(values) else None
            return float(item) if item is not None else None

        rows.append(
            {
                "model": model,
                "run_at": run_at,
                "valid_at": datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc),
                "temp_c": float(temp),
                "dewpoint_c": value("dew_point_2m"),
                "cloud_cover": value("cloud_cover"),
                "wind_kph": value("wind_speed_10m"),
                "wind_direction": value("wind_direction_10m"),
                "radiation_wm2": value("shortwave_radiation"),
                "temp_850hpa_c": value("temperature_850hPa"),
            }
        )
    return rows


def meteoblue_forecast(
    airport: dict,
    *,
    attempts: int = 5,
    timeout: float | None = None,
) -> list[dict]:
    if not settings.meteoblue_api_key or not settings.meteoblue_url_template:
        return []
    url = settings.meteoblue_url_template.format(
        lat=airport["latitude"],
        lon=airport["longitude"],
        elevation=airport["elevation_m"],
        apikey=settings.meteoblue_api_key,
    )
    payload = _get(url, attempts=attempts, timeout=timeout)
    daily = payload.get("data_day", {})
    metadata = payload.get("metadata") or {}
    times = daily.get("time", [])
    temps = daily.get("temperature_max", [])
    fetched_at = datetime.now(timezone.utc)
    model_run_at = _api_datetime(
        payload.get("modelrun_utc") or metadata.get("modelrun_utc")
    )
    available_at = _api_datetime(
        payload.get("modelrun_updatetime_utc")
        or metadata.get("modelrun_updatetime_utc")
    )
    return [
        {
            "model": "meteoblue",
            "run_at": fetched_at,
            "target_date": date.fromisoformat(day[:10]),
            "max_temp_c": float(value),
            "source": "meteoblue",
            "horizon": _forecast_horizon(
                fetched_at, date.fromisoformat(day[:10]), airport["timezone"]
            ),
            "model_run_at": model_run_at,
            "available_at": available_at,
            "fetched_at": fetched_at,
            "provenance_status": (
                "meteoblue mLM model run"
                if model_run_at is not None
                else "meteoblue run metadata unavailable"
            ),
        }
        for day, value in zip(times, temps)
        if value is not None
    ]


def historical_actuals(airport: dict, start: date, end: date) -> list[dict]:
    payload = _get(
        ARCHIVE_URL,
        {
            "latitude": airport["latitude"],
            "longitude": airport["longitude"],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": "temperature_2m_max",
            "timezone": airport["timezone"],
        },
    )
    daily = payload.get("daily", {})
    return [
        {"target_date": date.fromisoformat(day), "max_temp_c": float(value)}
        for day, value in zip(daily.get("time", []), daily.get("temperature_2m_max", []))
        if value is not None
    ]


def historical_model(airport: dict, model: str, start: date, end: date) -> list[dict]:
    payload = _get(
        HISTORICAL_FORECAST_URL,
        {
            "latitude": airport["latitude"],
            "longitude": airport["longitude"],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": "temperature_2m_max",
            "timezone": airport["timezone"],
            "models": model,
        },
    )
    daily = payload.get("daily", {})
    # Historical Forecast API is a reconstructed archive; label it explicitly.
    return [
        {
            "model": model,
            "run_at": datetime.combine(
                date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc
            ),
            "target_date": date.fromisoformat(day),
            "max_temp_c": float(value),
            "source": "historical-forecast",
            "horizon": "Legacy",
        }
        for day, value in zip(daily.get("time", []), daily.get("temperature_2m_max", []))
        if value is not None
    ]


def previous_run_d1(airport: dict, model: str, start: date, end: date) -> list[dict]:
    variable = "temperature_2m_previous_day1"
    payload = _get(
        PREVIOUS_RUNS_URL,
        {
            "latitude": airport["latitude"],
            "longitude": airport["longitude"],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": variable,
            "timezone": airport["timezone"],
            "models": model,
        },
    )
    hourly = payload.get("hourly", {})
    maxima: dict[date, float] = {}
    for timestamp, value in zip(hourly.get("time", []), hourly.get(variable, [])):
        if value is None:
            continue
        target = date.fromisoformat(timestamp[:10])
        maxima[target] = max(maxima.get(target, float("-inf")), float(value))
    tz = ZoneInfo(airport["timezone"])
    rows = []
    for target, max_temp in maxima.items():
        # ``run_at`` is only a stable row key. The maximum itself combines each
        # valid hour's value produced exactly 24 hours earlier; it is not a
        # single noon or evening model initialization.
        run_local = datetime.combine(target, datetime.min.time(), tzinfo=tz).replace(hour=12)
        rows.append(
            {
                "model": model,
                "run_at": run_local.astimezone(timezone.utc) - timedelta(days=1),
                "target_date": target,
                "max_temp_c": max_temp,
                "source": "previous-runs",
                "horizon": "D-1",
                "model_run_at": run_local.astimezone(timezone.utc) - timedelta(days=1),
                "available_at": None,
                "fetched_at": datetime.now(timezone.utc),
                "provenance_status": "Open-Meteo Previous Runs · per-hour 24h lead",
            }
        )
    return rows


def recent_metars(
    icao: str,
    hours: int = 24,
    *,
    attempts: int = 5,
    timeout: float | None = None,
) -> list[dict]:
    payload = _get(
        METAR_URL,
        {"ids": icao, "format": "json", "hours": hours},
        attempts=attempts,
        timeout=timeout,
    )
    rows = []
    for row in payload or []:
        observed = row.get("obsTime") or row.get("reportTime")
        if observed is None or row.get("temp") is None:
            continue
        if isinstance(observed, (int, float)):
            observed_at = datetime.fromtimestamp(observed, tz=timezone.utc)
        else:
            text = str(observed).replace("Z", "+00:00")
            observed_at = datetime.fromisoformat(text)
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
        try:
            wind_direction = float(row["wdir"]) if row.get("wdir") is not None else None
        except (TypeError, ValueError):
            # Variable winds are commonly reported as VRB and have no single direction.
            wind_direction = None
        cloud_cover, cloud_base_ft = _metar_clouds(row, str(row.get("rawOb") or ""))
        rows.append(
            {
                "observed_at": observed_at.astimezone(timezone.utc),
                "temp_c": float(row["temp"]),
                "dewpoint_c": float(row["dewp"]) if row.get("dewp") is not None else None,
                "wind_kph": (float(row["wspd"]) * 1.852 if row.get("wspd") is not None else None),
                "wind_direction": wind_direction,
                "cloud_cover": cloud_cover,
                "cloud_base_ft": cloud_base_ft,
                "raw": row.get("rawOb"),
            }
        )
    return sorted(rows, key=lambda item: item["observed_at"])


def _metar_clouds(row: dict, raw: str) -> tuple[float | None, float | None]:
    """Convert decoded/raw METAR layers to a conservative total-cover proxy."""
    cover_map = {"SKC": 0.0, "CLR": 0.0, "NSC": 0.0, "NCD": 0.0,
                 "FEW": 20.0, "SCT": 45.0, "BKN": 75.0, "OVC": 100.0}
    covers: list[float] = []
    bases: list[float] = []
    for layer in row.get("clouds") or []:
        token = str(layer.get("cover") or "").upper()
        if token in cover_map:
            covers.append(cover_map[token])
        base = layer.get("base")
        try:
            if base is not None:
                bases.append(float(base))
        except (TypeError, ValueError):
            pass
    for token, height in re.findall(
        r"\b(SKC|CLR|NSC|NCD|FEW|SCT|BKN|OVC)(\d{3})?\b", raw.upper()
    ):
        covers.append(cover_map[token])
        if height:
            bases.append(float(height) * 100)
    if "CAVOK" in raw.upper():
        covers.append(0.0)
    return (
        max(covers) if covers else None,
        min(bases) if bases else None,
    )


def _api_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _taf_group_datetime(issue_time: datetime, day: int, hour: int) -> datetime:
    """Resolve a DDHH TAF group around an issue time, including month rollover."""
    candidates: list[datetime] = []
    for month_offset in (-1, 0, 1):
        month_index = issue_time.year * 12 + issue_time.month - 1 + month_offset
        year, month_zero = divmod(month_index, 12)
        month = month_zero + 1
        if day <= calendar.monthrange(year, month)[1]:
            candidates.append(datetime(year, month, day, hour, tzinfo=timezone.utc))
    return min(candidates, key=lambda candidate: abs(candidate - issue_time))


def _raw_taf_temperature(
    raw_taf: str, marker: str, issue_time: datetime
) -> tuple[float | None, datetime | None]:
    match = re.search(rf"\b{marker}(M?\d{{2}})/(\d{{2}})(\d{{2}})Z\b", raw_taf)
    if not match:
        return None, None
    token, day, hour = match.groups()
    value = -float(token[1:]) if token.startswith("M") else float(token)
    return value, _taf_group_datetime(issue_time, int(day), int(hour))


def _decoded_taf_temperatures(
    report: dict, issue_time: datetime
) -> dict[str, tuple[float | None, datetime | None]]:
    decoded: dict[str, tuple[float | None, datetime | None]] = {}
    for period in report.get("fcsts") or []:
        for item in period.get("temp") or []:
            marker = str(item.get("maxOrMin") or "").upper()
            value = item.get("sfcTemp")
            valid_at = _api_datetime(item.get("validTime"))
            if marker in {"MAX", "MIN"} and value is not None and valid_at is not None:
                decoded[marker] = (float(value), valid_at)
    raw_taf = str(report.get("rawTAF") or "")
    decoded.setdefault("MAX", _raw_taf_temperature(raw_taf, "TX", issue_time))
    decoded.setdefault("MIN", _raw_taf_temperature(raw_taf, "TN", issue_time))
    return decoded


def _taf_periods_json(report: dict) -> str:
    periods = []
    for period in report.get("fcsts") or []:
        periods.append(
            {
                "time_from": (
                    _api_datetime(period.get("timeFrom")).isoformat()
                    if _api_datetime(period.get("timeFrom"))
                    else None
                ),
                "time_to": (
                    _api_datetime(period.get("timeTo")).isoformat()
                    if _api_datetime(period.get("timeTo"))
                    else None
                ),
                "time_bec": (
                    _api_datetime(period.get("timeBec")).isoformat()
                    if _api_datetime(period.get("timeBec"))
                    else None
                ),
                "change": period.get("fcstChange"),
                "probability": period.get("probability"),
                "wind_direction": period.get("wdir"),
                "wind_speed_kt": period.get("wspd"),
                "wind_gust_kt": period.get("wgst"),
                "weather": period.get("wxString"),
                "visibility_sm": period.get("visib"),
                "clouds": period.get("clouds") or [],
            }
        )
    return json.dumps(periods, separators=(",", ":"))


def _taf_rows(
    payload: object,
    *,
    fetched_at: datetime,
    backfilled: bool,
) -> list[dict]:
    rows = []
    for report in payload or []:
        issue_time = _api_datetime(report.get("issueTime"))
        valid_from = _api_datetime(report.get("validTimeFrom"))
        valid_to = _api_datetime(report.get("validTimeTo"))
        raw_taf = str(report.get("rawTAF") or "").strip()
        if issue_time is None or valid_from is None or valid_to is None or not raw_taf:
            continue
        temperatures = _decoded_taf_temperatures(report, issue_time)
        maximum, maximum_at = temperatures.get("MAX", (None, None))
        minimum, minimum_at = temperatures.get("MIN", (None, None))
        rows.append(
            {
                "airport": str(report.get("icaoId") or "").upper(),
                "issue_time": issue_time,
                "bulletin_time": _api_datetime(report.get("bulletinTime")),
                "valid_from": valid_from,
                "valid_to": valid_to,
                "raw_taf": raw_taf,
                "is_amended": bool(re.search(r"^TAF\s+AMD\b", raw_taf)),
                "is_corrected": bool(re.search(r"^TAF\s+COR\b", raw_taf)),
                "max_temp_c": maximum,
                "max_temp_at": maximum_at,
                "min_temp_c": minimum,
                "min_temp_at": minimum_at,
                "periods_json": _taf_periods_json(report),
                "collected_at": fetched_at,
                "source": "aviationweather.gov",
                "content_hash": hashlib.sha256(raw_taf.encode("utf-8")).hexdigest(),
                "revision_of_hash": None,
                "first_seen_at": fetched_at,
                "fetched_at": fetched_at,
                "target_local_date": None,
                "backfilled": backfilled,
                "coverage_status": "backfilled" if backfilled else "observed",
            }
        )
    return sorted(rows, key=lambda item: (item["airport"], item["issue_time"]))


def recent_tafs(
    icaos: str | list[str],
    *,
    attempts: int = 5,
    timeout: float | None = None,
) -> list[dict]:
    """Fetch the current decoded TAF revision for every requested airport."""
    identifiers = [icaos] if isinstance(icaos, str) else list(icaos)
    if not identifiers:
        return []
    payload = _get(
        TAF_URL,
        {"ids": ",".join(identifiers), "format": "json"},
        attempts=attempts,
        timeout=timeout,
    )
    return _taf_rows(
        payload,
        fetched_at=datetime.now(timezone.utc),
        backfilled=False,
    )


def historical_tafs_at(
    icaos: str | list[str],
    issued_as_of: datetime,
    *,
    attempts: int = 3,
    timeout: float | None = None,
) -> list[dict]:
    """Recover the TAF revision published as of one historical UTC instant.

    Recovered rows are marked as backfills. Their issue time is usable for the
    official TAF score, but ``first_seen_at`` remains the later retrieval time;
    they therefore cannot leak into reconstructed Weatherman decisions.
    """
    identifiers = [icaos] if isinstance(icaos, str) else list(icaos)
    if not identifiers:
        return []
    requested = issued_as_of.astimezone(timezone.utc)
    payload = _get(
        TAF_URL,
        {
            "ids": ",".join(identifiers),
            "format": "json",
            "time": "issue",
            "date": requested.isoformat().replace("+00:00", "Z"),
        },
        attempts=attempts,
        timeout=timeout,
    )
    return _taf_rows(
        payload,
        fetched_at=datetime.now(timezone.utc),
        backfilled=True,
    )


def polymarket_event_slug(airport: dict, target: date) -> str:
    city = airport.get("market_city")
    if not city:
        return ""
    return (
        f"highest-temperature-in-{city}-on-{MONTH_SLUGS[target.month]}-{target.day}-{target.year}"
    )


_TEMPERATURE_EVENT_SLUG = re.compile(
    r"^highest-temperature-in-(?P<city>.+)-on-"
    r"(?P<month>january|february|march|april|may|june|july|august|"
    r"september|october|november|december)-(?P<day>\d{1,2})-(?P<year>\d{4})$",
    re.IGNORECASE,
)


def _temperature_event_identity(event: dict) -> tuple[str, str, date] | None:
    slug = str(event.get("slug") or "").strip().casefold()
    match = _TEMPERATURE_EVENT_SLUG.match(slug)
    if not match:
        return None
    month = MONTH_SLUGS.index(match.group("month").casefold())
    target = date(int(match.group("year")), month, int(match.group("day")))
    city_slug = match.group("city").casefold()
    display_name = city_slug.replace("-", " ").title()
    title = str(event.get("title") or event.get("question") or "").strip()
    title_match = re.match(r"^Highest temperature in (.+?) on ", title, re.IGNORECASE)
    if title_match:
        display_name = title_match.group(1).strip()
    return city_slug, display_name, target


def discover_polymarket_temperature_events(
    *,
    include_closed: bool = False,
    max_pages: int = 6,
    page_size: int = 200,
) -> list[dict]:
    """Discover temperature-market cities before trying to map them to stations.

    Unknown cities are intentionally returned. The research dashboard can then flag
    them for station verification instead of silently omitting a new Polymarket city.
    """
    discovered: dict[str, dict] = {}
    cursor: str | None = None
    for page in range(max_pages):
        params: dict[str, Any] = {
            "limit": page_size,
            "order": "startDate",
            "ascending": "false",
            "title_search": "Highest temperature in",
        }
        if not include_closed:
            params["closed"] = "false"
        if cursor:
            params["after_cursor"] = cursor
        payload = _get(f"{POLYMARKET_GAMMA_URL}/events/keyset", params)
        if isinstance(payload, dict):
            events = payload.get("events") or []
            next_cursor = payload.get("next_cursor")
        else:
            # Retain compatibility with the legacy list response in tests and
            # during a provider-side rollout.
            events = payload if isinstance(payload, list) else []
            next_cursor = None
        if not events:
            break
        for event in events:
            identity = _temperature_event_identity(event)
            if identity is None:
                continue
            city_slug, display_name, target = identity
            markets = event.get("markets") or []
            labels = " ".join(
                str(market.get("groupItemTitle") or market.get("question") or "")
                for market in markets
            )
            unit = "F" if re.search(r"°?\s*F\b", labels, re.IGNORECASE) else "C"
            candidate = {
                "market_city": city_slug,
                "display_name": display_name,
                "target_date": target,
                "event_slug": str(event.get("slug") or ""),
                "resolution_source": event.get("resolutionSource"),
                "market_unit": unit,
                "active": bool(event.get("active", not event.get("closed", False))),
            }
            prior = discovered.get(city_slug)
            if prior is None or target >= prior["target_date"]:
                discovered[city_slug] = candidate
        cursor = str(next_cursor) if next_cursor else None
        if not cursor:
            break
    return sorted(discovered.values(), key=lambda row: row["market_city"])


def _json_array(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _temperature_range(label: str) -> tuple[float | None, float | None]:
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*°?C", label, re.IGNORECASE)
    if not match:
        return None, None
    temperature = float(match.group(1))
    lowered = label.casefold()
    if "below" in lowered or "lower" in lowered:
        return None, temperature
    if "higher" in lowered or "above" in lowered:
        return temperature, None
    return temperature, temperature


def polymarket_prices(
    airport: dict,
    target: date,
    *,
    attempts: int = 5,
    timeout: float | None = None,
) -> list[dict]:
    event_slug = polymarket_event_slug(airport, target)
    if not event_slug:
        return []
    try:
        event = _get(
            f"{POLYMARKET_GAMMA_URL}/events/slug/{event_slug}",
            attempts=attempts,
            timeout=timeout,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []
        raise
    if not isinstance(event, dict):
        return []
    captured_at = datetime.now(timezone.utc)
    rows = []
    for market in event.get("markets", []):
        outcomes = _json_array(market.get("outcomes"))
        prices = _json_array(market.get("outcomePrices"))
        tokens = _json_array(market.get("clobTokenIds"))
        try:
            yes_index = [str(outcome).casefold() for outcome in outcomes].index("yes")
            yes_price = float(prices[yes_index])
        except (ValueError, IndexError, TypeError):
            continue
        label = str(market.get("groupItemTitle") or market.get("question") or "")
        low, high = _temperature_range(label)
        if low is None and high is None:
            continue
        closed = bool(market.get("closed"))
        yes_won = yes_price >= 0.999 if closed else None
        rows.append(
            {
                "target_date": target,
                "event_slug": event_slug,
                "market_id": str(market["id"]),
                "market_slug": str(market.get("slug") or ""),
                "token_id": str(tokens[yes_index]) if yes_index < len(tokens) else None,
                "bucket_label": label,
                "bucket_low_c": low,
                "bucket_high_c": high,
                "yes_price": yes_price,
                "best_bid": _number(market.get("bestBid")),
                "best_ask": _number(market.get("bestAsk")),
                "spread": _number(market.get("spread")),
                "volume": _number(market.get("volumeNum") or market.get("volume")),
                "liquidity": _number(market.get("liquidityNum") or market.get("liquidity")),
                "closed": closed,
                "yes_won": yes_won,
                "resolution_source": event.get("resolutionSource"),
                "price_kind": "live",
                "captured_at": captured_at,
            }
        )
    return rows


def _book_timestamp(value: Any, fallback: datetime) -> datetime:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return fallback


def polymarket_order_books(token_ids: list[str]) -> dict[str, dict]:
    """Fetch public YES order-book depth for several outcome tokens."""
    requested = list(dict.fromkeys(str(token) for token in token_ids if token))
    if not requested:
        return {}
    payload = [{"token_id": token} for token in requested[:500]]
    fetched_at = datetime.now(timezone.utc)
    response = _post(f"{POLYMARKET_CLOB_URL}/books", payload, timeout=10)
    if not isinstance(response, list):
        return {}
    books: dict[str, dict] = {}
    for book in response:
        if not isinstance(book, dict):
            continue
        token_id = str(book.get("asset_id") or book.get("token_id") or "")
        if not token_id:
            continue
        asks = book.get("asks") if isinstance(book.get("asks"), list) else []
        bids = book.get("bids") if isinstance(book.get("bids"), list) else []
        books[token_id] = {
            "token_id": token_id,
            "observed_at": _book_timestamp(book.get("timestamp"), fetched_at),
            "fetched_at": fetched_at,
            "hash": book.get("hash"),
            "asks": asks,
            "bids": bids,
            "min_order_size": book.get("min_order_size") or book.get("minOrderSize"),
            "tick_size": book.get("tick_size") or book.get("tickSize"),
            "neg_risk": book.get("neg_risk") or book.get("negRisk"),
        }
    return books


def polymarket_historical_prices(
    airport: dict,
    target: date,
    sample_times: list[datetime],
) -> list[dict]:
    """Sample past YES trade prices near predefined decision times.

    Historical CLOB data does not reproduce the old order book or executable ask.
    Rows are therefore explicitly labelled as historical trade-price samples.
    """
    if not sample_times:
        return []
    event_slug = polymarket_event_slug(airport, target)
    if not event_slug:
        return []
    try:
        event = _get(f"{POLYMARKET_GAMMA_URL}/events/slug/{event_slug}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []
        raise
    if not isinstance(event, dict):
        return []

    start_ts = int(min(sample_times).timestamp()) - 4 * 3600
    end_ts = int(max(sample_times).timestamp()) + 4 * 3600
    rows: list[dict] = []
    for market in event.get("markets", []):
        outcomes = _json_array(market.get("outcomes"))
        tokens = _json_array(market.get("clobTokenIds"))
        try:
            yes_index = [str(outcome).casefold() for outcome in outcomes].index("yes")
            token_id = str(tokens[yes_index])
        except (ValueError, IndexError, TypeError):
            continue
        label = str(market.get("groupItemTitle") or market.get("question") or "")
        low, high = _temperature_range(label)
        if low is None and high is None:
            continue
        history = _get(
            f"{POLYMARKET_CLOB_URL}/prices-history",
            {
                "market": token_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": 30,
            },
            attempts=3,
        )
        points = history.get("history", []) if isinstance(history, dict) else []
        usable = [
            (datetime.fromtimestamp(float(point["t"]), tz=timezone.utc), float(point["p"]))
            for point in points
            if point.get("t") is not None and point.get("p") is not None
        ]
        if not usable:
            continue
        closed = bool(market.get("closed"))
        latest_prices = _json_array(market.get("outcomePrices"))
        try:
            final_yes = float(latest_prices[yes_index])
        except (IndexError, TypeError, ValueError):
            final_yes = 0.0
        for sample_at in sample_times:
            observed_at, price = min(
                usable, key=lambda point: abs(point[0] - sample_at)
            )
            if abs((observed_at - sample_at).total_seconds()) > 4 * 3600:
                continue
            rows.append(
                {
                    "target_date": target,
                    "event_slug": event_slug,
                    "market_id": str(market["id"]),
                    "market_slug": str(market.get("slug") or ""),
                    "token_id": token_id,
                    "bucket_label": label,
                    "bucket_low_c": low,
                    "bucket_high_c": high,
                    "yes_price": price,
                    "best_bid": None,
                    "best_ask": None,
                    "spread": None,
                    "volume": _number(market.get("volumeNum") or market.get("volume")),
                    "liquidity": None,
                    "closed": closed,
                    "yes_won": final_yes >= 0.999 if closed else None,
                    "resolution_source": event.get("resolutionSource"),
                    "price_kind": "historical trade-price sample",
                    "captured_at": sample_at,
                }
            )
        time.sleep(0.12)
    return rows
