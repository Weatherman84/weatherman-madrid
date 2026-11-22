"""Offline adapters for immutable exports. No production database imports."""
from collections import Counter
from datetime import timedelta
import gzip
import hashlib
import json
from math import cos, pi, sin
from pathlib import Path
from statistics import mean, pstdev

from .contracts import day_bounds, distribution, instant


FEATURES = (
    "observed_max", "gap_to_max", "plateau_minutes", "heating30", "heating60",
    "heating120", "observation_age", "wind_speed", "wind_sin", "wind_cos",
    "wind_change", "dewpoint_change", "cloud_cover", "remaining_model_rise",
    "model_radiation", "future_radiation", "prior_minus_observed", "model_spread",
    "season_sin", "season_cos", "local_hour", "model_age", "model_count",
)
MODELS = ("ecmwf_ifs025", "gfs_global", "icon_global", "icon_eu",
          "meteofrance_arpege_europe", "ukmo_global_deterministic_10km")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def observation_features(observations: list[dict], cutoff: str, day: str) -> dict:
    """Historical reception estimates are preserved by the enclosing evidence class."""
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


def enrich(row: dict, obs: list[dict], drivers: dict) -> dict:
    f = observation_features(obs, row["checkpoint_at"], row["target_date"])
    # A stored checkpoint maximum is authoritative for its original Champion cohort.
    # The v11 input maximum uses only separately cutoff-filtered observations.
    row["features"] = f
    row["observed_max"] = f.get("observed_max")
    row["observation_provenance"] = "archive_estimated_reception" if row["cohort"] == "deep" else "observation_time_only_reception_unverified"
    vals = [row["models"][m] for m in MODELS if m in row["models"]]
    from zoneinfo import ZoneInfo
    local = instant(row["checkpoint_at"]).astimezone(ZoneInfo("Europe/Madrid"))
    theta = 2 * pi * local.timetuple().tm_yday / 365.25
    f.update({"remaining_model_rise": drivers.get("remaining_model_rise_c"),
              "model_radiation": drivers.get("model_radiation_wm2"),
              "future_radiation": drivers.get("future_radiation_max_wm2"),
              "prior_minus_observed": mean(vals) - row["observed_max"] if vals and row["observed_max"] is not None else None,
              "model_spread": pstdev(vals) if vals else None,
              "season_sin": sin(theta), "season_cos": cos(theta), "local_hour": local.hour,
              "model_age": mean([row["model_ages"][m] for m in MODELS if m in row["model_ages"]]) if any(m in row["model_ages"] for m in MODELS) else None,
              "model_count": len(vals)})
    row["regime"] = ("cooling" if (f.get("heating60") or 0) < 0 else
                     "plateau" if (f.get("plateau_minutes") or 0) >= 90 else "other")
    row["season"] = ("winter", "spring", "summer", "autumn")[(int(row["target_date"][5:7]) % 12) // 3]
    return row


def normalize(deep: Path, daily: Path) -> tuple[list[dict], dict]:
    import pandas as pd
    metars = pd.read_parquet(deep / "normalized/metars.parquet")
    for col in ("observed_at", "available_at"):
        metars[col] = pd.to_datetime(metars[col], utc=True).map(lambda x: x.isoformat())
    obs_by_day = {}
    for o in metars.to_dict("records"):
        from zoneinfo import ZoneInfo
        day = instant(o["observed_at"]).astimezone(ZoneInfo("Europe/Madrid")).date().isoformat()
        obs_by_day.setdefault(day, []).append({k: None if pd.isna(v) else v for k, v in o.items()})
    rows = []
    sources = [{"path": "deep/normalized/metars.parquet", "sha256": sha(deep / "normalized/metars.parquet")}]
    p = deep / "derived/checkpoints.json"
    deep_sha = sha(p)
    sources.append({"path": "deep/derived/checkpoints.json", "sha256": deep_sha})
    for x in json.loads(p.read_text()):
        if not x.get("actual_complete") or x.get("actual_bucket") is None or not x.get("champion_probabilities"):
            continue
        models = x.get("model_history", {}).get("models", [])
        row = {"airport": x["airport"], "target_date": x["target_date"],
               "checkpoint": x["checkpoint_label"], "checkpoint_at": x["checkpoint_at"],
               "actual": x["actual_bucket"], "actual_available_at": x["actual_available_at"],
               "evidence_class": "historical_reduced_fidelity", "cohort": "deep",
               "models": {m["model"]: m["forecast_max_c"] for m in models},
               "model_ages": {m["model"]: m["age_at_checkpoint_minutes"] for m in models if m.get("age_at_checkpoint_minutes") is not None},
               "model_weights": x.get("model_weights", {}),
               "champion": distribution(x["champion_probabilities"]),
               "champion_center": x["champion_center_c"],
               "champion_exact": False, "missing_fields": x.get("missing_fields", []),
               "variants": {}, "source_sha256": deep_sha}
        rows.append(enrich(row, obs_by_day.get(row["target_date"], []), x["features_json"]))
    exports = []
    for p in sorted(daily.glob("*.json")):
        d = json.loads(p.read_text())
        if "checkpoints" in d:
            exports.append((p, d))
            sources.append({"path": "daily/" + p.name, "sha256": sha(p)})
    actuals, observations, checkpoints, raw_models = {}, {}, {}, {}
    for p, d in sorted(exports, key=lambda pair: pair[1].get("generated_at", "")):
        for a in d.get("actuals", []):
            if a.get("is_final_station_actual") and a.get("source") == "stored-metar-station":
                v = a["stored_metar_max_c"]
                if a["target_date"] in actuals and actuals[a["target_date"]] != v:
                    raise ValueError("Conflicting final stored METAR actuals")
                actuals[a["target_date"]] = v
        for o in d.get("observations", []):
            observations[o["observed_at"]] = o
        for m in d.get("model_forecasts", []):
            raw_models[m["target_date"], m["model"], m["run_at"], m["fetched_at"]] = m
        for c in d["checkpoints"]:
            checkpoints[c["target_date"], c["checkpoint"]] = (c, sha(p))
    obs = list(observations.values())
    for (day, cp), (x, source_sha) in sorted(checkpoints.items()):
        if not "2026-10-11" <= day <= "2026-11-20" or day not in actuals:
            continue
        variants = {v["variant"]: v for v in x["champion_and_challengers"] if v.get("bucket_probabilities")}
        if "Champion" not in variants:
            continue
        cutoff = instant(x["checkpoint_at"])
        # Only already received model rows enter independent v11 priors. Provider
        # publication metadata alone does not prove historical local reception.
        models = {}
        for m in raw_models.values():
            if m["target_date"] != day or not m.get("available_at"):
                continue
            if (instant(m["run_at"]) <= instant(m["available_at"]) <= instant(m["fetched_at"]) <= cutoff and
                    (cutoff - instant(m["run_at"])).total_seconds() <= 48 * 3600):
                previous = models.get(m["model"])
                if previous is None or instant(previous["run_at"]) < instant(m["run_at"]):
                    models[m["model"]] = m
        reconstructed = x.get("checkpoint_reconstructed") or "reconstruct" in x.get("checkpoint_status", "")
        timely = (x.get("checkpoint_status") == "scheduled-causal" and x.get("recorded_at") and
                  0 <= (instant(x["recorded_at"]) - cutoff).total_seconds() <= 15 * 60)
        evidence = "reconstructed" if reconstructed else "scheduled_causal_production" if timely else "unknown_provenance"
        _, end = day_bounds(day)
        row = {"airport": "LEMD", "target_date": day, "checkpoint": cp,
               "checkpoint_at": x["checkpoint_at"], "actual": int(actuals[day]),
               "actual_available_at": (end + timedelta(minutes=10)).isoformat(),
               "actual_availability_basis": "estimated_local_day_end_plus_10m",
               "cohort": "daily", "evidence_class": evidence,
               "models": {k: m["max_temp_c"] for k, m in models.items()},
               "model_ages": {k: (cutoff - instant(m["available_at"])).total_seconds() / 60 for k, m in models.items()},
               "model_weights": {}, "champion": distribution(variants["Champion"]["bucket_probabilities"]),
               "champion_center": variants["Champion"]["forecast_c"], "champion_exact": True,
               "variants": {k: v["bucket_probabilities"] for k, v in variants.items() if k != "Champion"},
               "stored_observed_max": x.get("observed_max_c"), "source_sha256": source_sha,
               "recorded_at": x.get("recorded_at"), "missing_fields": ["verified_observation_reception"]}
        rows.append(enrich(row, obs, x["forecast_drivers"]))
    rows.sort(key=lambda r: (r["target_date"], r["checkpoint_at"], r["cohort"]))
    inventory = {"sources": sources, "rows": len(rows),
                 "classes": dict(Counter(r["evidence_class"] for r in rows)),
                 "models": {m: {"rows": sum(m in r["models"] for r in rows),
                                  "first": min(r["target_date"] for r in rows if m in r["models"]),
                                  "last": max(r["target_date"] for r in rows if m in r["models"])}
                            for m in sorted({m for r in rows for m in r["models"]})}}
    return rows, inventory


def write_rows(path: Path, rows: list[dict]) -> None:
    content = "".join(json.dumps(r, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n" for r in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(content.encode(), mtime=0))


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in gzip.decompress(path.read_bytes()).decode().splitlines()]
