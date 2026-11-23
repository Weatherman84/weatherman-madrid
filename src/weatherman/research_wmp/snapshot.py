"""Offline, separately named WMP/Madrid adaptation. No app, DB or trading imports."""

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from math import isfinite
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import numpy as np

from .upstream.blend import build_distribution
from .upstream.models.bias_correct import regime_offset
from .upstream.models.climatology import fit_climatology
from .upstream.models.ensemble import sample

ZONE = ZoneInfo("Europe/Madrid")
CANDIDATE = "wmp_madrid_metar_floor_v1"
COMMIT = "cc01f6f228250138a9300e20d3cdc791675bbb96"
FAMILIES = {"ncep_gefs025": 31, "ecmwf_ifs025_ensemble": 51,
            "icon_seamless_eps": 40, "gem_global_ensemble": 21}


def instant(value):
    at = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if at.tzinfo is None:
        raise ValueError("Timezone required")
    return at.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def checkpoint(day):
    return datetime.combine(date.fromisoformat(day), time(16), ZONE).astimezone(timezone.utc)


def verify_upstream():
    root = Path(__file__).parent
    manifest = json.loads((root / "UPSTREAM.json").read_text())
    if manifest["commit"] != COMMIT:
        raise ValueError("Unsupported upstream version")
    for path, expected in manifest["files_sha256"].items():
        if hashlib.sha256((root / "upstream" / path).read_bytes()).hexdigest() != expected:
            raise ValueError("Modified upstream file: " + path)
    return manifest


def validate_protocol(p):
    fixed = {"candidate_id": CANDIDATE, "upstream_commit": COMMIT, "airport": "LEMD",
             "checkpoint": "Late Live @16:00", "timezone": "Europe/Madrid",
             "latitude": 40.466, "longitude": -3.555, "history_years": 20,
             "station_offset": 0.0, "spread_inflation": 1.0, "seed": 7,
             "ensemble_samples": 20000, "final_samples": 60000, "kernel_sd_c": 0.6,
             "family_members": FAMILIES, "research_only": True, "automatic_promotion": False,
             "automatic_execution": False, "live_activation": False,
             "historical_full_ensemble_validation_passed": False}
    if any(p.get(k) != v for k, v in fixed.items()):
        raise ValueError("Protocol differs from the prespecified diagnostic candidate")


def make_source(data, received_at, *, kind, request):
    return {"kind": kind, "received_at": instant(received_at).isoformat(),
            "sha256": digest(data), "request": request, "data": data}


def validate_source(source, kind, as_of):
    if source["kind"] != kind or source["sha256"] != digest(source["data"]):
        raise ValueError("Source kind/digest mismatch")
    if instant(source["received_at"]) > as_of:
        raise ValueError("Source received after declared information cutoff")
    request = source["request"]
    if kind != "metar" and any(request.get(k) != v for k, v in
                               {"latitude": 40.466, "longitude": -3.555,
                                "timezone": "Europe/Madrid", "temperature_unit": "celsius"}.items()):
        raise ValueError("Wrong grid location, timezone or unit")
    if kind == "metar" and request.get("station") != "LEMD":
        raise ValueError("Wrong METAR station")
    return source["data"]


def member_counts(ensemble, target):
    daily = ensemble["daily"]
    times = daily["time"]
    if len(set(times)) != len(times) or target not in times:
        raise ValueError("Missing/duplicate ensemble target date")
    index = times.index(target)
    ids = {f: set() for f in FAMILIES}
    for key, values in daily.items():
        if key == "time":
            continue
        match = re.fullmatch(r"temperature_2m_max(?:_member(\d+))?_(.+)", key)
        if not match or match[2] not in FAMILIES:
            raise ValueError("Unexpected member/product column")
        member, family = int(match[1] or 0), match[2]
        if (member in ids[family] or len(values) != len(times) or values[index] is None
                or not isfinite(values[index]) or ensemble["daily_units"].get(key) != "°C"):
            raise ValueError("Incomplete, duplicate, nonfinite or wrong-unit member")
        ids[family].add(member)
    if any(ids[f] != set(range(n)) for f, n in FAMILIES.items()):
        raise ValueError("All four complete EPS families required; no deterministic substitution")
    return {f: len(ids[f]) for f in ids}


