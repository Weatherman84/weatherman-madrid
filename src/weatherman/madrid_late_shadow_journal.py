"""Append-only Madrid Shadow decisions, with separately appended outcomes."""

from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from statistics import mean

from .madrid_late_shadow_model import (
    CHECKPOINT, PARAMETER_SHA, VERSION, ZONE, cutoff, digest, distribution, instant, score,
)
from .madrid_late_shadow_inputs import capture_due


def seal(payload):
    return {**payload, "sha256": digest(payload)}


def verify(record):
    payload = {k: v for k, v in record.items() if k != "sha256"}
    if record.get("sha256") != digest(payload):
        raise ValueError("Shadow record digest mismatch")
    return record


def validate_decision(record):
    verify(record)
    if (record.get("candidate_id") != VERSION or record.get("parameter_sha256") != PARAMETER_SHA
            or record.get("airport") != "LEMD" or record.get("checkpoint") != CHECKPOINT
            or record.get("research_only") is not True or record.get("automatic_promotion") is not False
            or record.get("automatic_execution") is not False
            or record.get("input_source_class") not in {"live_point_in_time_composite_v1", "live_open_meteo_coherent_day_v2"}):
        raise ValueError("Invalid Shadow identity/safety flags")
    if record["input_source_class"] == "live_open_meteo_coherent_day_v2":
        from .madrid_coherent_inputs import validate_bundle
        validate_bundle(record["inputs"])
    cut = cutoff(record["target_date"])
    if (instant(record["checkpoint_at"]) != cut or not capture_due(record["captured_at"])
            or cutoff(instant(record["captured_at"]).astimezone(ZONE).date()) != cut
            or not instant(record["captured_at"]) <= instant(record["generated_at"]) < cut):
        raise ValueError("Shadow was not frozen before its cutoff")
    if record.get("curve_available") not in (True, False):
        raise ValueError("Missing curve/fallback status")
    for name in ("candidate", "reference"):
        distribution(record[name])
    if digest(record["inputs"]) != record["input_sha256"]:
        raise ValueError("Frozen input digest mismatch")
    if any(record["inputs"].get(k) != record[k] for k in
           ("airport", "target_date", "checkpoint", "checkpoint_at", "curve_available", "input_source_class")):
        raise ValueError("Frozen input/decision identity mismatch")
    if not record["curve_available"] and record["candidate"] != record["reference"]:
        raise ValueError("Nonexact fallback")
    return record


def decision(row, *, captured_at, generated_at, run_id):
    from .madrid_late_shadow_model import predict

    vectors = predict(row)
    record = seal({"candidate_id": VERSION, "parameter_sha256": PARAMETER_SHA,
                   "airport": "LEMD", "checkpoint": CHECKPOINT,
                   "target_date": row["target_date"], "checkpoint_at": row["checkpoint_at"],
                   "captured_at": instant(captured_at).isoformat(),
                   "generated_at": instant(generated_at).isoformat(), "run_id": str(run_id),
                   "research_only": True, "automatic_promotion": False, "automatic_execution": False,
                   "input_source_class": row["input_source_class"],
                   "curve_available": row["curve_available"], "input_sha256": digest(row),
                   "curve_missing_reasons": row["curve_audit"].get("missing_reasons", {}),
                   "inputs": row, **vectors})
    return validate_decision(record)


