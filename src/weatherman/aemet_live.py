from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
import pandas as pd


AEMET_STATION_ID = "3129"
AEMET_CLASSIFICATION = "AEMET PHYSICAL OBSERVATIONS — NOT MARKET RESOLUTION"


def filter_metar_day(metars, target, timezone_name="Europe/Madrid"):
    """One shared local-day filter for every AEMET comparison, including DST."""
    day = pd.Timestamp(target).date()
    result = []
    for row in metars or []:
        stamp = pd.to_datetime(row.get("observed_at"), utc=True, errors="coerce")
        if pd.notna(stamp) and stamp.tz_convert(timezone_name).date() == day:
            result.append(row)
    return result


def observation_freshness(observed_at, now=None):
    stamp = pd.to_datetime(observed_at, utc=True, errors="coerce")
    if pd.isna(stamp):
        return "stale", None
    clock = pd.Timestamp(now or datetime.now(timezone.utc))
    age = max(0.0, (clock - stamp).total_seconds() / 60)
    return ("current" if age <= 75 else "delayed" if age <= 120 else "stale"), age


def normalized_public_base_url(value: str | None) -> str | None:
    """Accept only a credential-free HTTPS Worker origin."""
    candidate = str(value or "").strip().rstrip("/")
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    return candidate


def archive_path(target: date) -> str:
    return f"archive/aemet/{target:%Y/%m/%d}.json.gz"


def fetch_public_aemet_json(
    base_url: str,
    path: str,
    *,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    base = normalized_public_base_url(base_url)
    if base is None:
        raise ValueError("AEMET_PUBLIC_BASE_URL must be a credential-free HTTPS origin")
    safe_path = str(path).strip().lstrip("/")
    with httpx.Client(
        timeout=max(1.0, float(timeout_seconds)),
        follow_redirects=True,
        headers={"User-Agent": "Weatherman-Madrid/1.0.11 AEMET public reader"},
    ) as client:
        response = client.get(f"{base}/{safe_path}")
        response.raise_for_status()
        body = response.content
        # Older Worker archives can contain gzip bytes without declaring
        # Content-Encoding. httpx handles correctly declared gzip responses;
        # recognise the file signature as a compatibility fallback.
        if body.startswith(b"\x1f\x8b"):
            body = gzip.decompress(body)
        payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("AEMET public endpoint did not return a JSON object")
    station = payload.get("station") or {}
    if str(station.get("id") or "") != AEMET_STATION_ID:
        raise ValueError("AEMET public endpoint returned an unexpected station")
    if payload.get("classification") != AEMET_CLASSIFICATION:
        raise ValueError("AEMET public endpoint returned an unexpected classification")
    return payload


def curve_rows(
    payload: dict[str, Any],
    metars: list[dict[str, Any]] | None,
    *,
    timezone_name: str = "Europe/Madrid",
) -> list[dict[str, Any]]:
    """Build a compact Vega-ready AEMET/METAR comparison without changing Actuals."""

    def local_timestamp(value: Any) -> str | None:
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(parsed):
            return None
        return pd.Timestamp(parsed).tz_convert(timezone_name).strftime("%Y-%m-%dT%H:%M:%S")

    rows: list[dict[str, Any]] = []
    if payload.get("local_date"):
        metars = filter_metar_day(metars, payload["local_date"], timezone_name)
    for item in payload.get("observations") or []:
        timestamp = local_timestamp(item.get("observed_at"))
        temperature = pd.to_numeric(item.get("temperature_c"), errors="coerce")
        if timestamp is not None and pd.notna(temperature):
            rows.append(
                {
                    "timestamp": timestamp,
                    "temperature_c": float(temperature),
                    "series": "AEMET 3129 (physical)",
                }
            )
    for item in metars or []:
        timestamp = local_timestamp(item.get("observed_at"))
        temperature = pd.to_numeric(item.get("temp_c"), errors="coerce")
        if timestamp is not None and pd.notna(temperature):
            rows.append(
                {
                    "timestamp": timestamp,
                    "temperature_c": float(temperature),
                    "series": "LEMD METAR (integer)",
                }
            )
    maximum = payload.get("physical_tmax") or {}
    maximum_at = (
        maximum.get("peak_at") if maximum.get("peak_time_verified")
        else maximum.get("report_at") or maximum.get("observed_at")
    )
    timestamp = local_timestamp(maximum_at)
    temperature = pd.to_numeric(maximum.get("value_c"), errors="coerce")
    if timestamp is not None and pd.notna(temperature):
        rows.append(
            {
                "timestamp": timestamp,
                "temperature_c": float(temperature),
                "series": "AEMET physical Tmax",
            }
        )
    return rows
