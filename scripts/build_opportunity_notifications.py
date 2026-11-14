#!/usr/bin/env python3
"""Build deduplicated GitHub notification payloads from two scanner journals."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from weatherman.opportunity_notifications import (
    format_github_comment,
    select_new_notification_candidates,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    previous = json.loads(args.previous.read_text(encoding="utf-8"))
    current = json.loads(args.current.read_text(encoding="utf-8"))
    candidates = select_new_notification_candidates(previous, current)
    payload = [
        {**candidate, "github_comment": format_github_comment(candidate)}
        for candidate in candidates
    ]
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({"notification_candidates": len(payload)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
