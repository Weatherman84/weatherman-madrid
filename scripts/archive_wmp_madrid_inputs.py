"""Archive public WMP inputs only, with no DB secrets or forecast generation."""

import argparse
import json
import os
from pathlib import Path

from weatherman.research_wmp.archive import archive_once
from weatherman.research_wmp.capture import capture_snapshot

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, default=ROOT / "research/wmp_madrid_shadow_v1/PROTOCOL.json")
    parser.add_argument("--mode", choices=("auto", "audit"), default="auto")
    args = parser.parse_args()
    run_id = os.getenv("GITHUB_RUN_ID", "local") + "-" + os.getenv("GITHUB_RUN_ATTEMPT", "1")
    result = archive_once(args.store, json.loads(args.protocol.read_text()), run_id=run_id,
                          collector=lambda: capture_snapshot(allow_partial=True), mode=args.mode,
                          code_revision=os.getenv("WMP_CODE_REVISION", "local"))
    journal = result["journal"]
    print(json.dumps({"status": result["status"], "selected_days": journal["selected_days"],
                      "gap_days": journal["gap_days"], "forecast_generated": False,
                      "prospective_OOS_days": 0, "writes_production_database": False}))


if __name__ == "__main__":
    main()
