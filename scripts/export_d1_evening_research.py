"""Export Track C with one bounded, rolled-back Production-Neon read."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, text

from prepare_replay_lab import normalized_url
from weatherman.d1_evening_export import (
    ESTIMATED_BYTES_PER_D1_DAY,
    MAX_D1_CHECKPOINT_ROWS,
    MAX_MODEL_RUNS_PER_MODEL,
    MAX_MODELS_PER_CHECKPOINT,
    MAX_MODEL_SOURCE_ROWS,
    MAX_TAF_SOURCE_ROWS,
    build_d1_evening_export,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, default=Path("d1-evening-replay-export.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.days <= 90:
        raise SystemExit("--days must be between 1 and 90.")
    if args.dry_run:
        checkpoint_cap = min(MAX_D1_CHECKPOINT_ROWS, args.days)
        model_cap = min(
            MAX_MODEL_SOURCE_ROWS,
            args.days * MAX_MODELS_PER_CHECKPOINT * MAX_MODEL_RUNS_PER_MODEL,
        )
        taf_cap = min(MAX_TAF_SOURCE_ROWS, args.days * 8)
        print({
            "status": "dry_run",
            "days": args.days,
            "maximum_d1_checkpoint_rows": checkpoint_cap,
            "maximum_model_source_rows": model_cap,
            "maximum_taf_source_rows": taf_cap,
            "estimated_size_bytes": args.days * ESTIMATED_BYTES_PER_D1_DAY,
            "production_queries_executed": 0,
            "automatic_backfill": False,
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
                payload = build_d1_evening_export(
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
        "d1_checkpoints": payload["d1_checkpoint_rows"],
        "evidence_counts": payload["evidence_counts"],
        "file_size_bytes": args.output.stat().st_size,
        "generated_at": payload["generated_at"],
        "production_writes": False,
        "automatic_backfill": False,
    })


if __name__ == "__main__":
    main()
