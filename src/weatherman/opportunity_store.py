"""Append-only SQLite store dedicated to Opportunity Scanner shadow evidence."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS opportunity_signal (
  signal_id TEXT PRIMARY KEY, airport TEXT NOT NULL, target_date TEXT NOT NULL,
  checkpoint TEXT NOT NULL, rule_id TEXT NOT NULL, hc_class TEXT NOT NULL,
  opportunity_status TEXT NOT NULL, triggered_at TEXT NOT NULL,
  rule_set_version TEXT NOT NULL, evidence_class TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_snapshot (
  market_snapshot_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL,
  snapshot_at TEXT, market_id TEXT, payload_json TEXT NOT NULL,
  FOREIGN KEY(signal_id) REFERENCES opportunity_signal(signal_id)
);
CREATE TABLE IF NOT EXISTS shadow_trade (
  shadow_trade_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, airport TEXT NOT NULL,
  target_date TEXT NOT NULL, rule_id TEXT NOT NULL, entry_price REAL NOT NULL,
  stake_usd REAL NOT NULL CHECK(stake_usd = 1.0), payload_json TEXT NOT NULL,
  FOREIGN KEY(signal_id) REFERENCES opportunity_signal(signal_id)
);
CREATE TABLE IF NOT EXISTS shadow_resolution (
  shadow_resolution_id TEXT PRIMARY KEY, shadow_trade_id TEXT NOT NULL UNIQUE,
  resolved_at TEXT NOT NULL, win INTEGER NOT NULL, hypothetical_pnl_usd REAL NOT NULL,
  settlement_source TEXT NOT NULL, payload_json TEXT NOT NULL,
  FOREIGN KEY(shadow_trade_id) REFERENCES shadow_trade(shadow_trade_id)
);
CREATE TABLE IF NOT EXISTS revision_record (
  revision_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  recorded_at TEXT NOT NULL, code_version TEXT NOT NULL, reason TEXT NOT NULL,
  revision_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_result_observation (
  result_observation_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL,
  airport TEXT NOT NULL, target_date TEXT NOT NULL, recorded_at TEXT NOT NULL,
  evaluation_version TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_signal_date ON opportunity_signal(target_date, airport);
CREATE INDEX IF NOT EXISTS ix_trade_date ON shadow_trade(target_date, airport);
CREATE INDEX IF NOT EXISTS ix_result_opportunity
  ON opportunity_result_observation(opportunity_id, recorded_at);
"""


