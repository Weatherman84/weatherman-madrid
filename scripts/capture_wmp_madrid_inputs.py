"""One manual research input capture. No scheduler, inference, DB or credentials."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx

from weatherman.research_wmp.snapshot import ZONE, make_source

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", action="store_true", help="Print fixed public requests without fetching")
    args = parser.parse_args()
    today = datetime.now(ZONE).date()
    plan = request_plan(today)
    if args.plan:
        print(json.dumps(plan, indent=2))
        return
    if args.output.exists():
        raise SystemExit("Refusing to replace an existing captured input file")
    with ThreadPoolExecutor(max_workers=4) as pool:
        sources = dict(pool.map(retrieve, plan.items()))
    as_of = datetime.now(timezone.utc)
    if as_of.astimezone(ZONE).date() != today:
        raise SystemExit("Local date changed during capture; no input bundle saved")
    snapshot = {"schema": "wmp_madrid_inputs_v1", "target_date": today.isoformat(),
                "as_of": as_of.isoformat(), "sources": sources,
                "classification": "manual_research_inputs_only_not_a_forecast"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        stream.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
    print(json.dumps({"status": "captured_inputs_only", "output": str(args.output),
                      "target_date": today.isoformat(), "bytes": args.output.stat().st_size,
                      "prospective_OOS": False, "scheduled_job_created": False}))


if __name__ == "__main__":
    main()
