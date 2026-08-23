from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

import pytest

from weatherman.hourly_archive import (
    HourlyArchiveError,
    archive_sqlite_history,
    load_hourly_history,
)
from weatherman.history import read_archived_table
from weatherman.maintenance import REDUNDANT_HOURLY_INDEXES, maintain_sqlite_database


def create_database(path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE hourly_forecasts (
            id INTEGER PRIMARY KEY,
            airport TEXT NOT NULL,
            model TEXT NOT NULL,
            run_at TEXT NOT NULL,
            valid_at TEXT NOT NULL,
            temp_c REAL NOT NULL,
            dewpoint_c REAL,
            cloud_cover REAL,
            wind_kph REAL,
            wind_direction REAL,
            radiation_wm2 REAL,
            temp_850hpa_c REAL
        )
        """
    )
    connection.execute("CREATE TABLE forecast_snapshots (id INTEGER PRIMARY KEY, marker TEXT)")
    connection.execute("INSERT INTO forecast_snapshots (marker) VALUES ('keep me')")
    connection.executemany(
        """
        INSERT INTO hourly_forecasts (
            airport, model, run_at, valid_at, temp_c, dewpoint_c, cloud_cover,
            wind_kph, wind_direction, radiation_wm2, temp_850hpa_c
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "EDDM",
                "ecmwf",
                "2026-07-27 12:00:00",
                "2026-07-27 15:00:00",
                31.0,
                12.0,
                5.0,
                8.0,
                210.0,
                700.0,
                20.0,
            ),
            (
                "EDDM",
                "ecmwf",
                "2026-07-28 12:00:00",
                "2026-07-28 15:00:00",
                32.0,
                13.0,
                10.0,
                9.0,
                220.0,
                710.0,
                21.0,
            ),
            (
                "EDDM",
                "ecmwf",
                "2026-08-03 12:00:00",
                "2026-08-03 15:00:00",
                33.0,
                14.0,
                15.0,
                10.0,
                230.0,
                720.0,
                22.0,
            ),
        ],
    )
    for index_name, column in zip(
        REDUNDANT_HOURLY_INDEXES,
        ("airport", "model", "run_at", "valid_at"),
        strict=True,
    ):
        connection.execute(f'CREATE INDEX "{index_name}" ON hourly_forecasts ({column})')
    connection.commit()
    connection.close()


def test_maintenance_archives_before_pruning_and_keeps_research_history(tmp_path) -> None:
    database = tmp_path / "weatherman.db"
    archive = tmp_path / "hourly_archive"
    create_database(database)

    result = maintain_sqlite_database(
        database,
        hourly_retention_days=7,
        archive_directory=archive,
        reference_time=datetime(2026, 8, 3),
    )

    connection = sqlite3.connect(database)
    kept_runs = [
        row[0]
        for row in connection.execute(
            "SELECT run_at FROM hourly_forecasts ORDER BY run_at"
        ).fetchall()
    ]
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    marker = connection.execute("SELECT marker FROM forecast_snapshots").fetchone()[0]
    connection.close()
    archived = read_archived_table("hourly_forecasts", directory=archive)
    connection = sqlite3.connect(database)
    live = connection.execute(
        "SELECT run_at FROM hourly_forecasts ORDER BY run_at"
    ).fetchall()
    connection.close()
    complete_days = sorted(
        [str(value)[:10] for value in archived.run_at.tolist()]
        + [str(row[0])[:10] for row in live]
    )

    assert kept_runs == ["2026-07-28 12:00:00", "2026-08-03 12:00:00"]
    assert [str(value)[:10] for value in archived.run_at.tolist()] == ["2026-07-27"]
    assert complete_days == [
        "2026-07-27",
        "2026-07-28",
        "2026-08-03",
    ]
    assert marker == "keep me"
    assert not indexes.intersection(REDUNDANT_HOURLY_INDEXES)
    assert result["hourly_forecasts_archived"] == 1
    assert result["hourly_forecasts_pruned"] == 1
    assert result["archive_rows_added"] == 1
    assert result["archive_total_rows"] == 1
    assert result["indexes_dropped"] == 4
    assert result["vacuumed"] is True
    assert (archive / "manifest.json").exists()
    assert (archive / "hourly_forecasts" / "2026-07-27.jsonl.gz").exists()


def test_recovered_archive_deduplicates_overlap_and_is_idempotent(tmp_path) -> None:
    database = tmp_path / "weatherman.db"
    archive = tmp_path / "hourly_archive"
    create_database(database)
    recovered = archive_sqlite_history(database, archive)
    before_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in archive.glob("hourly-*.csv.gz")
    }

    first = maintain_sqlite_database(
        database,
        hourly_retention_days=7,
        archive_directory=archive,
        reference_time=datetime(2026, 8, 3),
    )
    second = maintain_sqlite_database(
        database,
        hourly_retention_days=7,
        archive_directory=archive,
        reference_time=datetime(2026, 8, 3),
    )
    after_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in archive.glob("hourly-*.csv.gz")
    }
    complete = load_hourly_history(database, archive)

    assert recovered["archive_rows"] == 3
    assert first["archive_rows_added"] == 3
    assert first["hourly_forecasts_pruned"] == 1
    assert second["hourly_forecasts_pruned"] == 0
    assert second["archive_total_rows"] == 3
    assert len(complete) == 3
    assert before_hashes == after_hashes
    assert len(read_archived_table("hourly_forecasts", directory=archive)) == 3


def test_corrupt_archive_blocks_deletion(tmp_path) -> None:
    database = tmp_path / "weatherman.db"
    archive = tmp_path / "hourly_archive"
    create_database(database)
    archive.mkdir()
    (archive / "hourly-2026-07-27.csv.gz").write_bytes(b"not gzip")

    with pytest.raises(HourlyArchiveError):
        maintain_sqlite_database(
            database,
            hourly_retention_days=7,
            archive_directory=archive,
            reference_time=datetime(2026, 8, 3),
        )

    connection = sqlite3.connect(database)
    count = connection.execute("SELECT COUNT(*) FROM hourly_forecasts").fetchone()[0]
    connection.close()
    assert count == 3


def test_maintenance_skips_missing_database(tmp_path) -> None:
    result = maintain_sqlite_database(tmp_path / "missing.db")

    assert result["status"] == "skipped_missing_database"
    assert result["database_bytes"] == 0