def _encoded(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class OpportunityStore:
    """A separate store; it never imports or connects to the Madrid database."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _append(connection: sqlite3.Connection, table: str, key: str, value: str,
                columns: Mapping[str, Any]) -> bool:
        existing = connection.execute(
            f"SELECT payload_json FROM {table} WHERE {key} = ?", (value,)
        ).fetchone()
        payload = columns["payload_json"]
        if existing:
            if existing["payload_json"] != payload:
                raise RuntimeError(f"Immutable {table} conflict for {value}")
            return False
        names = list(columns)
        marks = ",".join("?" for _ in names)
        connection.execute(
            f"INSERT INTO {table} ({','.join(names)}) VALUES ({marks})",
            tuple(columns[name] for name in names),
        )
        return True

    def append_signal(self, signal: Mapping[str, Any]) -> bool:
        if signal.get("research_only") is not True or signal.get("automatic_promotion") is not False:
            raise ValueError("Only research-only, non-promoting signals may be persisted")
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM opportunity_signal WHERE signal_id = ?", (signal["signal_id"],)
            ).fetchone()
            if existing:
                return False
            return self._append(connection, "opportunity_signal", "signal_id", signal["signal_id"], {
                "signal_id": signal["signal_id"], "airport": signal["airport"],
                "target_date": signal["target_date"], "checkpoint": signal["checkpoint"],
                "rule_id": signal["rule_id"], "hc_class": signal["hc_class"],
                "opportunity_status": signal["opportunity_status"],
                "triggered_at": signal["triggered_at"], "rule_set_version": signal["rule_set_version"],
                "evidence_class": signal["evidence_class"], "payload_json": _encoded(signal),
            })

    def import_journal(self, journal: Mapping[str, Any]) -> None:
        """Restore a previously published safe journal into an empty/partial store."""
        if not journal:
            return
        safe = (
            journal.get("classification") == "CROSS-AIRPORT RESEARCH-ONLY SHADOW JOURNAL"
            and journal.get("research_only") is True
            and journal.get("automatic_promotion") is False
            and journal.get("automatic_execution") is False
            and journal.get("writes_production_database") is False
        )
        if not safe:
            raise ValueError("Existing scanner journal failed safety validation")
        for row in journal.get("opportunity_signals", []):
            self.append_signal(row)
        for row in journal.get("market_snapshots", []):
            self.append_market_snapshot(row)
        for row in journal.get("shadow_trades", []):
            self.append_trade(row)
        for row in journal.get("shadow_resolutions", []):
            self.append_resolution(row)
        for row in journal.get("result_observations", []):
            self.append_result_observation(row)

    def append_market_snapshot(self, snapshot: Mapping[str, Any]) -> bool:
        with self.connect() as connection:
            return self._append(connection, "market_snapshot", "market_snapshot_id", snapshot["market_snapshot_id"], {
                "market_snapshot_id": snapshot["market_snapshot_id"], "signal_id": snapshot["signal_id"],
                "snapshot_at": snapshot.get("snapshot_at"), "market_id": snapshot.get("market_id"),
                "payload_json": _encoded(snapshot),
            })

    def append_trade(self, trade: Mapping[str, Any]) -> bool:
        if trade.get("real_money") is not False or float(trade.get("stake_usd", 0)) != 1.0:
            raise ValueError("Only $1 nominal, non-real-money shadow trades may be persisted")
        with self.connect() as connection:
            return self._append(connection, "shadow_trade", "shadow_trade_id", trade["shadow_trade_id"], {
                "shadow_trade_id": trade["shadow_trade_id"], "signal_id": trade["signal_id"],
                "airport": trade["airport"], "target_date": trade["target_date"],
                "rule_id": trade["rule_id"], "entry_price": trade["entry_price"],
                "stake_usd": trade["stake_usd"], "payload_json": _encoded(trade),
            })

    def append_resolution(self, resolution: Mapping[str, Any]) -> bool:
        with self.connect() as connection:
            return self._append(connection, "shadow_resolution", "shadow_resolution_id", resolution["shadow_resolution_id"], {
                "shadow_resolution_id": resolution["shadow_resolution_id"],
                "shadow_trade_id": resolution["shadow_trade_id"], "resolved_at": resolution["resolved_at"],
                "win": int(bool(resolution["win"])),
                "hypothetical_pnl_usd": resolution["hypothetical_pnl_usd"],
                "settlement_source": resolution["settlement_source"],
                "payload_json": _encoded(resolution),
            })

    def append_revision(self, *, revision_id: str, entity_type: str, entity_id: str,
                        code_version: str, reason: str, revision: Mapping[str, Any]) -> None:
        if not reason.strip():
            raise ValueError("A revision reason is required")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO revision_record VALUES (?, ?, ?, ?, ?, ?, ?)",
                (revision_id, entity_type, entity_id, datetime.now(timezone.utc).isoformat(),
                 code_version, reason, _encoded(revision)),
            )

    def append_result_observation(self, result: Mapping[str, Any]) -> bool:
        """Append one immutable result state; changed states form the audit history."""
        with self.connect() as connection:
            return self._append(
                connection,
                "opportunity_result_observation",
                "result_observation_id",
                result["result_observation_id"],
                {
                    "result_observation_id": result["result_observation_id"],
                    "opportunity_id": result["opportunity_id"],
                    "airport": result["airport"],
                    "target_date": result["target_date"],
                    "recorded_at": result["recorded_at"],
                    "evaluation_version": result["evaluation_version"],
                    "payload_json": _encoded(result),
                },
            )

    def records(self, table: str) -> list[dict[str, Any]]:
        allowed = {
            "opportunity_signal", "market_snapshot", "shadow_trade", "shadow_resolution",
            "revision_record", "opportunity_result_observation",
        }
        if table not in allowed:
            raise ValueError("Unknown scanner table")
        with self.connect() as connection:
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        return [json.loads(row["payload_json"] if "payload_json" in row.keys() else row["revision_json"]) for row in rows]

    def journal(self, *, generated_at: datetime | None = None) -> dict[str, Any]:
        generated = generated_at or datetime.now(timezone.utc)
        signals = self.records("opportunity_signal")
        markets = self.records("market_snapshot")
        trades = self.records("shadow_trade")
        resolutions = self.records("shadow_resolution")
        results = self.records("opportunity_result_observation")
        return {
            "schema_version": "opportunity_scanner_journal_v2",
            "app_version": "0.1-shadow", "rule_set_version": "frozen_opportunity_rule_set_v1",
            "classification": "CROSS-AIRPORT RESEARCH-ONLY SHADOW JOURNAL",
            "generated_at": generated.isoformat(), "research_only": True,
            "automatic_promotion": False, "automatic_execution": False,
            "writes_production_database": False, "contains_credentials": False,
            "opportunity_signals": signals, "market_snapshots": markets,
            "shadow_trades": trades, "shadow_resolutions": resolutions,
            "result_observations": results,
        }
