from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .history import (
    ARCHIVE_SPECS,
    DEFAULT_ARCHIVE_DIRECTORY,
    HistoryArchiveError,
    archive_table_rows,
    sqlite_table_report,
    validate_history_archive,
)
from .hourly_archive import ARCHIVE_COLUMNS as LEGACY_HOURLY_COLUMNS
from .hourly_archive import read_hourly_archive
from .settings import ROOT, settings


DEFAULT_RETENTION_DAYS = 3
DATABASE_WARNING_BYTES = 35 * 1024 * 1024
DATABASE_HARD_LIMIT_BYTES = 48 * 1024 * 1024
REDUNDANT_HOURLY_INDEXES = (
    "ix_hourly_forecasts_airport",
    "ix_hourly_forecasts_model",
    "ix_hourly_forecasts_run_at",
    "ix_hourly_forecasts_valid_at",
)


def configured_sqlite_path() -> Path | None:
    prefix = "sqlite:///"
    if not settings.database_url.startswith(prefix):
        return None
    path = Path(settings.database_url.removeprefix(prefix))
    return path if path.is_absolute() else ROOT / path


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    quoted = table.replace('"', '""')
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{quoted}")')]


def _copy_database(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=60)
    destination = sqlite3.connect(destination_path, timeout=60)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


def _import_legacy_hourly_archive(
    archive_directory: Path,
    legacy_directory: Path,
) -> dict[str, int]:
    if not legacy_directory.exists():
        return {"source_rows": 0, "rows_added": 0, "files_changed": 0}
    frame = read_hourly_archive(legacy_directory)
    if frame.empty:
        return {"source_rows": 0, "rows_added": 0, "files_changed": 0}
    rows = frame.where(frame.notna(), None).to_dict(orient="records")
    result = archive_table_rows(
        rows,
        spec=ARCHIVE_SPECS["hourly_forecasts"],
        columns=LEGACY_HOURLY_COLUMNS,
        directory=archive_directory,
    )
    return {key: int(result[key]) for key in ("source_rows", "rows_added", "files_changed")}


def _archive_and_prune_table(
    connection: sqlite3.Connection,
    *,
    table: str,
    retention_days: int,
    archive_directory: Path,
    reference_time: datetime,
) -> dict[str, object]:
    spec = ARCHIVE_SPECS[table]
    columns = _table_columns(connection, table)
    if not columns:
        return {
            "table": table,
            "status": "missing_table",
            "archived": 0,
            "pruned": 0,
            "cutoff": None,
        }
    required = {spec.event_time, *spec.key_columns}
    missing = sorted(required.difference(columns))
    if missing:
        return {
            "table": table,
            "status": "incompatible_schema",
            "archived": 0,
            "pruned": 0,
            "cutoff": None,
            "reason": f"Required columns are missing: {', '.join(missing)}",
        }

    quoted = table.replace('"', '""')
    event = spec.event_time.replace('"', '""')
    latest = connection.execute(
        f'SELECT MAX("{event}") FROM "{quoted}"'
    ).fetchone()[0]
    if latest is None:
        return {
            "table": table,
            "status": "empty",
            "archived": 0,
            "pruned": 0,
            "cutoff": None,
        }
    latest_at = datetime.fromisoformat(str(latest).replace("Z", "+00:00"))
    if latest_at.tzinfo is None:
        latest_at = latest_at.replace(tzinfo=timezone.utc)
    latest_day = latest_at.astimezone(timezone.utc).date()
    reference_day = max(latest_day, reference_time.astimezone(timezone.utc).date())
    first_kept_day = reference_day - timedelta(days=max(1, retention_days) - 1)
    cutoff = f"{first_kept_day.isoformat()} 00:00:00"
    selected_columns = [column for column in columns if column != "id"]
    sql_columns = ", ".join(f'"{column.replace(chr(34), chr(34) * 2)}"' for column in selected_columns)
    cursor = connection.execute(
        f'SELECT {sql_columns} FROM "{quoted}" WHERE "{event}" < ? '
        f'ORDER BY "{event}"',
        (cutoff,),
    )
    rows = [dict(zip(selected_columns, row)) for row in cursor.fetchall()]
    archive_result = archive_table_rows(
        rows,
        spec=spec,
        columns=selected_columns,
        directory=archive_directory,
    )
    if int(archive_result["source_rows"]) != len(rows):
        # Natural-key duplicates should not exist in these append-only tables.
        raise HistoryArchiveError(
            f"{table} contains duplicate natural keys; refusing to prune {len(rows)} rows."
        )
    deleted = connection.execute(
        f'DELETE FROM "{quoted}" WHERE "{event}" < ?',
        (cutoff,),
    ).rowcount
    return {
        "table": table,
        "status": "archived" if rows else "within_retention",
        "archived": len(rows),
        "archive_rows_added": int(archive_result["rows_added"]),
        "archive_files_changed": int(archive_result["files_changed"]),
        "pruned": max(int(deleted or 0), 0),
        "cutoff": cutoff,
    }


