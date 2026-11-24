"""Append-only input archive. No forecast, outcome, database or promotion path."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

from .snapshot import ZONE, checkpoint, digest, instant, validate_snapshot

START_DATE = "2026-11-24"


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def write_once(path, payload):
    path = Path(path)
    body = encode(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text() != body:
            raise ValueError("Refusing to replace an immutable archive file")
        return False
    with path.open("x") as stream:
        stream.write(body)
    return True


def read_record(path):
    r = json.loads(Path(path).read_text())
    if r.get("sha256") != digest({k: v for k, v in r.items() if k != "sha256"}):
        raise ValueError("Archive record digest mismatch")
    return r


def seal(payload):
    return {**payload, "sha256": digest(payload)}


def capture_window(now):
    now = instant(now)
    day = now.astimezone(ZONE).date().isoformat()
    return day >= START_DATE and checkpoint(day) - timedelta(minutes=10) <= now < checkpoint(day)


def build_index(root, protocol):
    root = Path(root)
    selected, gaps, attempts = [], [], []
    for path in sorted((root / "checkpoints").glob("*.json")):
        r = read_record(path)
        if (path.stem != r["target_date"] or r.get("status") != "selected_inputs_only"
                or r.get("protocol_sha256") != digest(protocol)
                or r.get("prospective_OOS") is not False or r.get("forecast_generated") is not False):
            raise ValueError("Invalid archived checkpoint identity")
        bundle_path = root / "bundles" / (r["input_sha256"] + ".json")
        bundle = json.loads(bundle_path.read_text())
        if digest(bundle) != r["input_sha256"]:
            raise ValueError("Captured input object digest mismatch")
        _, counts, _ = validate_snapshot(bundle, protocol)
        if counts != r["members"] or r["source_sha256"] != {k: s["sha256"] for k, s in bundle["sources"].items()}:
            raise ValueError("Checkpoint source summary differs from its captured object")
        cut = checkpoint(r["target_date"])
        if (not capture_window(r["capture_started_at"])
                or bundle["target_date"] != r["target_date"]
                or bundle["capture_started_at"] != r["capture_started_at"]
                or bundle["as_of"] != r["capture_finished_at"]
                or not instant(r["capture_started_at"]) <= instant(r["capture_finished_at"]) <= instant(r["archived_at"]) < cut):
            raise ValueError("Archived inputs violate cutoff")
        if bundle.get("capture_errors") or any(
            not instant(r["capture_started_at"]) <= instant(s["received_at"]) <= instant(r["capture_finished_at"])
            for s in bundle["sources"].values()
        ):
            raise ValueError("Archived source receipts violate capture interval")
        selected.append(r)
    for kind, dest in (("gaps", gaps), ("attempts", attempts)):
        for path in sorted((root / kind).glob("*.json")):
            dest.append(read_record(path))
    for path in sorted((root / "bundles").glob("*.json")):
        if digest(json.loads(path.read_text())) != path.stem:
            raise ValueError("Diagnostic input object digest mismatch")
    if {r["target_date"] for r in selected} & {r["target_date"] for r in gaps}:
        raise ValueError("A selected checkpoint cannot also be a closed gap")
    return {"schema": "wmp_madrid_input_archive_v1", "airport": "LEMD",
            "research_only": True, "data_only": True, "writes_production_database": False,
            "automatic_execution": False, "automatic_promotion": False,
            "forecast_generated": False, "prospective_OOS_days": 0,
            "archive_not_before": START_DATE, "protocol_sha256": digest(protocol),
            "selected_days": len(selected), "gap_days": len(gaps),
            "selected": selected, "gaps": gaps, "attempts": attempts}


def archive_once(root, protocol, *, run_id, collector, mode="auto", clock=None, code_revision="local"):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("Unsafe run identity")
    if mode not in {"auto", "audit"}:
        raise ValueError("Unknown archive mode")
    clock = clock or (lambda: datetime.now(timezone.utc))
    root = Path(root)
    # Verify every existing selected object before reading or mutating anything.
    build_index(root, protocol)
    now = instant(clock())
    day = now.astimezone(ZONE).date().isoformat()
    cut = checkpoint(day)
    selected = root / "checkpoints" / (day + ".json")
    gap = root / "gaps" / (day + ".json")
    status = "audit_only" if mode == "audit" else "outside_capture_window"
    if mode == "auto" and selected.exists():
        status = "already_selected_no_request"
    elif mode == "auto" and gap.exists():
        status = "closed_gap_no_backfill"
    elif mode == "auto" and capture_window(now):
        attempt_path = root / "attempts" / (run_id + ".json")
        if attempt_path.exists():
            return {"status": "attempt_already_recorded", "journal": build_index(root, protocol)}
        bundle = collector()
        finished = instant(clock())
        input_hash = digest(bundle)
        write_once(root / "bundles" / (input_hash + ".json"), bundle)
        reason, counts = None, {}
        try:
            if bundle["target_date"] != day:
                raise ValueError("Capture crossed target day")
            started, captured = instant(bundle["capture_started_at"]), instant(bundle["as_of"])
            if not now <= started <= captured <= finished:
                raise ValueError("Source capture clock inconsistent")
            if any(not started <= instant(s["received_at"]) <= captured for s in bundle["sources"].values()):
                raise ValueError("Source receipt outside actual capture interval")
            if bundle.get("capture_errors"):
                raise ValueError("Source capture reported incomplete requests")
            if finished >= cut:
                reason = "late_capture_not_selected"
            else:
                _, counts, _ = validate_snapshot(bundle, protocol)
        except (ValueError, KeyError, TypeError) as exc:
            reason = "invalid_or_incomplete_input: " + str(exc)
        status = reason or "selected_inputs_only"
        common = {"target_date": day, "run_id": run_id, "status": status,
                  "code_revision": code_revision,
                  "attempt_started_at": now.isoformat(), "archived_at": finished.isoformat(),
                  "input_sha256": input_hash, "protocol_sha256": digest(protocol),
                  "members": counts, "forecast_generated": False, "prospective_OOS": False}
        write_once(attempt_path, seal(common))
        if reason is None:
            record = {**common, "capture_started_at": bundle["capture_started_at"],
                      "capture_finished_at": bundle["as_of"],
                      "source_sha256": {k: s["sha256"] for k, s in bundle["sources"].items()}}
            write_once(selected, seal(record))
    elif mode == "auto" and day >= START_DATE and now >= cut + timedelta(minutes=10):
        write_once(gap, seal({"target_date": day, "status": "no_qualifying_inputs_before_cutoff",
                              "recorded_at": now.isoformat(), "run_id": run_id,
                              "forecast_generated": False, "prospective_OOS": False}))
        status = "gap_recorded"
    journal = build_index(root, protocol)
    root.mkdir(parents=True, exist_ok=True)
    # Only this derived index may change; source and decision objects are immutable.
    (root / "journal.json").write_text(encode(journal))
    return {"status": status, "journal": journal}
