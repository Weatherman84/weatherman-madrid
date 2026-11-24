"""Point-in-time live adapter v1. Sources are captured before the 16:00 cutoff."""
from datetime import timedelta
from math import cos, isfinite, pi, sin
from statistics import mean, pstdev

from .madrid_late_shadow_model import MODELS, CHECKPOINT, ZONE, bounds, cutoff, instant

# Preserve the historical feature formulas; receipt is now proved by the live snapshot.
day_bounds = bounds


def capture_due(now):
    """Never reconstruct a missed cutoff. First valid capture in this window wins."""
    cut = cutoff(instant(now).astimezone(ZONE).date())
    return cut - timedelta(minutes=10) <= instant(now) < cut


def interpolate(points, at):
    at = instant(at)
    for t, y in points:
        if t == at:
            return y
    for (a, x), (b, y) in zip(points, points[1:]):
        if a < at < b and (b - a).total_seconds() == 3600:
            return x + (y - x) * (at - a).total_seconds() / 3600
    raise ValueError("No adjacent forecast-hour bracket")


def build_input(day, captured_at, observations, forecasts, hourly, bulletins, drivers=None):
    cut, captured = cutoff(day), instant(captured_at)
    start, end = bounds(day)
    if not cut - timedelta(minutes=10) <= captured < cut:
        raise ValueError("Outside prospective capture window")
    usable = []
    seen = {}
    for o in observations:
        at = instant(o["observed_at"])
        if start <= at <= captured and at + timedelta(minutes=5) <= cut:
            if not isfinite(o["temp_c"]) or int(o["temp_c"]) != o["temp_c"]:
                raise ValueError("Integer METAR observation required")
            if at in seen and seen[at] != o:
                raise ValueError("Conflicting same-time observations")
            seen[at] = o
    for o in seen.values():
        usable.append({**o, "available_at": max(captured, instant(o["observed_at"]) + timedelta(minutes=5)).isoformat()})
    f = observation_features(usable, cut.isoformat(), str(day))
    if not f or not 5 <= f["observation_age"] <= 45:
        raise ValueError("No fresh pre-cutoff METAR")
    chosen = {}
    for model in MODELS:
        options = [r for r in forecasts if r["model"] == model]
        if not options:
            raise ValueError("Missing common forecast: " + model)
        r = max(options, key=lambda r: (instant(r["fetched_at"]), r["id"]))
        fetched, run = instant(r["fetched_at"]), instant(r["run_at"])
        if (not run <= fetched <= captured or captured - fetched > timedelta(hours=6)
                or captured - run > timedelta(hours=48)
                or not isfinite(r["max_temp_c"])):
            raise ValueError("Invalid/stale latest forecast: " + model)
        if r.get("available_at") and not run <= instant(r["available_at"]) <= fetched:
            raise ValueError("Invalid forecast availability: " + model)
        chosen[model] = r
    values = {m: r["max_temp_c"] for m, r in chosen.items()}
    drivers = drivers or {}
    if drivers and not start <= instant(drivers["captured_at"]) <= captured:
        raise ValueError("Driver snapshot outside captured information set")
    # These three unchanged reference features retain their stored Champion meaning.
    old = drivers.get("features", {})
    theta = 2 * pi * cut.astimezone(ZONE).timetuple().tm_yday / 365.25
    f.update(remaining_model_rise=old.get("remaining_model_rise_c"),
             model_radiation=old.get("model_radiation_wm2"),
             future_radiation=old.get("future_radiation_max_wm2"),
             prior_minus_observed=mean(values.values()) - f["observed_max"],
             model_spread=pstdev(values.values()), season_sin=sin(theta), season_cos=cos(theta),
             local_hour=16, model_count=6, curve_remaining_mean_c=None)
    eligible = [t for t in bulletins if
                instant(t["issue_time"]) <= instant(t["available_at"]) <= captured
                and instant(t["valid_from"]) < end and instant(t["valid_to"]) > start]
    taf = {"status": "no_eligible_bulletin"}
    if eligible:
        t = max(eligible, key=lambda t: (instant(t["issue_time"]), instant(t["available_at"]), t["id"]))
        tx = t.get("max_temp_c")
        present = tx is not None and t.get("max_temp_at") is not None and start <= instant(t["max_temp_at"]) < end
        taf = {k: t[k] for k in ("issue_time", "available_at", "content_hash")}
        taf.update(status="target_TX_available" if present else "latest_bulletin_has_no_target_TX",
                   max_temp_c=tx if present else None, max_temp_at=t.get("max_temp_at") if present else None)
    for key in ("taf_tx_minus_known_metar_max_c", "taf_tx_minus_equal_model_mean_c", "taf_issue_age_hours"):
        f[key] = None
    if taf["status"] == "target_TX_available":
        f.update(taf_tx_minus_known_metar_max_c=taf["max_temp_c"] - f["observed_max"],
                 taf_tx_minus_equal_model_mean_c=taf["max_temp_c"] - mean(values.values()),
                 taf_issue_age_hours=(cut - instant(taf["issue_time"])).total_seconds() / 3600)
    curves, reasons = {}, {}
    for model, r in chosen.items():
        points = sorted((instant(p["valid_at"]), p["temp_c"]) for p in hourly
                        if p["model"] == model and instant(p["run_at"]) == instant(r["run_at"])
                        and start <= instant(p["valid_at"]) < end)
        expected = [start + timedelta(hours=i) for i in range(int((end - start).total_seconds() / 3600))]
        if [t for t, _ in points] != expected or any(not isfinite(v) for _, v in points):
            reasons[model] = "latest_matched_run_has_incomplete_hourly_day"
            continue
        if abs(max(v for _, v in points) - r["max_temp_c"]) > 1e-8:
            reasons[model] = "daily_hourly_maximum_mismatch"
            continue
        future = max(v for t, v in points if t >= cut)
        anchor = f["observed_max"] - f["gap_to_max"] + future - interpolate(points, f["latest_observed_at"])
        curves[model] = {"remaining_rise_c": max(0, anchor - f["observed_max"]),
                         "available_at": captured.isoformat(), "version": r["run_at"],
                         "initialized_at": r.get("model_run_at")}
    available = set(curves) == set(MODELS)
    if available:
        f["curve_remaining_mean_c"] = mean(c["remaining_rise_c"] for c in curves.values())
    return {"airport": "LEMD", "target_date": str(day), "checkpoint": CHECKPOINT,
            "checkpoint_at": cut.isoformat(), "observed_max": f["observed_max"], "models": values,
            "features": f, "taf_present": taf["status"] == "target_TX_available", "taf_audit": taf,
            "curve_available": available,
            "curve_audit": {"status": "available" if available else "missing_comparable_long_path",
                            "observation_at": f["latest_observed_at"], "models": curves,
                            "missing_reasons": reasons, "source": "live_point_in_time_composite_v1"},
            "input_source_class": "live_point_in_time_composite_v1",
            "source_snapshot": {"observations": observations,
                                "forecasts": list(chosen.values()),
                                "hourly": [p for p in hourly if p["model"] in chosen and
                                           instant(p["run_at"]) == instant(chosen[p["model"]]["run_at"])],
                                "bulletins": bulletins, "drivers": drivers},
            "driver_snapshot_at": drivers.get("captured_at"),
            "model_provenance": {m: {k: r.get(k) for k in
                                    ("id", "run_at", "model_run_at", "available_at", "fetched_at", "provenance_status")}
                                 for m, r in chosen.items()}}