def _atomic_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def maintain_sqlite_database(
    database_path: Path | None = None,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    hourly_retention_days: int | None = None,
    archive_directory: Path | None = None,
    legacy_hourly_directory: Path | None = None,
    reference_time: datetime | None = None,
) -> dict[str, object]:
    """Safely archive every historisable table on a verified SQLite copy.

    The source database is replaced only after every partition roundtrips, the
    manifest hashes validate, SQLite passes ``integrity_check``, and the compact
    copy remains below the hard operating limit. Re-running the same migration is
    idempotent because archives merge on stable natural keys.
    """
    if hourly_retention_days is not None:
        retention_days = int(hourly_retention_days)
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")

    resolved_path = database_path or configured_sqlite_path()
    if resolved_path is None:
        return {"status": "skipped_non_sqlite"}
    path = Path(resolved_path)
    if not path.exists():
        return {"status": "skipped_missing_database", "database_bytes": 0}
    archive_path = Path(archive_directory or DEFAULT_ARCHIVE_DIRECTORY)
    maintenance_time = reference_time or datetime.now(timezone.utc)
    if maintenance_time.tzinfo is None:
        maintenance_time = maintenance_time.replace(tzinfo=timezone.utc)
    legacy_path = Path(legacy_hourly_directory or path.parent / "hourly_archive")
    before = sqlite_table_report(path)
    if (archive_path / "manifest.json").exists():
        validate_history_archive(archive_path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.stage1.", suffix=".sqlite", dir=path.parent
    )
    os.close(descriptor)
    working_path = Path(temporary_name)
    table_results: list[dict[str, object]] = []
    legacy_result: dict[str, int] = {}
    dropped_indexes = 0
    try:
        _copy_database(path, working_path)
        legacy_result = _import_legacy_hourly_archive(archive_path, legacy_path)
        connection = sqlite3.connect(working_path, timeout=60)
        try:
            connection.execute("PRAGMA busy_timeout = 60000")
            connection.execute("PRAGMA foreign_keys = ON")
            for table in ARCHIVE_SPECS:
                table_results.append(
                    _archive_and_prune_table(
                        connection,
                        table=table,
                        retention_days=(
                            ARCHIVE_SPECS[table].retention_days
                            if table in {"collection_runs", "collection_coverage"}
                            else retention_days
                        ),
                        archive_directory=archive_path,
                        reference_time=maintenance_time,
                    )
                )
            existing_indexes = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            for index_name in REDUNDANT_HOURLY_INDEXES:
                if index_name in existing_indexes:
                    connection.execute(f'DROP INDEX "{index_name}"')
                    dropped_indexes += 1
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise HistoryArchiveError(f"SQLite integrity_check failed: {integrity}")
            connection.execute("VACUUM")
            connection.execute("PRAGMA optimize")
        finally:
            connection.close()

        manifest = validate_history_archive(archive_path)
        compact_bytes = working_path.stat().st_size
        if compact_bytes >= DATABASE_HARD_LIMIT_BYTES:
            raise HistoryArchiveError(
                "Compacted SQLite copy is still above the 48-MiB operating limit: "
                f"{compact_bytes} bytes. The original database was left untouched."
            )
        os.replace(working_path, path)
        after = sqlite_table_report(path)
        result: dict[str, object] = {
            "status": "maintained",
            "retention_days": retention_days,
            "database_warning": compact_bytes >= DATABASE_WARNING_BYTES,
            "database_warning_bytes": DATABASE_WARNING_BYTES,
            "database_hard_limit_bytes": DATABASE_HARD_LIMIT_BYTES,
            "database_bytes_before": int(before["database_bytes"]),
            "database_bytes": compact_bytes,
            "database_bytes_saved": int(before["database_bytes"]) - compact_bytes,
            "hourly_forecasts_archived": sum(
                int(item.get("archived", 0))
                for item in table_results
                if item["table"] == "hourly_forecasts"
            ),
            "hourly_forecasts_pruned": sum(
                int(item.get("pruned", 0))
                for item in table_results
                if item["table"] == "hourly_forecasts"
            ),
            "archive_rows_added": sum(
                int(item.get("archive_rows_added", 0)) for item in table_results
            )
            + int(legacy_result.get("rows_added", 0)),
            "archive_total_rows": int(manifest.get("total_rows", 0)),
            "archive_files": int(manifest.get("partition_count", 0)),
            "archive_directory": str(archive_path),
            "legacy_hourly_rows_imported": int(legacy_result.get("source_rows", 0)),
            "indexes_dropped": dropped_indexes,
            "vacuumed": True,
            "tables": table_results,
            "before": before,
            "after": after,
        }
        _atomic_report(archive_path / "last-maintenance.json", result)
        return result
    finally:
        try:
            working_path.unlink()
        except FileNotFoundError:
            pass
