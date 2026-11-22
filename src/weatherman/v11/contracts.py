"""Fail-closed temporal and probability contracts; no network or database access."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from math import erf, exp, isfinite, log
from zoneinfo import ZoneInfo


EVIDENCE_CLASSES = frozenset({
    "scheduled_causal_production", "scheduled_causal_shadow", "historical_exact_run",
    "historical_reduced_fidelity", "reconstructed", "unknown_provenance",
})


def instant(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required")
    return result.astimezone(timezone.utc)


def day_bounds(day: str, tz: str = "Europe/Madrid") -> tuple[datetime, datetime]:
    d = date.fromisoformat(day)
    zone = ZoneInfo(tz)
    return (datetime.combine(d, time(), zone).astimezone(timezone.utc),
            datetime.combine(d + timedelta(days=1), time(), zone).astimezone(timezone.utc))


def checkpoint(day: str, hour: int, previous_day: bool = False,
               tz: str = "Europe/Madrid") -> datetime:
    d = date.fromisoformat(day) - timedelta(days=int(previous_day))
    return datetime.combine(d, time(hour), ZoneInfo(tz)).astimezone(timezone.utc)


@dataclass(frozen=True)
class ModelRun:
    model: str
    initialized_at: str
    available_at: str
    received_at: str
    value: float

    def require_causal(self, cutoff: str, max_age_hours: float = 48) -> None:
        init, available, received, cut = map(instant, (
            self.initialized_at, self.available_at, self.received_at, cutoff))
        if not init <= available <= received <= cut:
            raise ValueError("Run initialization/publication/reception exceeds cutoff or is inconsistent")
        if (cut - init).total_seconds() > max_age_hours * 3600:
            raise ValueError("Stale model run")
        if not isfinite(self.value):
            raise ValueError("Non-finite forecast")


def require_training_labels(rows: list[dict], fit_at: str) -> None:
    for row in rows:
        if instant(row["actual_available_at"]) >= instant(fit_at):
            raise ValueError("Training label unavailable at fit time")


def distribution(values: dict) -> dict[int, float]:
    result = {}
    for key, value in values.items():
        numeric_key = float(key)
        if not isfinite(numeric_key) or not numeric_key.is_integer() or int(numeric_key) in result:
            raise ValueError("Non-integer or duplicate bucket boundary")
        result[int(numeric_key)] = float(value)
    if not result or any(not isfinite(p) or p < 0 for p in result.values()):
        raise ValueError("Invalid probability vector")
    total = sum(result.values())
    if abs(total - 1) > 1e-5:
        raise ValueError("Incomplete probability vector")
    return {k: p / total for k, p in sorted(result.items())}


def normal_buckets(mu: float, sigma: float) -> dict[int, float]:
    """Normal statistical location fitted to reported METAR integers, not physical Tmax.

    Integrate half-degree cells. Endpoint cells absorb numerical tails. No truth-dependent
    support and no renormalizing away mass below an observed floor.
    """
    if not isfinite(mu) or not isfinite(sigma) or sigma <= 0:
        raise ValueError("Finite mean and positive spread required")
    lo, hi = int(mu - 10 * sigma) - 2, int(mu + 10 * sigma) + 2
    def cdf(x):
        return (1 + erf((x - mu) / (sigma * 2 ** .5))) / 2
    return distribution({k: (1.0 if k == hi else cdf(k + .5)) -
                         (0.0 if k == lo else cdf(k - .5)) for k in range(lo, hi + 1)})


def observed_floor(prior: dict, floor: int | None) -> dict[int, float]:
    """Pushforward max(observed, future). Accumulate lower mass AT the floor."""
    prior = distribution(prior)
    if floor is None:
        return prior
    result = {}
    for k, p in prior.items():
        result[max(floor, k)] = result.get(max(floor, k), 0) + p
    return distribution(result)


def delta_probabilities(values: dict, observed: int) -> dict[str, float]:
    p = distribution(values)
    if any(k < observed and v > 1e-12 for k, v in p.items()):
        raise ValueError("Mass below observed maximum")
    return {"hold": p.get(observed, 0), "plus_one": p.get(observed + 1, 0),
            "plus_two_or_more": sum(v for k, v in p.items() if k >= observed + 2)}


def remaining_hazard(windows: list[tuple[str, float]], cutoff: str, day: str,
                     tz: str = "Europe/Madrid") -> float:
    """Conditional hazard aggregation. No sunset/hour lock; caller supplies fitted hazards."""
    cut = instant(cutoff)
    start, end = day_bounds(day, tz)
    if not start <= cut < end:
        raise ValueError("Cutoff outside target day")
    survival, previous = 1.0, cut
    for at, hazard in windows:
        at = instant(at)
        if not previous < at <= end or not isfinite(hazard) or not 0 <= hazard <= 1:
            raise ValueError("Invalid hazard or unordered/non-future window")
        survival *= 1 - hazard
        previous = at
    return 1 - survival


def reported_bucket(value: float, policy: str) -> int:
    """Explicit rounding contract; do not assume an exchange uses this policy."""
    if policy != "half_away_from_zero" or not isfinite(value):
        raise ValueError("Explicit supported rounding policy required")
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def market_partition(values: dict, buckets: list[dict]) -> dict[str, float]:
    """Map reported integer target into exhaustive disjoint inclusive market bounds."""
    prior, result = distribution(values), {b["id"]: 0.0 for b in buckets}
    if len(result) != len(buckets):
        raise ValueError("Duplicate market outcome")
    for k, p in prior.items():
        hits = [b for b in buckets if (b["lower"] is None or k >= b["lower"]) and
                (b["upper"] is None or k <= b["upper"])]
        if len(hits) != 1:
            raise ValueError("Market boundaries overlap or omit forecast support")
        result[hits[0]["id"]] += p
    return result


def score(values: dict, actual: int) -> dict:
    p = distribution(values)
    # Symmetric CDF differences can differ by one floating-point bit across libc
    # implementations. Resolve probability ties at 12 decimals by lower bucket.
    # Probabilities and proper scores themselves retain full precision.
    order = sorted(p, key=lambda k: (-round(p[k], 12), k))
    center = sum(k * v for k, v in p.items())
    low, high = min(min(p), actual), max(max(p), actual)
    cumulative, rps = 0.0, 0.0
    for k in range(low, high + 1):
        cumulative += p.get(k, 0)
        rps += (cumulative - int(actual <= k)) ** 2
    def quantile(q):
        c = 0
        for k, v in p.items():
            c += v
            if c >= q:
                return k
        return max(p)
    winner = p.get(actual, 0)
    return {"modal": order[0], "center": center, "top1": int(actual == order[0]),
            "top2": int(actual in order[:2]), "mae": abs(center - actual),
            "bias": center - actual,
            "brier": sum((v - int(k == actual)) ** 2 for k, v in p.items()) + int(actual not in p),
            "log_loss": -log(max(1e-12, winner)), "zero_winner_mass": int(winner == 0),
            "discrete_crps": rps, "winner_probability": winner,
            "coverage80": int(quantile(.1) <= actual <= quantile(.9)),
            "width80": quantile(.9) - quantile(.1), "confidence": p[order[0]]}


def temperature_scale(values: dict, temperature: float) -> dict[int, float]:
    p = distribution(values)
    if not isfinite(temperature) or temperature <= 0:
        raise ValueError("Positive temperature required")
    logs = {k: log(v) / temperature for k, v in p.items() if v > 0}
    peak = max(logs.values())
    out = {k: exp(v - peak) for k, v in logs.items()}
    total = sum(out.values())
    return distribution({k: v / total for k, v in out.items()})
