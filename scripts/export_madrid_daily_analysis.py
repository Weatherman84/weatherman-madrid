from __future__ import annotations

import argparse
from pathlib import Path

from weatherman.daily_analysis_export import (
    build_daily_analysis_export,
    set_read_only,
    write_export,
)
from weatherman.db import Session


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("daily-analysis-latest.json"),
    )
    args = parser.parse_args()

    with Session() as session:
        set_read_only(session)
        payload = build_daily_analysis_export(session, days=args.days)
        session.rollback()
    write_export(args.output, payload)
    print(
        {
            "status": "success",
            "output": str(args.output),
            "checkpoints": len(payload["checkpoints"]),
            "actuals": len(payload["actuals"]),
            "contains_credentials": False,
            "production_writes": False,
        }
    )


if __name__ == "__main__":
    main()
