"""Read-only frozen v11 Late display data. No inference, DB or trading imports."""

from datetime import date, datetime
import hashlib
import json
from math import isfinite
from pathlib import Path
from zoneinfo import ZoneInfo


DATA = Path(__file__).with_name("data") / "madrid_late_curve_shadow_v1.json"
VERSION = "madrid_late_single_curve_feature_v1"


def probabilities(values):
    if not isinstance(values, dict) or not values:
        raise ValueError("Missing frozen probabilities")
    result = {}
    for k, v in values.items():
        number, p = float(k), float(v)
        if not number.is_integer() or not isfinite(p) or not 0 <= p <= 1:
            raise ValueError("Invalid frozen probability")
        key = int(number)
        if key in result:
            raise ValueError("Duplicate bucket")
        result[key] = p
    if abs(sum(result.values()) - 1) > 1e-8:
        raise ValueError("Incomplete frozen probability vector")
    return result


def load_bundle(path=DATA):
    path = Path(path)
    body = path.read_bytes()
    parts = path.with_suffix(".sha256").read_text().split()
    if not parts or len(parts[0]) != 64 or any(c not in "0123456789abcdef" for c in parts[0]):
        raise ValueError("Invalid shadow display archive checksum")
    expected = parts[0]
    if hashlib.sha256(body).hexdigest() != expected:
        raise ValueError("Shadow display archive checksum mismatch")
    bundle = json.loads(body)
    if (
        bundle.get("candidate_id") != VERSION
        or bundle.get("airport") != "LEMD"
        or bundle.get("research_only") is not True
        or bundle.get("automatic_promotion") is not False
        or bundle.get("prospective_OOS_days") != 0
    ):
        raise ValueError("Unsupported shadow display archive")
    seen = set()
    if not bundle.get("cases") or not any(r["curve_available"] for r in bundle["cases"]):
        raise ValueError("Missing comparable historical shadow cases")
    for name in ("candidate", "reference"):
        summary = bundle["benchmark"][name]
        if (
            summary["n"] <= 0
            or not 0 <= summary["top1"] <= 1
            or any(not isfinite(summary[k]) or summary[k] < 0 for k in ("brier", "log_loss", "mae"))
        ):
            raise ValueError("Invalid historical benchmark")
    for case in bundle["cases"]:
        day = date.fromisoformat(case["target_date"])
        cut = datetime.fromisoformat(case["cutoff"])
        if cut.tzinfo is None:
            raise ValueError("Missing frozen cutoff timezone")
        cut = cut.astimezone(ZoneInfo("Europe/Madrid"))
        if case["target_date"] in seen or cut.date() != day or (cut.hour, cut.minute) != (16, 0):
            raise ValueError("Invalid frozen Late case identity")
        seen.add(case["target_date"])
        for name in ("candidate", "reference", "champion"):
            probabilities(case[name])
        if not case["curve_available"] and probabilities(case["candidate"]) != probabilities(
            case["reference"]
        ):
            raise ValueError("Invalid reference fallback")
    return bundle


def view_for_target(bundle, target):
    target = target.isoformat() if isinstance(target, date) else str(target)
    record = next((r for r in bundle["cases"] if r["target_date"] == target), None)
    return {
        "target_date": target,
        "record": record,
        "live_available": False,
        "status": "missing_snapshot"
        if record is None
        else "research_replay"
        if record["curve_available"]
        else "reference_fallback",
    }


def modal(values):
    p = probabilities(values)
    k = min(p, key=lambda k: (-round(p[k], 12), k))
    return k, p[k]


