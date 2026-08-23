from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_collector_writes_to_neon_without_committing_a_sqlite_file() -> None:
    source = workflow("madrid-collector.yml")
    assert "run-collector --airports LEMD" in source
    assert "secrets.DATABASE_URL" in source
    assert "group: madrid-neon-writer" in source
    assert "contents: read" in source
    assert "git push" not in source
    assert "weatherman.db" not in source


def test_collector_has_hourly_safety_and_fifteen_minute_active_windows() -> None:
    source = workflow("madrid-collector.yml")
    assert 'cron: "7 0-4,21-23 * * *"' in source
    assert 'cron: "7,22,37,52 5-20 * * *"' in source
    assert 'METEOBLUE_DAILY_CALL_LIMIT: "4"' in source


def test_history_import_is_shallow_and_madrid_only() -> None:
    source = workflow("import-madrid-history.yml")
    assert "--depth 1" in source
    assert "sparse-checkout set data" in source
    assert "migrate_madrid_to_neon.py" in source
    assert "data/weatherman.db" in source


def test_replay_requires_a_separate_database_secret() -> None:
    source = workflow("prepare-replay-lab.yml")
    assert "secrets.REPLAY_DATABASE_URL" in source
    script = (ROOT / "scripts" / "prepare_replay_lab.py").read_text(encoding="utf-8")
    assert "production and replay point to the same Neon database" in script
    assert "SET TRANSACTION READ ONLY" in script


def test_replay_pilot_is_manual_read_only_and_downloadable() -> None:
    source = workflow("run-madrid-replay.yml")
    script = (ROOT / "scripts" / "run_madrid_replay_pilot.py").read_text(
        encoding="utf-8"
    )
    assert "workflow_dispatch" in source
    assert "actions/upload-artifact@v4" in source
    assert "secrets.REPLAY_DATABASE_URL" in source
    assert "SET TRANSACTION READ ONLY" in script
    assert 'transaction.rollback()' in script
    assert '"automatic_promotion": False' in script
