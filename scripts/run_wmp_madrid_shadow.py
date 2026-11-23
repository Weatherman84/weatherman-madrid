"""Replay a captured WMP input bundle offline and report historical data readiness."""

import argparse
import json
from pathlib import Path

from weatherman.research_wmp.snapshot import checkpoint, digest, instant, member_counts, run_snapshot

ROOT = Path(__file__).resolve().parents[1]


def coverage(cases, snapshots):
    eligible = []
    reasons = {"missing_target_member_snapshot": 0, "late_or_incomplete_source_bundle": 0}
    for case in cases:
        day = case["target_date"]
        candidates = [s for s in snapshots if s["target_date"] == day]
        if not candidates:
            reasons["missing_target_member_snapshot"] += 1
            continue
        accepted = False
        for snapshot in candidates:
            try:
                cut = checkpoint(day)
                if instant(snapshot["as_of"]) > cut or any(instant(s["received_at"]) > cut for s in snapshot["sources"].values()):
                    continue
                member_counts(snapshot["sources"]["ensemble"]["data"], day)
                accepted = True
            except (ValueError, KeyError, TypeError):
                continue
        if accepted:
            eligible.append(day)
        else:
            reasons["late_or_incomplete_source_bundle"] += 1
    return {"cases_checked": len(cases), "candidate_snapshot_dates": sorted({s["target_date"] for s in snapshots}),
            "member_archive_screen_passed_cases": len(eligible), "eligible_dates": eligible,
            "excluded": reasons, "full_historical_skill_evaluation_performed": False,
            "full_historical_scores": None,
            "live_activation": False,
            "gate_status": "BLOCKED_MISSING_HISTORICAL_MEMBER_INPUTS" if not eligible else "REQUIRES_FULL_REPLAY_AND_INDEPENDENT_EVALUATION",
            "note": "A member-archive screen alone does not validate source hashes, calibration or forecast skill. No deterministic proxy is used."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, default=ROOT / "research/wmp_madrid_shadow_v1/PROTOCOL.json")
    parser.add_argument("--cases", type=Path, default=ROOT / "src/weatherman/data/madrid_late_curve_shadow_v1.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to replace an existing research result")
    snapshot = json.loads(args.snapshot.read_text())
    protocol = json.loads(args.protocol.read_text())
    forecast = run_snapshot(snapshot, protocol)
    audit = coverage(json.loads(args.cases.read_text())["cases"], [snapshot])
    result = {"forecast_diagnostic": forecast, "historical_readiness": audit,
              "input_file_sha256": digest(snapshot), "code_status": "offline_research_prototype",
              "production_changes": False, "live_job_enabled": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": forecast["status"], "members": sum(forecast["members"].values()),
                      "historical_cases": audit["cases_checked"],
                      "replayable_member_cases": audit["member_archive_screen_passed_cases"],
                      "activation_gate": audit["gate_status"]}))


if __name__ == "__main__":
    main()
