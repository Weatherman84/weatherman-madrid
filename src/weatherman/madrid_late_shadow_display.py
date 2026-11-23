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
                kind = "prospective curve candidate" if record["curve_available"] else "prospective reference fallback; curve unavailable"
                return {"status": "prospective_shadow" if record["curve_available"] else "prospective_fallback",
                        "target_date": day, "record": record, "live_available": True,
                        "probabilities": probabilities(record["candidate"]),
                        "caption": f"v11 Shadow · Late 16:00: {kind}; inputs frozen {at} Madrid. Research only; First Live unchanged."}
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
