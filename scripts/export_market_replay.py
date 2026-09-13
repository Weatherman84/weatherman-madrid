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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, default=Path("market-replay-export.json"))
    args = parser.parse_args()

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
    print(
        f"Wrote {args.output} with {payload['available_checkpoint_count']}/"
        f"{payload['checkpoint_count']} available checkpoint snapshots."
    )


if __name__ == "__main__":
    main()