def table_view_for_target(target, bundle=None, live=None):
    """One view for both existing cockpit tables; missing remains missing."""
    if live is not None:
        try:
            from .madrid_late_shadow_public import validate_journal
            from .madrid_late_shadow_model import ZONE, instant

            validate_journal(live)
            day = target.isoformat() if isinstance(target, date) else str(target)
            record = next((r for r in live["decisions"] if r["target_date"] == day), None)
            if record is not None:
                at = instant(record["captured_at"]).astimezone(ZONE).strftime("%H:%M:%S")
                reasons = record.get("curve_missing_reasons", {})
                reason_text = "; ".join(f"{capture_reason_label(reason)} ({list(reasons.values()).count(reason)}/6 models)"
                                        for reason in sorted(set(reasons.values())))
                evidence = live.get("evidence_by_input_source_class", {}).get(record["input_source_class"], live)
                kind = "prospective curve candidate" if record["curve_available"] else "prospective reference fallback; curve unavailable"
                return {"status": "prospective_shadow" if record["curve_available"] else "prospective_fallback",
                        "target_date": day, "record": record, "live_available": True,
                        "probabilities": probabilities(record["candidate"]),
                        "caption": f"v11 Shadow · Late 16:00: {kind}; inputs frozen {at} Madrid. "
                                   f"Source {record['input_source_class']}; parameters {record['parameter_sha256'][:12]}. "
                                   f"Curve days {evidence['curve_evidence']['frozen_days']}; fallback days {evidence['fallback_only']['frozen_days']}. "
                                   f"Research only; First Live unchanged. {reason_text}"}
            coverage = live.get("recording_coverage", {})
            attempts = [a for a in live.get("capture_attempts", []) if a["target_date"] == day]
            reason = capture_reason_label(attempts[-1]["reason"]) if attempts else INCIDENT_REASONS.get(day, "reason not recorded")
            if isinstance(coverage, dict) and day in coverage.get("missing_days", []):
                return {"status": "missed_checkpoint", "target_date": day, "record": None,
                        "live_available": False, "probabilities": {},
                        "caption": f"v11 Shadow · Late 16:00: no valid pre-cutoff forecast was recorded for {day}. Reason: {reason}. The missing checkpoint is not backfilled."}
        except (ValueError, KeyError, TypeError):
            live = {"status": "live_journal_unavailable"}
    try:
        view = view_for_target(bundle if bundle is not None else load_bundle(), target)
    except (OSError, ValueError, KeyError, TypeError):
        return {
            "status": "archive_unavailable",
            "probabilities": {},
            "caption": "v11 Shadow · Late 16:00: archive unavailable.",
            "record": None,
        }
    record = view["record"]
    view["probabilities"] = probabilities(record["candidate"]) if record else {}
    if record is None:
        view["caption"] = (
            "v11 Shadow · Late 16:00: — = no recorded Shadow for this day; missed cutoffs are not backfilled."
        )
        if live and live.get("status") == "live_journal_unavailable":
            view["caption"] += " Live journal temporarily unavailable."
    elif not record["curve_available"]:
        view["caption"] = (
            "v11 Shadow · Late 16:00: retrospective reference fallback; no comparable curve. Not a live forecast."
        )
    else:
        view["caption"] = (
            "v11 Shadow · Late 16:00: saved retrospective research forecast, not the current live nowcast. First Live unchanged."
        )
    return view


def checkpoint_shadow_value(view, checkpoint):
    if checkpoint != "Late Live @16:00" or not view["probabilities"]:
        return "—"
    bucket, p = modal(view["probabilities"])
    return f"{bucket} °C · {p:.1%}"


def bucket_shadow_value(view, bucket):
    return f"{view['probabilities'].get(int(bucket), 0):.1%}" if view["probabilities"] else "—"


def comparison_rows(case):
    vectors = {
        "Shadow candidate": probabilities(case["candidate"]),
        "TX research reference": probabilities(case["reference"]),
        "Archived Champion": probabilities(case["champion"]),
    }
    all_buckets = set().union(*(p.keys() for p in vectors.values()))
    selected = {k for k in all_buckets if max(p.get(k, 0) for p in vectors.values()) >= 0.001}
    selected.add(int(case["actual"]))
    selected.update(modal(p)[0] for p in vectors.values())
    rows = [
        {"Bucket": f"{k} °C", **{name: p.get(k, 0) for name, p in vectors.items()}}
        for k in sorted(selected)
    ]
    remaining = all_buckets - selected
    if remaining:
        rows.append(
            {
                "Bucket": "Other buckets",
                **{name: sum(p.get(k, 0) for k in remaining) for name, p in vectors.items()},
            }
        )
    return rows


INCIDENT_REASONS = {
    "2026-11-24": "no trigger reached the capture window (incident reviewed 2026-11-27)",
    "2026-11-27": "latest ECMWF forecast rejected as invalid/stale (incident reviewed 2026-11-27)",
}


def component_views(view):
    """Reference and emitted fallback are not two independently scored candidates."""
    record = view.get("record")
    reference = probabilities(record["reference"]) if record else {}
    active = bool(record and record["curve_available"])
    return {
        "v11 Reference · Late": {"probabilities": reference},
        "v11 Curve · Late": {"probabilities": view["probabilities"] if active else {}},
        "v11 Fallback · Late": {"probabilities": view["probabilities"] if record and not active else {}},
    }


def stored_late_champion(variants, target):
    """Exact day/time match; never substitute the current Champion nowcast."""
    from .madrid_late_shadow_model import cutoff, instant
    if variants.empty:
        return {}
    selected = [r for r in variants.to_dict("records")
                if str(r.get("target_date"))[:10] == str(target)[:10]
                and r.get("airport") == "LEMD" and r.get("variant") == "Champion"
                and instant(r["captured_at"]) == cutoff(target)]
    if len(selected) != 1:
        return {}
    try:
        values = selected[0]["probabilities_json"]
        return probabilities(json.loads(values) if isinstance(values, str) else values)
    except (ValueError, KeyError, TypeError):
        return {}


def capture_reason_label(reason):
    return {
        "latest_fetch_too_old": "latest model receipt is older than six hours",
        "latest_run_too_old": "latest model cycle is older than 48 hours",
        "stale_metar": "no sufficiently fresh METAR",
        "model_source_unavailable": "weather model source unavailable",
        "incomplete_local_day": "local-day hourly curve incomplete",
        "daily_hourly_maximum_mismatch": "daily maximum differs from hourly maximum",
        "latest_matched_run_has_incomplete_hourly_day": "matching hourly day is incomplete",
        "model_updated_during_fetch": "model changed during source capture",
        "invalid_forecast_time_order": "source timestamps are inconsistent",
    }.get(reason, reason.replace("_", " "))
