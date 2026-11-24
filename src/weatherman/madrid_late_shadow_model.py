"""Inference from frozen research numbers. No fitting, providers, DB or trading."""

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from math import erf, exp, isfinite, log, sqrt
from pathlib import Path
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

VERSION = "madrid_late_single_curve_feature_v1"
CHECKPOINT = "Late Live @16:00"
MODELS = ("ecmwf_ifs025", "gfs_global", "icon_global", "icon_eu",
          "meteofrance_arpege_europe", "ukmo_global_deterministic_10km")
PARAMETERS = Path(__file__).with_name("data") / "madrid_late_shadow_parameters_v1.json"
PARAMETER_SHA = "2a99c69a586032b829829eb7c53b9c5a21a6e161b66ca4eb68f5b2e8e0038ced"
ZONE = ZoneInfo("Europe/Madrid")


def instant(value):
    at = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if at.tzinfo is None:
        raise ValueError("Timezone required")
    return at.astimezone(timezone.utc)


def cutoff(day):
    return datetime.combine(date.fromisoformat(str(day)), time(16), ZONE).astimezone(timezone.utc)


def bounds(day):
    d = date.fromisoformat(str(day))
    return (datetime.combine(d, time(), ZONE).astimezone(timezone.utc),
            datetime.combine(d + timedelta(days=1), time(), ZONE).astimezone(timezone.utc))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def load_parameters(path=PARAMETERS):
    body = Path(path).read_bytes()
    if hashlib.sha256(body).hexdigest() != PARAMETER_SHA:
        raise ValueError("Frozen parameter digest mismatch")
    return json.loads(body)


def distribution(values):
    p = {int(k): float(v) for k, v in values.items()}
    if (not p or len(p) != len(values)
            or any(float(k) != int(k) for k in values)
            or any(not isfinite(v) or v < 0 for v in p.values())
            or abs(sum(p.values()) - 1) > 1e-5):
        raise ValueError("Invalid full probability vector")
    total = sum(p.values())
    return {k: v / total for k, v in sorted(p.items())}


def classifier(model, features):
    raw = [features.get(k) for k in model["features"]]
    missing = [v is None for v in raw]
    if any(v is not None and not isfinite(v) for v in raw):
        raise ValueError("Nonfinite predictor")
    values = [model["imputation"][i] if v is None else v for i, v in enumerate(raw)]
    values += [float(missing[i]) for i in model["missingness_columns"]]
    scaled = [(v - m) / s for v, m, s in
              zip(values, model["scaler_mean"], model["scaler_scale"], strict=True)]
    logits = [bias + sum(a * b for a, b in zip(coef, scaled, strict=True))
              for coef, bias in zip(model["coefficients"], model["intercept"], strict=True)]
    masses = [exp(v - max(logits)) for v in logits]
    return {int(k): p / sum(masses) for k, p in zip(model["classes"], masses, strict=True)}


def prior(parameters, models, floor):
    a, b, c, d = parameters["reference"]["shared_prior"]["emos"]
    values = [models[m] for m in MODELS]
    mu, sigma = a + b * mean(values), sqrt(c + d * pstdev(values) ** 2)
    lo, hi = int(mu - 10 * sigma) - 2, int(mu + 10 * sigma) + 2

    def cdf(x):
        return (1 + erf((x - mu) / (sigma * 2 ** .5))) / 2

    p = distribution({k: (1.0 if k == hi else cdf(k + .5)) -
                      (0.0 if k == lo else cdf(k - .5)) for k in range(lo, hi + 1)})
    result = {}
    for k, v in p.items():
        result[max(floor, k)] = result.get(max(floor, k), 0) + v
    return distribution(result)


