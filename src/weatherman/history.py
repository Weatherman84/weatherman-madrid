from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd
from sqlalchemy import Date, DateTime, select

from .settings import ROOT, settings


ARCHIVE_SCHEMA_VERSION = 2
DEFAULT_ARCHIVE_DIRECTORY = ROOT / "data" / "history_archive"


class HistoryArchiveError(RuntimeError):
    """Raised before live rows are removed when history cannot be verified."""


@dataclass(frozen=True)
class ArchiveSpec:
    table: str
    event_time: str
    key_columns: tuple[str, ...]
    retention_days: int = 3


ARCHIVE_SPECS: dict[str, ArchiveSpec] = {
    spec.table: spec
    for spec in (
        ArchiveSpec(
            "hourly_forecasts",
            "run_at",
            ("airport", "model", "run_at", "valid_at"),
        ),
        ArchiveSpec(
            "forecasts",
            "run_at",
            ("airport", "model", "run_at", "target_date"),
        ),
        ArchiveSpec(
            "market_snapshots",
            "captured_at",
            ("market_id", "captured_at"),
        ),
        ArchiveSpec(
            "forecast_snapshots",
            "captured_at",
            ("airport", "target_date", "captured_at"),
        ),
        ArchiveSpec(
            "forecast_variant_snapshots",
            "captured_at",
            ("airport", "target_date", "captured_at", "variant"),
        ),
        ArchiveSpec(
            "regime_memory_snapshots",
            "captured_at",
            ("airport", "target_date", "captured_at"),
        ),
        ArchiveSpec(
            "signal_snapshots",
            "captured_at",
            ("market_id", "captured_at"),
        ),
        ArchiveSpec(
            "strategy_snapshots",
            "captured_at",
            ("airport", "target_date", "captured_at", "timing", "strategy"),
        ),
        ArchiveSpec(
            "shadow_evaluations",
            "captured_at",
            ("market_id", "captured_at"),
        ),
        ArchiveSpec(
            "basket_snapshots",
            "captured_at",
            ("airport", "target_date", "captured_at", "strategy"),
        ),
        ArchiveSpec(
            "observations",
            "observed_at",
            ("airport", "observed_at"),
        ),
        ArchiveSpec(
            "taf_reports",
            "issue_time",
            ("airport", "content_hash"),
        ),
        ArchiveSpec(
            "collection_runs",
            "scheduled_at",
            ("run_id",),
            retention_days=14,
        ),
        ArchiveSpec(
            "collection_coverage",
            "scheduled_at",
            ("run_id", "airport", "data_type"),
            retention_days=14,
        ),
    )
}


def _utc_text(value: object) -> str:
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        raise HistoryArchiveError(f"Invalid archive timestamp: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize("UTC")
    else:
        parsed = parsed.tz_convert("UTC")
    return parsed.isoformat()


def _partition_day(value: object) -> str:
    return _utc_text(value)[:10]


def _json_value(value: object) -> object:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "item"):
        value = value.item()
    return value


def _canonical_record(row: Mapping[str, object], columns: Sequence[str]) -> dict[str, object]:
    return {column: _json_value(row.get(column)) for column in columns}


def _key(record: Mapping[str, object], key_columns: Sequence[str]) -> tuple[str, ...]:
    values = tuple("" if record.get(column) is None else str(record[column]) for column in key_columns)
    if any(value == "" for value in values):
        raise HistoryArchiveError(
            f"Archive key {tuple(key_columns)!r} contains an empty value: {record!r}"
        )
    return values


def _archive_path(directory: Path, spec: ArchiveSpec, day: str) -> Path:
    return directory / spec.table / f"{day}.jsonl.gz"