def append_record(root, kind, record):
    """Create once. Conflicting retry raises; existing bytes are never rewritten."""
    verify(record)
    path = Path(root) / kind / (record["target_date"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(record, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n"
    if path.exists():
        if json.loads(path.read_text()) != json.loads(body):
            raise ValueError("Refusing to replace an immutable " + kind + " record")
        return False
    with path.open("x") as stream:
        stream.write(body)
    return True


def records(root, kind):
    result = []
    for path in sorted((Path(root) / kind).glob("*.json")):
        r = json.loads(path.read_text())
        if path.stem != r["target_date"]:
            raise ValueError("Journal filename/identity mismatch")
        result.append(validate_decision(r) if kind == "decisions" else verify(r))
    return result


def outcome(record, actual, champion, *, evaluated_at):
    validate_decision(record)
    at = instant(evaluated_at)
    # Final measured METAR day only, after 06:00 Madrid on the next local day.
    earliest = datetime.combine(date.fromisoformat(record["target_date"]) + timedelta(days=1), time(6), ZONE)
    if at < earliest:
        raise ValueError("Outcome attempted before next-day closeout")
    if int(actual) != actual or actual < record["inputs"]["observed_max"]:
        raise ValueError("Invalid measured METAR outcome")
    vectors = {k: record[k] for k in ("candidate", "reference")}
    if champion is not None:
        vectors["champion"] = distribution(champion["probabilities"])
    return seal({"target_date": record["target_date"], "decision_sha256": record["sha256"],
                 "evaluated_at": at.isoformat(), "actual": actual, "actual_source": "stored-metar-station",
                 "champion": champion, "scores": {k: score(v, actual) for k, v in vectors.items()}})


def summary(decisions, outcomes, curve_available):
    selected = {r["target_date"]: r for r in decisions if r["curve_available"] == curve_available}
    pairs = [o for o in outcomes if o["target_date"] in selected]
    result = {"frozen_days": len(selected), "resolved_days": len(pairs), "metrics": {}}
    for name in ("candidate", "reference", "champion"):
        part = [o["scores"][name] for o in pairs if name in o["scores"]]
        if not part:
            continue
        bins = []
        for i in range(10):
            group = [s for s in part if min(int(s["confidence"] * 10), 9) == i]
            if group:
                bins.append({"lower": i / 10, "n": len(group),
                             "predicted": mean(s["confidence"] for s in group),
                             "observed": mean(s["top1"] for s in group)})
        result["metrics"][name] = {"n": len(part), **{
            k: mean(s[k] for s in part) for k in
            ("top1", "top2", "mae", "bias", "brier", "log_loss", "coverage80")},
            "reliability": bins,
            "calibration_error": sum(b["n"] * abs(b["predicted"] - b["observed"]) for b in bins) / len(part)}
    paired = [o for o in pairs if "champion" in o["scores"]]
    result["paired_champion_days"] = len(paired)
    result["paired_champion_metrics"] = {
        name: {k: mean(o["scores"][name][k] for o in paired) for k in
               ("top1", "top2", "mae", "bias", "brier", "log_loss", "coverage80")}
        for name in ("candidate", "champion") if paired}
    return result


def recording_coverage(decisions, as_of):
    """Describe absent recordings, never fabricate forecasts or OOS evidence."""
    generated = instant(as_of)
    today = generated.astimezone(ZONE).date()
    closed = today if generated >= cutoff(today) + timedelta(minutes=10) else today - timedelta(days=1)
    start = date(2026, 11, 24)
    recorded = {r["target_date"] for r in decisions}
    missing = [(start + timedelta(days=i)).isoformat() for i in range(max(0, (closed - start).days + 1))
               if (start + timedelta(days=i)).isoformat() not in recorded]
    return {"recording_start_date": start.isoformat(), "closed_through": closed.isoformat(),
            "missing_days": missing, "missing_day_count": len(missing),
            "classification": "recording_coverage_only_not_forecasts"}


def build_journal(root, as_of=None):
    decisions, outcomes = records(root, "decisions"), records(root, "outcomes")
    by_day = {r["target_date"]: r for r in decisions}
    for o in outcomes:
        if o["target_date"] not in by_day or o["decision_sha256"] != by_day[o["target_date"]]["sha256"]:
            raise ValueError("Outcome does not match immutable decision")
    generated = instant(as_of or datetime.now(timezone.utc))
    attempts = []
    for path in sorted((Path(root) / "attempts").glob("*.json")):
        attempt = verify(json.loads(path.read_text()))
        if attempt.get("classification") != "operational_attempt_not_forecast":
            raise ValueError("Invalid operational attempt")
        attempts.append(attempt)
    attempts.sort(key=lambda a: a["recorded_at"])
    classes = sorted({d["input_source_class"] for d in decisions})
    cohorts = {source: {"curve_evidence": summary([d for d in decisions if d["input_source_class"] == source], outcomes, True),
                        "fallback_only": summary([d for d in decisions if d["input_source_class"] == source], outcomes, False)}
               for source in classes}
    return {"schema": "madrid_late_prospective_shadow_v1", "candidate_id": VERSION,
            "airport": "LEMD", "parameter_sha256": PARAMETER_SHA, "research_only": True,
            "automatic_promotion": False, "automatic_execution": False,
            "generated_at": generated.isoformat(),
            "capture_window_local": "15:50 <= capture < 16:00 Europe/Madrid; first valid capture wins",
            "decisions": decisions, "outcomes": outcomes, "capture_attempts": attempts,
            "evidence_by_input_source_class": cohorts,
            "aggregate_metrics_classification": "mixed_input_versions_diagnostic_only" if len(classes) > 1 else "single_input_version",
            "recording_coverage": recording_coverage(decisions, generated),
            "curve_evidence": summary(decisions, outcomes, True),
            "fallback_only": summary(decisions, outcomes, False)}
