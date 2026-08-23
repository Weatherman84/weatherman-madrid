from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd


ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_COLUMNS = (
    "airport",
    "model",
    "run_at",
    "valid_at",
    "temp_c",
    "dewpoint_c",
    "cloud_cover",
    "wind_kph",
    "wind_direction",
    "radiation_wm2",
    "temp_850hpa_c",
)
ARCHIVE_KEY_COLUMNS = ("airport", "model", "run_at", "valid_at")
ARCHIVE_PREFIX = "hourly-"
ARCHIVE_SUFFIX = ".csv.gz"


class HourlyArchiveError(RuntimeError):
    """Raised before live rows are deleted when an archive cannot be trusted."""


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _row_key(row: Sequence[str]) -> tuple[str, str, str, str]:
    return tuple(row[index] for index in range(len(ARCHIVE_KEY_COLUMNS)))  # type: ignore[return-value]


def _archive_day(run_at: object) -> str:
    value = str(run_at)
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError as exc:
        raise HourlyArchiveError(f"Invalid hourly run_at value: {value!r}") from exc


def _archive_path(directory: Path, day: str) -> Path:
    return directory / f"{ARCHIVE_PREFIX}{day}{ARCHIVE_SUFFIX}"


def _read_rows(path: Path) -> list[tuple[str, ...]]:
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = tuple(next(reader))
            if header != ARCHIVE_COLUMNS:
                raise HourlyArchiveError(
                    f"Archive {path.name} has schema {header!r}; expected {ARCHIVE_COLUMNS!r}."
                )
            rows = [tuple(row) for row in reader]
    except (OSError, EOFError, StopIteration, csv.Error) as exc:
        raise HourlyArchiveError(f"Cannot read hourly archive {path}: {exc}") from exc
    for row in rows:
        if len(row) != len(ARCHIVE_COLUMNS):
            raise HourlyArchiveError(
                f"Archive {path.name} contains a row with {len(row)} columns."
            )
        if _archive_day(row[2]) not in path.name:
            raise HourlyArchiveError(
                f"Archive {path.name} contains a run from {_archive_day(row[2])}."
            )
    return rows