def _atomic_bytes(path: Path, payload_writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            payload_writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _write_records(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    def write(handle) -> None:
        with gzip.GzipFile(fileobj=handle, mode="wb", filename="", mtime=0) as zipped:
            for record in records:
                line = json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                zipped.write(line + b"\n")

    _atomic_bytes(path, write)


def _read_records(path: Path) -> list[dict[str, object]]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
    except (OSError, EOFError, json.JSONDecodeError) as exc:
        raise HistoryArchiveError(f"Cannot read archive {path}: {exc}") from exc
    if not all(isinstance(record, dict) for record in records):
        raise HistoryArchiveError(f"Archive {path} contains a non-object row.")
    return records


def _archive_filter_values(value: object) -> set[object]:
    values = value if isinstance(value, (set, tuple, list, frozenset)) else (value,)
    return {_json_value(item) for item in values}


def _record_matches_filters(
    record: Mapping[str, object],
    filters: Mapping[str, object],
) -> bool:
    """Apply equality filters before archive rows enter an in-memory frame.

    Archive partitions intentionally contain every airport.  Filtering only after
    building a DataFrame caused a selected-airport Streamlit rerun to materialise
    the complete archive repeatedly.  JSON values are canonicalised on write, so
    equality and membership filters can safely be evaluated while streaming.
    """
    for column, value in filters.items():
        if column not in record:
            return False
        if record.get(column) not in _archive_filter_values(value):
            return False
    return True


def _read_filtered_records(
    path: Path,
    *,
    filters: Mapping[str, object] | None = None,
    minimums: Mapping[str, object] | None = None,
    maximums: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    def ordered(value: object, boundary: object) -> tuple[object, object]:
        if isinstance(boundary, datetime):
            left = pd.Timestamp(value)
            right = pd.Timestamp(boundary)
            if left.tzinfo is None:
                left = left.tz_localize("UTC")
            else:
                left = left.tz_convert("UTC")
            if right.tzinfo is None:
                right = right.tz_localize("UTC")
            else:
                right = right.tz_convert("UTC")
            return left, right
        if isinstance(boundary, date):
            return pd.Timestamp(value).date(), boundary
        return value, boundary

    def within_bounds(record: Mapping[str, object]) -> bool:
        for column, boundary in (minimums or {}).items():
            value = record.get(column)
            if value is None:
                return False
            left, right = ordered(value, boundary)
            if left < right:
                return False
        for column, boundary in (maximums or {}).items():
            value = record.get(column)
            if value is None:
                return False
            left, right = ordered(value, boundary)
            if left > right:
                return False
        return True

    records: list[dict[str, object]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise HistoryArchiveError(
                        f"Archive {path} contains a non-object row."
                    )
                if filters and not _record_matches_filters(record, filters):
                    continue
                if not within_bounds(record):
                    continue
                records.append(record)
    except HistoryArchiveError:
        raise
    except (OSError, EOFError, json.JSONDecodeError) as exc:
        raise HistoryArchiveError(f"Cannot read archive {path}: {exc}") from exc
    return records


def _manifest_path(directory: Path) -> Path:
    return directory / "manifest.json"


def _read_manifest(directory: Path) -> dict[str, object]:
    path = _manifest_path(directory)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoryArchiveError(f"Cannot read history manifest: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _scan_history_archive(
    directory: Path,
    previous: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return verified partition metadata without changing the trust anchor."""
    directory = Path(directory)
    previous_entries = {
        str(item.get("file")): item
        for item in (previous or {}).get("partitions", [])
        if isinstance(item, dict)
    }
    partitions: list[dict[str, object]] = []
    now = datetime.now(timezone.utc).isoformat()
    for table, spec in sorted(ARCHIVE_SPECS.items()):
        for path in sorted((directory / table).glob("*.jsonl.gz")):
            records = _read_records(path)
            for record in records:
                if spec.event_time not in record:
                    raise HistoryArchiveError(
                        f"Archive {path} has no {spec.event_time!r} column."
                    )
                if _partition_day(record[spec.event_time]) != path.name[:10]:
                    raise HistoryArchiveError(
                        f"Archive {path} contains an event outside its UTC day."
                    )
                _key(record, spec.key_columns)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            relative = path.relative_to(directory).as_posix()
            old = previous_entries.get(relative, {})
            entry = {
                "table": table,
                "file": relative,
                "utc_date": path.name[:10],
                "rows": len(records),
                "minimum_event_time": min(
                    (str(record[spec.event_time]) for record in records), default=None
                ),
                "maximum_event_time": max(
                    (str(record[spec.event_time]) for record in records), default=None
                ),
                "schema_version": ARCHIVE_SCHEMA_VERSION,
                "columns": sorted({key for record in records for key in record}),
                "sha256": digest,
                "bytes": path.stat().st_size,
                "created_at": (
                    old.get("created_at") if old.get("sha256") == digest else now
                ),
            }
            partitions.append(entry)
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "generated_at": now,
        "partition_count": len(partitions),
        "total_rows": sum(int(item["rows"]) for item in partitions),
        "total_bytes": sum(int(item["bytes"]) for item in partitions),
        "partitions": partitions,
    }


def _publish_history_manifest(directory: Path, payload: Mapping[str, object]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_bytes(_manifest_path(directory), lambda handle: handle.write(encoded))


def rebuild_history_manifest(directory: Path) -> dict[str, object]:
    """Validate every partition and atomically publish the archive manifest."""
    directory = Path(directory)
    payload = _scan_history_archive(directory, _read_manifest(directory))
    _publish_history_manifest(directory, payload)
    return payload


def validate_history_archive(directory: Path) -> dict[str, object]:
    """Re-read every partition without blessing a mismatch before raising."""
    directory = Path(directory)
    existing = _read_manifest(directory)
    scanned = _scan_history_archive(directory, existing)
    if not existing:
        _publish_history_manifest(directory, scanned)
        return scanned
    old_by_file = {
        str(item.get("file")): item
        for item in existing.get("partitions", [])
        if isinstance(item, dict)
    }
    scanned_by_file = {
        str(item.get("file")): item
        for item in scanned.get("partitions", [])
        if isinstance(item, dict)
    }
    if old_by_file.keys() != scanned_by_file.keys():
        missing = sorted(old_by_file.keys() - scanned_by_file.keys())
        unexpected = sorted(scanned_by_file.keys() - old_by_file.keys())
        raise HistoryArchiveError(
            f"Archive file set changed unexpectedly; missing={missing}, unexpected={unexpected}"
        )
    checked_fields = (
        "sha256",
        "rows",
        "minimum_event_time",
        "maximum_event_time",
        "schema_version",
        "columns",
        "bytes",
    )
    for item in scanned.get("partitions", []):
        assert isinstance(item, dict)
        old = old_by_file.get(str(item["file"]))
        assert old is not None
        if any(old.get(field) != item.get(field) for field in checked_fields):
            raise HistoryArchiveError(
                f"Archive manifest mismatch for verified partition: {item['file']}"
            )
    _publish_history_manifest(directory, scanned)
    return scanned


def archive_table_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    spec: ArchiveSpec,
    columns: Sequence[str],
    directory: Path,
) -> dict[str, int]:
    """Merge source rows into deterministic UTC-day files and verify roundtrips."""
    grouped: dict[str, list[dict[str, object]]] = {}
    source_keys: set[tuple[str, ...]] = set()
    for raw in rows:
        record = _canonical_record(raw, columns)
        timestamp_columns = {
            spec.event_time,
            *(column for column in spec.key_columns if column.endswith("_at")),
        }
        for column in timestamp_columns:
            if record.get(column) is not None:
                record[column] = _utc_text(record[column])
        day = _partition_day(record[spec.event_time])
        grouped.setdefault(day, []).append(record)
        source_keys.add(_key(record, spec.key_columns))

    rows_added = 0
    changed = 0
    verified_keys: set[tuple[str, ...]] = set()
    for day, new_records in sorted(grouped.items()):
        path = _archive_path(Path(directory), spec, day)
        existing = _read_records(path) if path.exists() else []
        by_key = {_key(record, spec.key_columns): record for record in existing}
        old_count = len(by_key)
        for record in new_records:
            by_key[_key(record, spec.key_columns)] = record
        merged = [by_key[key] for key in sorted(by_key)]
        if merged != existing:
            _write_records(path, merged)
            changed += 1
        roundtrip = _read_records(path)
        if roundtrip != merged:
            raise HistoryArchiveError(f"Roundtrip verification failed for {path}.")
        verified_keys.update(_key(record, spec.key_columns) for record in roundtrip)
        rows_added += max(0, len(merged) - old_count)
    if not source_keys.issubset(verified_keys):
        raise HistoryArchiveError(
            f"Archive verification did not preserve every source key for {spec.table}."
        )
    manifest = rebuild_history_manifest(Path(directory)) if grouped else _read_manifest(Path(directory))
    return {
        "source_rows": len(source_keys),
        "rows_added": rows_added,
        "files_changed": changed,
        "archive_rows": int(manifest.get("total_rows", 0)),
    }


def _normalise_model_frame(model: type, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.copy()
    for column in model.__table__.columns:
        if column.name not in result:
            continue
        if isinstance(column.type, DateTime):
            result[column.name] = pd.to_datetime(result[column.name], utc=True, errors="coerce")
        elif isinstance(column.type, Date):
            result[column.name] = pd.to_datetime(
                result[column.name], errors="coerce"
            ).dt.date
    return result


def read_archived_table(
    table: str,
    *,
    directory: Path = DEFAULT_ARCHIVE_DIRECTORY,
    start: object | None = None,
    end: object | None = None,
    filters: Mapping[str, object] | None = None,
    minimums: Mapping[str, object] | None = None,
    maximums: Mapping[str, object] | None = None,
) -> pd.DataFrame:
    spec = ARCHIVE_SPECS.get(table)
    if spec is None:
        return pd.DataFrame()
    start_day = _partition_day(start) if start is not None else None
    end_day = _partition_day(end) if end is not None else None
    records: list[dict[str, object]] = []
    for path in sorted((Path(directory) / table).glob("*.jsonl.gz")):
        day = path.name[:10]
        if start_day is not None and day < start_day:
            continue
        if end_day is not None and day > end_day:
            continue
        records.extend(
            _read_filtered_records(
                path,
                filters=filters,
                minimums=minimums,
                maximums=maximums,
            )
        )
    return pd.DataFrame(records)


def read_archive_live(
    model: type,
    bind,
    *,
    filters: Mapping[str, object] | None = None,
    minimums: Mapping[str, object] | None = None,
    maximums: Mapping[str, object] | None = None,
    as_of_column: str | None = None,
    as_of: object | None = None,
    directory: Path | None = None,
) -> pd.DataFrame:
    """Return one deduplicated frame across immutable archives and the live store."""
    statement = select(model)
    for column, value in (filters or {}).items():
        attribute = getattr(model, column)
        if isinstance(value, (set, tuple, list, frozenset)):
            statement = statement.where(attribute.in_(tuple(value)))
        else:
            statement = statement.where(attribute == value)
    for column, value in (minimums or {}).items():
        statement = statement.where(getattr(model, column) >= value)
    for column, value in (maximums or {}).items():
        statement = statement.where(getattr(model, column) <= value)
    if as_of_column is not None and as_of is not None:
        statement = statement.where(getattr(model, as_of_column) <= as_of)
    live = pd.read_sql(statement, bind)

    spec = ARCHIVE_SPECS.get(str(model.__tablename__))
    if spec is None:
        return _normalise_model_frame(model, live)
    if directory is None:
        configured_database = None
        if settings.database_url.startswith("sqlite:///"):
            configured_database = (
                ROOT / settings.database_url.removeprefix("sqlite:///")
            ).resolve()
        engine = getattr(bind, "engine", bind)
        database_name = getattr(getattr(engine, "url", None), "database", None)
        bound_database = (
            Path(database_name).resolve()
            if database_name not in {None, "", ":memory:"}
            else None
        )
        # A temporary or in-memory database is an independent store and must
        # never be silently combined with the production archive. Tests and
        # migration tools can still pass an explicit archive directory.
        if configured_database is None or bound_database != configured_database:
            return _normalise_model_frame(model, live)
        directory = DEFAULT_ARCHIVE_DIRECTORY
    archive_start = (minimums or {}).get(spec.event_time)
    archive_end = (maximums or {}).get(spec.event_time)
    if as_of_column == spec.event_time and as_of is not None:
        archive_end = min(archive_end, as_of) if archive_end is not None else as_of
    archive_maximums = dict(maximums or {})
    if as_of_column is not None and as_of is not None:
        existing_maximum = archive_maximums.get(as_of_column)
        archive_maximums[as_of_column] = (
            min(existing_maximum, as_of)
            if existing_maximum is not None
            else as_of
        )
    archived = read_archived_table(
        spec.table,
        directory=directory,
        start=archive_start,
        end=archive_end,
        filters=filters,
        minimums=minimums,
        maximums=archive_maximums,
    )
    archived = _normalise_model_frame(model, archived)
    if not archived.empty:
        for column, value in (filters or {}).items():
            if column not in archived:
                archived = archived.iloc[0:0]
                break
            if isinstance(value, (set, tuple, list, frozenset)):
                archived = archived[archived[column].isin(tuple(value))]
            else:
                archived = archived[archived[column] == value]
        for column, value in (minimums or {}).items():
            if column in archived:
                archived = archived[archived[column] >= value]
        for column, value in (maximums or {}).items():
            if column in archived:
                archived = archived[archived[column] <= value]
        if as_of_column is not None and as_of is not None and as_of_column in archived:
            archived = archived[archived[as_of_column] <= as_of]
    live = _normalise_model_frame(model, live)
    if archived.empty:
        return live
    if live.empty:
        combined = archived
    else:
        combined = pd.concat([archived, live], ignore_index=True, sort=False)
    available_keys = [column for column in spec.key_columns if column in combined]
    if available_keys:
        combined = combined.drop_duplicates(available_keys, keep="last")
    if spec.event_time in combined:
        combined = combined.sort_values(spec.event_time)
    return combined.reset_index(drop=True)


def sqlite_table_report(database_path: Path) -> dict[str, object]:
    """Return row counts and best-effort on-disk bytes for migration evidence."""
    path = Path(database_path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        sizes: dict[str, int] = {}
        try:
            sizes = {
                str(name): int(size or 0)
                for name, size in connection.execute(
                    "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
                )
            }
        except sqlite3.DatabaseError:
            pass
        index_catalog = [
            (str(name), str(table))
            for name, table in connection.execute(
                "SELECT name, tbl_name FROM sqlite_master "
                "WHERE type='index' ORDER BY tbl_name, name"
            )
        ]
        index_rows = [
            {"index": name, "table": table, "bytes": sizes.get(name, 0)}
            for name, table in index_catalog
        ]
        rows = []
        for table in sorted(tables):
            quoted = table.replace('"', '""')
            count = int(connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
            data_bytes = sizes.get(table, 0)
            index_bytes = sum(
                int(item["bytes"])
                for item in index_rows
                if item["table"] == table
            )
            rows.append(
                {
                    "table": table,
                    "rows": count,
                    "data_bytes": data_bytes,
                    "index_bytes": index_bytes,
                    "bytes": data_bytes + index_bytes,
                }
            )
        return {
            "database": str(path),
            "database_bytes": path.stat().st_size,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tables": rows,
            "indexes": index_rows,
        }
    finally:
        connection.close()
