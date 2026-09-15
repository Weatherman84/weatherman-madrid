"""Export both v0.1 research tracks in one bounded Production-Neon read."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, text

from prepare_replay_lab import normalized_url
from weatherman.research_replay_export import (
    ESTIMATED_BYTES_PER_CHECKPOINT,
    MAX_CHECKPOINT_ROWS,
    build_research_replay_export,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, default=Path("regime-replay-export.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.days <= 90:
        raise SystemExit("--days must be between 1 and 90.")
    estimated_rows = min(args.days * 4, MAX_CHECKPOINT_ROWS)
    if args.dry_run:
        print({
            "status": "dry_run",
            "days": args.days,
            "checkpoints": estimated_rows,
            "maximum_rows": MAX_CHECKPOINT_ROWS,
            "estimated_size_bytes": estimated_rows * ESTIMATED_BYTES_PER_CHECKPOINT,
            "production_queries_executed": 0,
        })
        return

    database_url = normalized_url(os.getenv("DATABASE_URL", ""))
    if not database_url:
        raise SystemExit("DATABASE_URL is required.")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                payload = build_research_replay_export(
                    connection, days=args.days, end_date=args.end_date
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print({
        "status": "success",
        "output": str(args.output),
        "period_days": payload["exported_final_days"],
        "checkpoints": payload["checkpoint_rows"],
        "file_size_bytes": args.output.stat().st_size,
        "generated_at": payload["generated_at"],
        "production_writes": False,
    })


if __name__ == "__main__":
    main()
