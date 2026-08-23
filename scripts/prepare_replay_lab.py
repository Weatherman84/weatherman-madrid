from __future__ import annotations

import os
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text


def normalized_url(value: str) -> str:
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value.removeprefix("postgresql://")
    return value


def database_identity(value: str) -> tuple[str | None, str]:
    parsed = urlsplit(value.replace("postgresql+psycopg://", "postgresql://", 1))
    return parsed.hostname, parsed.path.strip("/")


def ensure_replay_schema(connection) -> None:
    """Create only research-owned objects in the isolated replay database."""
    connection.execute(text("CREATE SCHEMA IF NOT EXISTS replay_lab"))
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS replay_lab.runs ("
            "id BIGSERIAL PRIMARY KEY, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "scope TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "evidence_policy TEXT NOT NULL)"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS replay_lab.results ("
            "id BIGSERIAL PRIMARY KEY, "
            "run_id BIGINT NOT NULL REFERENCES replay_lab.runs(id), "
            "airport VARCHAR(8) NOT NULL, "
            "target_date DATE NOT NULL, "
            "checkpoint TEXT NOT NULL, "
            "stage TEXT NOT NULL, "
            "evidence_class TEXT NOT NULL, "
            "forecast_c DOUBLE PRECISION, "
            "actual_c DOUBLE PRECISION, "
            "metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb)"
        )
    )


def main() -> None:
    production_url = normalized_url(os.getenv("DATABASE_URL", ""))
    replay_url = normalized_url(os.getenv("REPLAY_DATABASE_URL", ""))
    if not production_url or not replay_url:
        raise SystemExit("DATABASE_URL and REPLAY_DATABASE_URL are both required.")
    if database_identity(production_url) == database_identity(replay_url):
        raise SystemExit(
            "Safety stop: production and replay point to the same Neon database."
        )

    production = create_engine(production_url, pool_pre_ping=True)
    replay = create_engine(replay_url, pool_pre_ping=True)
    with production.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        madrid_rows = int(
            connection.scalar(
                text("SELECT COUNT(*) FROM forecasts WHERE airport = 'LEMD'")
            )
            or 0
        )
    with replay.begin() as connection:
        ensure_replay_schema(connection)
    print(
        {
            "status": "ready",
            "production_access": "read-only verification",
            "production_madrid_forecasts": madrid_rows,
            "replay_schema": "replay_lab",
            "automatic_promotion": False,
        }
    )


if __name__ == "__main__":
    main()
