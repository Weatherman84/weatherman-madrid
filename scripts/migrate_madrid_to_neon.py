from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, func, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from weatherman.db import Base, ENGINE, init_db
from weatherman.history import ARCHIVE_SPECS


AIRPORT = "LEMD"
SKIP_TABLES = {"provider_calls"}


def convert_value(column, value):
    if value is None:
        return None
    if isinstance(column.type, DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(column.type, Date) and not isinstance(column.type, DateTime):
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    if isinstance(column.type, Boolean):
        return bool(value)
    return value


def normalize_record(table, source: dict[str, object]) -> dict[str, object]:
    return {
        column.name: convert_value(column, source[column.name])
        for column in table.columns
        if column.name in source
    }


def airport_record(record: dict[str, object]) -> bool:
    if "airport" in record:
        return str(record.get("airport") or "") == AIRPORT
    return False


def insert_rows(connection, table, rows: Iterable[dict[str, object]]) -> int:
    total = 0
    batches: dict[tuple[str, ...], list[dict[str, object]]] = {}

    def flush(columns: tuple[str, ...]) -> None:
        nonlocal total
        batch = batches.get(columns, [])
        if not batch:
            return
        if connection.dialect.name == "postgresql":
            statement = postgresql_insert(table).values(batch).on_conflict_do_nothing()
        else:
            statement = sqlite_insert(table).values(batch).on_conflict_do_nothing()
        result = connection.execute(statement)
        total += max(0, int(result.rowcount or 0))
        batch.clear()

    for row in rows:
        normalized = normalize_record(table, row)
        if normalized:
            columns = tuple(normalized)
            batch = batches.setdefault(columns, [])
            batch.append(normalized)
            if len(batch) >= 500:
                flush(columns)
    for columns in tuple(batches):
        flush(columns)
    return total


def sqlite_rows(source: Path, table_name: str) -> Iterable[dict[str, object]]:
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        available = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if table_name not in available:
            return
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')]
        if "airport" not in columns:
            return
        query = f'SELECT * FROM "{table_name}" WHERE airport = ?'
        for row in connection.execute(query, (AIRPORT,)):
            yield dict(row)
    finally:
        connection.close()


def archive_rows(directory: Path, table_name: str) -> Iterable[dict[str, object]]:
    table_directory = directory / table_name
    if not table_directory.exists():
        return
    for path in sorted(table_directory.glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                if airport_record(record):
                    yield record


def advance_postgres_sequences(connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    for table in Base.metadata.sorted_tables:
        if "id" not in table.c:
            continue
        connection.execute(
            text(
                "SELECT setval(CAST(pg_get_serial_sequence(:table_name, 'id') AS regclass), "
                "GREATEST(COALESCE((SELECT MAX(id) FROM \"" + table.name + "\"), 1), 1), "
                "COALESCE((SELECT MAX(id) FROM \"" + table.name + "\"), 0) > 0)"
            ),
            {"table_name": table.name},
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sqlite", type=Path, required=True)
    parser.add_argument("--archive-directory", type=Path)
    parser.add_argument("--allow-sqlite-target", action="store_true")
    args = parser.parse_args()
    if not args.source_sqlite.exists():
        raise SystemExit(f"Source database not found: {args.source_sqlite}")
    if ENGINE.dialect.name != "postgresql" and not args.allow_sqlite_target:
        raise SystemExit("Refusing migration: DATABASE_URL is not PostgreSQL/Neon.")

    init_db()
    report: dict[str, dict[str, int]] = {}
    with ENGINE.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name in SKIP_TABLES or "airport" not in table.c:
                continue
            archived = 0
            if args.archive_directory and table.name in ARCHIVE_SPECS:
                archived = insert_rows(
                    connection,
                    table,
                    archive_rows(args.archive_directory, table.name),
                )
            current = insert_rows(
                connection,
                table,
                sqlite_rows(args.source_sqlite, table.name),
            )
            total = int(
                connection.scalar(
                    select(func.count()).select_from(table).where(table.c.airport == AIRPORT)
                )
                or 0
            )
            report[table.name] = {
                "archive_inserted": archived,
                "current_inserted": current,
                "madrid_rows": total,
            }
        advance_postgres_sequences(connection)
    print(json.dumps({"airport": AIRPORT, "tables": report}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
