"""Export compact four-checkpoint research evidence with a rolled-back read."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, text

from prepare_replay_lab import normalized_url
from weatherman.checkpoint_research_export import (
    ESTIMATED_BYTES_PER_CHECKPOINT,
    MAX_CHECKPOINT_ROWS,
    MAX_MODEL_RUNS_PER_MODEL,
    MAX_MODELS_PER_CHECKPOINT,
    MAX_MODEL_SOURCE_ROWS,
    MAX_TAF_SOURCE_ROWS,
    build_checkpoint_research_export,
)
from weatherman.market_replay_export import CHECKPOINT_LABELS


def _serialize_with_size(payload: dict) -> str:
    rendered = ""
    for _ in range(3):
        rendered = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        payload["export_log"]["output_file_size_bytes"] = len(rendered.encode("utf-8"))
    return json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, default=Path("checkpoint-research-export.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.days <= 90:
        raise SystemExit("--days must be between 1 and 90.")
    checkpoint_cap = min(MAX_CHECKPOINT_ROWS, args.days * len(CHECKPOINT_LABELS))
    model_cap = min(
        MAX_MODEL_SOURCE_ROWS,
        args.days
        * len(CHECKPOINT_LABELS)
        * MAX_MODELS_PER_CHECKPOINT
        * MAX_MODEL_RUNS_PER_MODEL,
    )
    taf_cap = min(MAX_TAF_SOURCE_ROWS, args.days * 8)
    if args.dry_run:
        print({
            "status": "dry_run",
            "days": args.days,
            "maximum_checkpoint_rows": checkpoint_cap,
            "maximum_model_source_rows": model_cap,
            "maximum_taf_source_rows": taf_cap,
            "estimated_size_bytes": checkpoint_cap * ESTIMATED_BYTES_PER_CHECKPOINT,
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
                payload = build_checkpoint_research_export(
                    connection, days=args.days, end_date=args.end_date
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
    args.output.write_text(_serialize_with_size(payload), encoding="utf-8")
    print({
        "status": "success",
        "output": str(args.output),
        "target_days": payload["exported_final_days"],
        "checkpoints": len(payload["records"]),
        "rows_by_evidence_class": payload["export_log"]["rows_by_evidence_class"],
        "rows_by_checkpoint": payload["export_log"]["rows_by_checkpoint"],
        "database_queries_executed": payload["export_log"]["database_queries_executed"],
        "file_size_bytes": args.output.stat().st_size,
        "generated_at": payload["generated_at"],
        "production_writes": False,
        "automatic_backfill": False,
    })


if __name__ == "__main__":
    main()