def validate_input(row, parameters):
    if (row.get("airport") != "LEMD" or row.get("checkpoint") != CHECKPOINT
            or instant(row["checkpoint_at"]) != cutoff(row["target_date"])
            or instant(parameters["fit_at"]) > instant(row["checkpoint_at"])):
        raise ValueError("Madrid Late identity/fit mismatch")
    if not all(m in row["models"] and isfinite(row["models"][m]) for m in MODELS):
        raise ValueError("Six finite common models required")
    f, floor = row["features"], row["observed_max"]
    cut = cutoff(row["target_date"])
    start, _ = bounds(row["target_date"])
    at, age = instant(f["latest_observed_at"]), f["observation_age"]
    if (not isfinite(floor) or int(floor) != floor or f["observed_max"] != floor
            or not 5 <= age <= parameters["maximum_observation_age_minutes"]
            or not start <= at <= cut or abs((cut - at).total_seconds() / 60 - age) > 1e-8):
        raise ValueError("METAR floor/time/freshness mismatch")
    audit = row["taf_audit"]
    if row["taf_present"] != (audit["status"] == "target_TX_available"):
        raise ValueError("TAF routing mismatch")
    if row["taf_present"]:
        if not instant(audit["issue_time"]) <= instant(audit["available_at"]) <= cut:
            raise ValueError("Future TAF")
        if not bounds(row["target_date"])[0] <= instant(audit["max_temp_at"]) < bounds(row["target_date"])[1]:
            raise ValueError("TAF TX outside target day")
        expected = {"taf_tx_minus_known_metar_max_c": audit["max_temp_c"] - floor,
                    "taf_tx_minus_equal_model_mean_c": audit["max_temp_c"] - mean(row["models"][m] for m in MODELS),
                    "taf_issue_age_hours": (cut - instant(audit["issue_time"])).total_seconds() / 3600}
        if any(f.get(k) is None or abs(f[k] - v) > 1e-8 for k, v in expected.items()):
            raise ValueError("TAF feature mismatch")
    value = f.get("curve_remaining_mean_c")
    if row["curve_available"]:
        if value is None or not isfinite(value) or value < 0:
            raise ValueError("Invalid curve scalar")
        audit = row["curve_audit"]
        if (audit["status"] != "available" or set(audit["models"]) != set(MODELS)
                or audit["observation_at"] != f["latest_observed_at"]):
            raise ValueError("Missing curve audit")
        rises = [d["remaining_rise_c"] for d in audit["models"].values()]
        if any(not isfinite(x) or x < 0 for x in rises) or abs(value - mean(rises)) > 1e-8:
            raise ValueError("Curve scalar/audit mismatch")
        for d in audit["models"].values():
            if instant(d["available_at"]) > cut:
                raise ValueError("Future curve")
    elif value is not None:
        raise ValueError("Invalid missing-curve fallback")


def predict(row, parameters=None):
    parameters = parameters or load_parameters()
    validate_input(row, parameters)
    branch = "B_plus_TAF" if row["taf_present"] else "A_control"
    floor = row["observed_max"]
    backoff = parameters["backoff"]
    background = prior(parameters, row["models"], floor)

    def vector(model):
        q = classifier(model, row["features"])
        p = distribution({floor: q.get(0, 0), floor + 1: q.get(1, 0),
                          **{floor + int(k): q.get(2, 0) * v for k, v in
                             parameters["reference"]["shared_tail"].items()}})
        return distribution({k: (1 - backoff) * p.get(k, 0) + backoff * background.get(k, 0)
                             for k in sorted(set(p) | set(background))})

    reference = vector(parameters["reference"]["models"][branch])
    candidate = vector(parameters["candidate"]["models"][branch]) if row["curve_available"] else dict(reference)
    return {"candidate": candidate, "reference": reference}


def score(values, actual):
    p = distribution(values)
    order = sorted(p, key=lambda k: (-round(p[k], 12), k))
    center = sum(k * v for k, v in p.items())

    def quantile(q):
        total = 0
        for k, v in p.items():
            total += v
            if total >= q:
                return k
        return max(p)

    return {"top1": int(actual == order[0]), "top2": int(actual in order[:2]),
            "mae": abs(center - actual), "bias": center - actual,
            "brier": sum((v - int(k == actual)) ** 2 for k, v in p.items()) + int(actual not in p),
            "log_loss": -log(max(1e-12, p.get(actual, 0))),
            "coverage80": int(quantile(.1) <= actual <= quantile(.9)),
            "confidence": p[order[0]], "modal": order[0]}
