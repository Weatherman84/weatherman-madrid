"""Bounded public WMP input capture; no scheduler, inference or database."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json

import httpx

from .snapshot import ZONE, make_source

GRID = {"latitude": 40.466, "longitude": -3.555, "timezone": "Europe/Madrid", "temperature_unit": "celsius"}


def request_plan(today):
    return {
        "history": ("https://archive-api.open-meteo.com/v1/archive", {
            **GRID, "start_date": (today - timedelta(days=int(20 * 365.25))).isoformat(),
            "end_date": (today - timedelta(days=5)).isoformat(),
            "daily": "temperature_2m_max,temperature_2m_min"}),
        "recent": ("https://api.open-meteo.com/v1/forecast", {
            **GRID, "daily": "temperature_2m_max", "past_days": 35, "forecast_days": 1}),
        "ensemble": ("https://ensemble-api.open-meteo.com/v1/ensemble", {
            **GRID, "daily": "temperature_2m_max", "models": "gfs025,ecmwf_ifs025,icon_seamless,gem_global",
            "forecast_days": 3}),
        "metar": ("https://aviationweather.gov/api/data/metar", {
            "ids": "LEMD", "format": "json", "hours": 48}),
    }


def retrieve(item):
    kind, (url, params) = item
    chunks, size = [], 0
    with httpx.stream("GET", url, params=params, timeout=25) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 5_000_000:
                raise ValueError("Oversized research input")
            chunks.append(chunk)
    at = datetime.now(timezone.utc)
    payload = json.loads(b"".join(chunks))
    request = {**params, "endpoint": url}
    if kind == "metar":
        normalized = []
        for r in payload:
            if r.get("icaoId") != "LEMD":
                raise ValueError("Unexpected METAR station")
            if r.get("temp") is None:
                continue
            provider = datetime.fromisoformat(r["receiptTime"].replace("Z", "+00:00"))
            normalized.append({"station": "LEMD", "temp_c": r["temp"],
                               "observed_at": datetime.fromtimestamp(r["obsTime"], timezone.utc).isoformat(),
                               "received_at": max(provider, at).isoformat(),
                               "provider_received_at": provider.isoformat()})
        payload = normalized
        request["station"] = "LEMD"
    return kind, make_source(payload, at, kind=kind, request=request)



def capture_snapshot(*, allow_partial=False):
    started = datetime.now(timezone.utc)
    today = started.astimezone(ZONE).date()
    sources, errors = {}, {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(retrieve, item): item[0] for item in request_plan(today).items()}
        for future in as_completed(pending):
            kind = pending[future]
            try:
                name, source = future.result()
                sources[name] = source
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                errors[kind] = type(exc).__name__
    finished = datetime.now(timezone.utc)
    if finished.astimezone(ZONE).date() != today:
        raise ValueError("Local date changed during source capture")
    if errors and not allow_partial:
        raise RuntimeError("Incomplete public source capture: " + ", ".join(sorted(errors)))
    return {"schema": "wmp_madrid_inputs_v1", "target_date": today.isoformat(),
            "capture_started_at": started.isoformat(), "as_of": finished.isoformat(),
            "sources": sources, "capture_errors": errors,
            "classification": "research_inputs_only_not_a_forecast"}