def metar_floor(data, as_of):
    day = as_of.astimezone(ZONE).date()
    versions = {}
    for r in data:
        if r.get("station") != "LEMD":
            raise ValueError("Mixed METAR station")
        at, receipt = instant(r["observed_at"]), instant(r["received_at"])
        if not at <= receipt:
            raise ValueError("METAR receipt precedes observation")
        if at.astimezone(ZONE).date() != day or receipt > as_of or at + timedelta(minutes=5) > as_of:
            continue
        value = r["temp_c"]
        if not isfinite(value) or int(value) != value:
            raise ValueError("Integer measured METAR required")
        version = instant(r.get("provider_received_at") or r["received_at"])
        if at in versions and version == versions[at][0] and int(value) != versions[at][1]:
            raise ValueError("Conflicting same-time METAR revisions")
        if at not in versions or version > versions[at][0]:
            versions[at] = (version, int(value))
    usable = [(at, value) for at, (_, value) in versions.items()]
    if not usable or (as_of - max(t for t, _ in usable)).total_seconds() > 45 * 60:
        raise ValueError("Missing/stale measured METAR floor")
    return max(v for _, v in usable)


def buckets(samples):
    labels, counts = np.unique(np.floor(samples + 0.5).astype(int), return_counts=True)
    return {int(k): float(n / samples.size) for k, n in zip(labels, counts)}


def run_snapshot(snapshot, protocol):
    """Replay only. Historical skill/production activation cannot be inferred here."""
    validate_protocol(protocol)
    verify_upstream()
    if snapshot["schema"] != "wmp_madrid_inputs_v1":
        raise ValueError("Unsupported input schema")
    at = instant(snapshot["as_of"])
    today = at.astimezone(ZONE).date()
    if snapshot["target_date"] != today.isoformat():
        raise ValueError("Madrid same-day diagnostic only")
    payloads = {k: validate_source(snapshot["sources"][k], k, at)
                for k in ("history", "recent", "ensemble", "metar")}
    history, recent, ensemble = (payloads[k] for k in ("history", "recent", "ensemble"))
    times = history["daily"]["time"]
    if (len(times) < 7000 or len(set(times)) != len(times)
            or max(date.fromisoformat(d) for d in times) > today - timedelta(days=5)
            or any(date.fromisoformat(d) < today - timedelta(days=int(20 * 365.25)) for d in times)):
        raise ValueError("Historical window differs from the 20-year upstream request")
    if any(date.fromisoformat(d) > today or date.fromisoformat(d) < today - timedelta(days=35)
           for d in recent["daily"]["time"]):
        raise ValueError("Recent modeled-regime data outside declared date window")
    for p in (history, recent):
        if p["daily_units"].get("temperature_2m_max") != "°C":
            raise ValueError("Wrong historical/recent temperature unit")
        if len(p["daily"]["time"]) != len(p["daily"]["temperature_2m_max"]):
            raise ValueError("Daily time/value length mismatch")
    counts = member_counts(ensemble, today.isoformat())
    floor = metar_floor(payloads["metar"], at)
    rng = np.random.default_rng(protocol["seed"])
    clim = fit_climatology(history)
    regime = regime_offset(recent, clim)
    eps = sample(ensemble, today, n=20000, rng=rng, kernel_sd=.6)
    samples, weight = build_distribution(clim, today, 0, eps, regime, 0.0, rng,
                                         n=60000, spread_inflation=1.0)
    raw = buckets(samples)
    adjusted = buckets(np.maximum(samples, floor))
    cut = checkpoint(today.isoformat())
    return {"candidate_id": CANDIDATE, "airport": "LEMD", "target_date": today.isoformat(),
            "checkpoint": "Late Live @16:00", "checkpoint_at": cut.isoformat(),
            "input_as_of": at.isoformat(), "status": "offline_diagnostic_only",
            "all_sources_received_by_checkpoint": all(instant(s["received_at"]) <= cut for s in snapshot["sources"].values()),
            "prospective_OOS": False, "research_only": True, "automatic_execution": False,
            "automatic_promotion": False, "live_activation": False,
            "input_sha256": digest(snapshot), "protocol_sha256": digest(protocol),
            "upstream_commit": COMMIT, "members": counts, "ensemble_weight": weight,
            "regime_offset": regime, "observed_metar_floor_c": floor,
            "upstream_grid_math_without_floor": raw, "candidate": adjusted,
            "climatology_coefficients": clim.coeffs.tolist(), "climatology_ref_year": clim.ref_year,
            "climatology_residual_sha256": digest(clim.resid.tolist()),
            "source_sha256": {k: s["sha256"] for k, s in snapshot["sources"].items()},
            "differences_from_app": ["measured LEMD METAR floor replaces modeled grid floor",
                                     "full settlement-compatible integer buckets replace default grid bins",
                                     "no GEV context, LLM commentary, prices, Kelly or trading signals"]}