def observation_features(observations: list[dict], cutoff: str, day: str) -> dict:
    """Same frozen METAR feature formulas, on a pre-cutoff database snapshot."""
    start, end = day_bounds(day)
    cut = instant(cutoff)
    usable = sorted((o for o in observations if o.get("temp_c") is not None and
                     start <= instant(o["observed_at"]) <= cut < end and
                     instant(o.get("available_at") or o["observed_at"]) <= cut),
                    key=lambda o: instant(o["observed_at"]))
    if not usable:
        return {}
    last = usable[-1]
    at = instant(last["observed_at"])
    maximum = max(o["temp_c"] for o in usable)
    def previous(minutes):
        candidates = [o for o in usable if instant(o["observed_at"]) <= at - timedelta(minutes=minutes)]
        if not candidates:
            return None
        p = candidates[-1]
        if (at - instant(p["observed_at"])).total_seconds() > (minutes + 20) * 60:
            return None
        return p
    def rate(minutes):
        p = previous(minutes)
        return None if p is None else (last["temp_c"] - p["temp_c"]) / (
            (at - instant(p["observed_at"])).total_seconds() / 3600)
    plateau = at
    for o in reversed(usable):
        if o["temp_c"] != last["temp_c"] or (plateau - instant(o["observed_at"])).total_seconds() > 45 * 60:
            break
        plateau = instant(o["observed_at"])
    wind = last.get("wind_direction")
    past = previous(120)
    def difference(field):
        a, b = last.get(field), past.get(field) if past else None
        return None if a is None or b is None else a - b
    return {"observed_max": int(maximum), "gap_to_max": maximum - last["temp_c"],
            "plateau_minutes": (at - plateau).total_seconds() / 60,
            "heating30": rate(30), "heating60": rate(60), "heating120": rate(120),
            "observation_age": (cut - at).total_seconds() / 60,
            "wind_speed": last.get("wind_kph"),
            "wind_sin": None if wind is None else sin(wind * pi / 180),
            "wind_cos": None if wind is None else cos(wind * pi / 180),
            "wind_change": difference("wind_kph"), "dewpoint_change": difference("dewpoint_c"),
            "cloud_cover": last.get("cloud_cover"), "latest_observed_at": at.isoformat()}

