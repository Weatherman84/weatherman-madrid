"""Create the compact Madrid Polymarket Replay v2 export from read-only Neon."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, text

from prepare_replay_lab import normalized_url
from weatherman.market_replay_export import build_market_replay_export
from weatherman.market_replay_export import (
    ESTIMATED_BUCKETS_PER_CHECKPOINT,
    ESTIMATED_BYTES_PER_MARKET_ROW,
    MAX_EXPORT_DAYS,
    MAX_EXPORT_ROWS,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, default=Path("market-replay-export.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.days <= MAX_EXPORT_DAYS:
        raise SystemExit(f"--days must be between 1 and {MAX_EXPORT_DAYS}.")

    estimated_rows = min(args.days * 4 * ESTIMATED_BUCKETS_PER_CHECKPOINT, MAX_EXPORT_ROWS)
    estimated_bytes = estimated_rows * ESTIMATED_BYTES_PER_MARKET_ROW
    if args.dry_run:
        print({
            "status": "dry_run",
            "days": args.days,
            "checkpoints": args.days * 4,
            "maximum_rows": MAX_EXPORT_ROWS,
            "estimated_rows": estimated_rows,
            "estimated_size_bytes": estimated_bytes,
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
                payload = build_market_replay_export(
                    connection,
                    days=args.days,
                    end_date=args.end_date,
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()

    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    file_size = args.output.stat().st_size
    print(
        {
            "status": "success",
            "output": str(args.output),
            "period_days": payload["exported_final_days"],
            "checkpoints": payload["checkpoint_count"],
            "rows": payload["exported_market_rows"],
            "file_size_bytes": file_size,
            "generated_at": payload["generated_at"],
            "production_writes": False,
        }
    )


if __name__ == "__main__":
    main()