def _write_rows(path: Path, rows: Iterable[Sequence[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as zipped:
                with io.TextIOWrapper(zipped, encoding="utf-8", newline="") as text:
                    writer = csv.writer(text, lineterminator="\n")
                    writer.writerow(ARCHIVE_COLUMNS)
                    writer.writerows(rows)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _file_summary(path: Path) -> dict[str, object]:
    rows = _read_rows(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "file": path.name,
        "rows": len(rows),
        "run_date": path.name.removeprefix(ARCHIVE_PREFIX).removesuffix(ARCHIVE_SUFFIX),
        "minimum_run_at": min((row[2] for row in rows), default=None),
        "maximum_run_at": max((row[2] for row in rows), default=None),
        "sha256": digest,
        "bytes": path.stat().st_size,
    }


def rebuild_manifest(directory: Path) -> dict[str, object]:
    """Validate every immutable day file and write a deterministic manifest."""
    directory = Path(directory)
    files = [_file_summary(path) for path in sorted(directory.glob("hourly-*.csv.gz"))]
    payload: dict[str, object] = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "columns": list(ARCHIVE_COLUMNS),
        "files": files,
        "file_count": len(files),
        "total_rows": sum(int(item["rows"]) for item in files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "minimum_run_at": min(
            (str(item["minimum_run_at"]) for item in files if item["minimum_run_at"]),
            default=None,
        ),
        "maximum_run_at": max(
            (str(item["maximum_run_at"]) for item in files if item["maximum_run_at"]),
            default=None,
        ),
    }
    _atomic_json(directory / "manifest.json", payload)
    return payload


def archive_rows(
    rows: Iterable[Sequence[object]],
    directory: Path,
) -> dict[str, int]:
    """Merge rows into deterministic daily archives and deduplicate by forecast key."""
    directory = Path(directory)
    grouped: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    source_rows = 0
    for raw_row in rows:
        if len(raw_row) != len(ARCHIVE_COLUMNS):
            raise HourlyArchiveError(
                f"Hourly source row has {len(raw_row)} columns; expected {len(ARCHIVE_COLUMNS)}."
            )
        row = tuple(_text(value) for value in raw_row)
        grouped[_archive_day(row[2])].append(row)
        source_rows += 1

    rows_added = 0
    files_changed = 0
    for day, new_rows in sorted(grouped.items()):
        path = _archive_path(directory, day)
        existing = _read_rows(path) if path.exists() else []
        by_key = {_row_key(row): row for row in existing}
        previous_count = len(by_key)
        for row in new_rows:
            by_key[_row_key(row)] = row
        merged = sorted(by_key.values(), key=_row_key)
        rows_added += max(0, len(merged) - previous_count)
        if merged != existing:
            _write_rows(path, merged)
            files_changed += 1
            if _read_rows(path) != merged:
                raise HourlyArchiveError(f"Round-trip verification failed for {path}.")

    manifest = rebuild_manifest(directory) if grouped or directory.exists() else {}
    return {
        "source_rows": source_rows,
        "rows_added": rows_added,
        "files_changed": files_changed,
        "archive_rows": int(manifest.get("total_rows", 0)),
        "archive_files": int(manifest.get("file_count", 0)),
    }


def archive_sqlite_history(
    database_path: Path,
    directory: Path,
    *,
    before: str | datetime | None = None,
) -> dict[str, int]:
    """Export SQLite hourly paths without modifying the source database."""
    path = Path(database_path)
    if not path.exists():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        query = f"SELECT {', '.join(ARCHIVE_COLUMNS)} FROM hourly_forecasts"
        parameters: tuple[object, ...] = ()
        if before is not None:
            query += " WHERE run_at < ?"
            parameters = (_text(before),)
        query += " ORDER BY airport, model, run_at, valid_at"
        return archive_rows(connection.execute(query, parameters), directory)
    finally:
        connection.close()


def _archive_file_date(path: Path) -> date:
    value = path.name.removeprefix(ARCHIVE_PREFIX).removesuffix(ARCHIVE_SUFFIX)
    return date.fromisoformat(value)


def read_hourly_archive(
    directory: Path,
    *,
    airports: Iterable[str] | None = None,
    run_start: date | datetime | str | None = None,
    run_end: date | datetime | str | None = None,
) -> pd.DataFrame:
    """Read archived paths with optional run-date and airport pruning."""
    directory = Path(directory)
    start_day = date.fromisoformat(str(run_start)[:10]) if run_start is not None else None
    end_day = date.fromisoformat(str(run_end)[:10]) if run_end is not None else None
    requested_airports = {str(value) for value in airports or ()}
    frames: list[pd.DataFrame] = []
    for path in sorted(directory.glob("hourly-*.csv.gz")):
        archive_day = _archive_file_date(path)
        if start_day is not None and archive_day < start_day:
            continue
        if end_day is not None and archive_day > end_day:
            continue
        rows = _read_rows(path)
        if rows:
            frames.append(pd.DataFrame(rows, columns=ARCHIVE_COLUMNS))
    if not frames:
        return pd.DataFrame(columns=ARCHIVE_COLUMNS)
    frame = pd.concat(frames, ignore_index=True)
    if requested_airports:
        frame = frame[frame.airport.isin(requested_airports)]
    return _normalise_frame(frame)


def _normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=ARCHIVE_COLUMNS)
    result = frame[list(ARCHIVE_COLUMNS)].copy()
    for column in ("run_at", "valid_at"):
        result[column] = pd.to_datetime(result[column], utc=True, errors="coerce")
    for column in ARCHIVE_COLUMNS[4:]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["airport", "model", "run_at", "valid_at", "temp_c"])
    return result.sort_values(list(ARCHIVE_KEY_COLUMNS)).drop_duplicates(
        list(ARCHIVE_KEY_COLUMNS),
        keep="last",
    )


def load_hourly_history(
    database_path: Path,
    directory: Path,
    *,
    airports: Iterable[str] | None = None,
    run_start: date | datetime | str | None = None,
    run_end: date | datetime | str | None = None,
) -> pd.DataFrame:
    """Return one deduplicated history across the compact live DB and archives."""
    requested_airports = tuple(str(value) for value in airports or ())
    archived = read_hourly_archive(
        directory,
        airports=requested_airports,
        run_start=run_start,
        run_end=run_end,
    )
    path = Path(database_path)
    live = pd.DataFrame(columns=ARCHIVE_COLUMNS)
    if path.exists():
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conditions: list[str] = []
            parameters: list[object] = []
            if requested_airports:
                placeholders = ",".join("?" for _ in requested_airports)
                conditions.append(f"airport IN ({placeholders})")
                parameters.extend(requested_airports)
            if run_start is not None:
                conditions.append("run_at >= ?")
                parameters.append(str(run_start))
            if run_end is not None:
                conditions.append("run_at < ?")
                parameters.append(str(run_end))
            query = f"SELECT {', '.join(ARCHIVE_COLUMNS)} FROM hourly_forecasts"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            live = pd.read_sql_query(query, connection, params=parameters)
        finally:
            connection.close()
    combined = pd.concat([archived, _normalise_frame(live)], ignore_index=True)
    return _normalise_frame(combined)
