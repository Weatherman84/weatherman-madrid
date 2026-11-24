"""One manual research input capture. No scheduler, inference, DB or credentials."""

import argparse
from datetime import datetime
import json
from pathlib import Path

from weatherman.research_wmp.capture import capture_snapshot, request_plan
from weatherman.research_wmp.snapshot import ZONE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", action="store_true", help="Print fixed public requests without fetching")
    args = parser.parse_args()
    if args.plan:
        print(json.dumps(request_plan(datetime.now(ZONE).date()), indent=2))
        return
    if args.output.exists():
        raise SystemExit("Refusing to replace an existing captured input file")
    snapshot = capture_snapshot()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        stream.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
    print(json.dumps({"status": "captured_inputs_only", "output": str(args.output),
                      "target_date": snapshot["target_date"], "bytes": args.output.stat().st_size,
                      "prospective_OOS": False, "scheduled_job_created": False}))


if __name__ == "__main__":
    main()
