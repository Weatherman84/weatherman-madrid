#!/usr/bin/env python3
"""Build a compact causal six-airport feed without a database connection."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import httpx

from weatherman.opportunity_feed import build_live_feed, validate_live_feed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("opportunity-scanner-feed.json"))
    parser.add_argument(
        "--existing-url", help="Existing compact feed; used only for rolling peak history"
    )
    parser.add_argument(
        "--as-of", help="UTC ISO timestamp for deterministic tests/manual diagnostics"
    )
    args = parser.parse_args()
    existing = None
    if args.existing_url:
        response = httpx.get(args.existing_url, timeout=15, follow_redirects=True)
        if response.status_code != 404:
            response.raise_for_status()
            existing = response.json()
            validate_live_feed(existing)
    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else None
    feed = build_live_feed(as_of=as_of, existing_feed=existing)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(feed, default=str, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "built",
                "output": str(args.output),
                "airports": len(feed["forecasts"]),
                "markets": len(feed["markets"]),
                "collection_errors": len(feed["collection_errors"]),
                "writes_production_database": False,
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
