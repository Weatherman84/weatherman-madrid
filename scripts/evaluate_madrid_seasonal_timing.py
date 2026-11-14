#!/usr/bin/env python3
"""Build an offline Madrid seasonal/timing research report from exports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from weatherman.seasonal_timing_research import (
    DEFAULT_HANDOFF_REGISTRY,
    build_research_report,
    load_exports,
    load_handoff_registry,
    report_markdown,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", required=True, help="daily-analysis JSON")
    parser.add_argument("--output", required=True, help="research report JSON")
    parser.add_argument("--markdown-output", help="optional human-readable report")
    parser.add_argument("--start-date", help="inclusive diagnostic window start (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="inclusive diagnostic window end (YYYY-MM-DD)")
    parser.add_argument(
        "--handoff-registry",
        default=str(DEFAULT_HANDOFF_REGISTRY),
        help="structured registry of completed Madrid research handoffs",
    )
    args = parser.parse_args()

    payloads, provenance = load_exports(args.input)
    handoffs, handoff_provenance = load_handoff_registry(args.handoff_registry)
    report = build_research_report(
        payloads,
        provenance=provenance,
        analysis_start_date=args.start_date,
        analysis_end_date=args.end_date,
        handoff_registry=handoffs,
        handoff_provenance=handoff_provenance,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    if args.markdown_output:
        markdown = Path(args.markdown_output)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(report_markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
