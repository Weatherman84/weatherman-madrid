"""Capture today's immutable v0.1 shadow decisions in the isolated replay DB."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import create_engine, text

from prepare_replay_lab import database_identity, ensure_replay_schema, normalized_url
from weatherman.market_replay_export import (
    MARKET_COLUMNS,
    _bucket_c,
    _market_rows,
    _optional_bool,
    _optional_number,
    checkpoint_schedule,
)
from weatherman.research_replay_export import (
    _actual_map,
    _champion_rows,
    _checkpoint_rows,
    _resolution_map,
)
from weatherman.research_tracks import (
    TRADING_CHALLENGER_VERSION,
    build_trading_shadow_decision,
    positive_temperature_bucket,
)


SETTLEMENT_LOOKBACK_DAYS = 14


def _market_checkpoints(connection, target, now: datetime) -> dict[str, dict]:
    schedules = [
        (target, label, checkpoint_at)
        for label, checkpoint_at in checkpoint_schedule(target)
        if checkpoint_at <= now
    ]
    frame = _market_rows(connection, schedules)
    if not frame.empty:
        frame["market_captured_at"] = pd.to_datetime(
            frame.market_captured_at, utc=True, errors="coerce"
        )
    result = {}
    for _, label, checkpoint_at in schedules:
        selected = frame[frame.export_checkpoint_label.eq(label)] if not frame.empty else frame
        if selected.empty:
            result[label] = {"status": "unavailable", "markets": []}
            continue
        captured = pd.Timestamp(selected.market_captured_at.iloc[0]).to_pydatetime()
        age = max(0.0, (checkpoint_at - captured).total_seconds() / 60)
        markets = []
        for row in selected.itertuples():
            item = {name: getattr(row, name) for name in MARKET_COLUMNS}
            markets.append({
                "bucket_c": _bucket_c(item["bucket_low_c"], item["bucket_high_c"]),
                "best_bid": _optional_number(item["best_bid"]),
                "best_ask": _optional_number(item["best_ask"]),
                "spread": _optional_number(item["spread"]),
                "yes_price": _optional_number(item["yes_price"]),
                "closed": _optional_bool(item["closed"]),
            })
        result[label] = {
            "status": "available",
            "market_captured_at": captured.isoformat(),
            "market_snapshot_age_minutes": round(age, 3),
            "markets": markets,
        }
    return result


def _capture_payloads(connection, target, generated: datetime) -> list[dict]:
    snapshots = _checkpoint_rows(connection, [target])
    champions = _champion_rows(connection, snapshots)
    if snapshots.empty or champions.empty:
        return []
    frame = snapshots.merge(champions, on=["target_date", "captured_at"], how="inner")
    markets = _market_checkpoints(connection, target, generated)
    schedule = dict(checkpoint_schedule(target))
    decisions = []
    for row in frame.to_dict("records"):
        label = str(row["checkpoint_label"])
        if schedule[label] > generated:
            continue
        decision = build_trading_shadow_decision(
            target_date=target.isoformat(),
            checkpoint=label,
            generated_at=generated.isoformat(),
            probabilities=row.get("probabilities_json"),
            taf_bucket=positive_temperature_bucket(row.get("taf_max_temp_c")),
            market_checkpoint=markets.get(label),
            forecast_confidence=row.get("forecast_confidence"),
            regimes=[],
        )
        decision["checkpoint_at"] = schedule[label].isoformat()
        decision["evidence_class"] = "sequential_live_shadow"
        decisions.append(decision)
    return decisions


def _insert_decisions(connection, decisions: list[dict]) -> int:
    inserted = 0
    statement = text(
        "INSERT INTO replay_lab.trading_shadow_decisions ("
        "target_date, checkpoint, checkpoint_at, generated_at, challenger_version, "
        "evidence_class, top1_bucket, top1_probability, top2_bucket, top2_probability, "
        "taf_bucket, taf_outside_top2, selected_buckets_json, market_snapshot_at, "
        "forecast_confidence, regimes_json, "
        "market_snapshot_status, market_snapshot_age_minutes, market_freshness_band, "
        "strategy_code, research_only, automatic_promotion) VALUES ("
        ":target_date, :checkpoint, :checkpoint_at, :generated_at, :version, :evidence, "
        ":top1, :top1p, :top2, :top2p, :taf, :taf_outside, CAST(:selected AS JSONB), "
        ":market_at, :confidence, CAST(:regimes AS JSONB), "
        ":market_status, :market_age, :freshness, :strategy, TRUE, FALSE) "
        "ON CONFLICT (target_date, checkpoint, challenger_version) DO NOTHING"
    )
    for item in decisions:
        result = connection.execute(statement, {
            "target_date": item["target_date"], "checkpoint": item["checkpoint"],
            "checkpoint_at": item["checkpoint_at"], "generated_at": item["generated_at"],
            "version": item["challenger_version"], "evidence": item["evidence_class"],
            "top1": item["champion_top1_bucket"], "top1p": item["champion_top1_probability"],
            "top2": item["champion_top2_bucket"], "top2p": item["champion_top2_probability"],
            "taf": item["taf_bucket"], "taf_outside": item["taf_outside_top2"],
            "selected": json.dumps(item["selected_buckets"], separators=(",", ":")),
            "confidence": item["forecast_confidence"],
            "regimes": json.dumps(item["regimes"], separators=(",", ":")),
            "market_at": item["market_snapshot_at"],
            "market_status": item["market_snapshot_status"],
            "market_age": item["market_snapshot_age_minutes"],
            "freshness": item["market_freshness_band"], "strategy": item["strategy_code"],
        })
        inserted += int(result.rowcount or 0)
    return inserted


def _settle(production, replay, evaluated_at: datetime) -> int:
    cutoff = evaluated_at.date() - timedelta(days=SETTLEMENT_LOOKBACK_DAYS)
    with replay.connect() as connection:
        pending = pd.read_sql(text(
            "SELECT d.id, d.target_date, d.selected_buckets_json "
            "FROM replay_lab.trading_shadow_decisions d LEFT JOIN "
            "replay_lab.trading_shadow_outcomes o ON o.decision_id=d.id "
            "WHERE o.id IS NULL AND d.target_date>=:cutoff ORDER BY d.target_date LIMIT 56"
        ), connection, params={"cutoff": cutoff})
    if pending.empty:
        return 0
    dates = sorted(pd.to_datetime(pending.target_date).dt.date.unique())
    with production.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            actuals = _actual_map(connection, dates)
            resolutions = _resolution_map(connection, dates)
        finally:
            transaction.rollback()
    settled = 0
    with replay.begin() as connection:
        for row in pending.itertuples():
            target = pd.Timestamp(row.target_date).date()
            resolution = resolutions.get(target)
            if not resolution:
                continue
            selected = row.selected_buckets_json
            if isinstance(selected, str):
                selected = json.loads(selected)
            asks = [pd.to_numeric(item.get("best_ask"), errors="coerce") for item in selected]
            pnl = None
            if selected and all(pd.notna(ask) and float(ask) > 0 for ask in asks):
                payout = sum(
                    float(item["weight"]) / float(ask)
                    for item, ask in zip(selected, asks, strict=True)
                    if item["bucket_c"] == resolution["bucket_c"]
                )
                pnl = round(payout - 1.0, 8)
            result = connection.execute(text(
                "INSERT INTO replay_lab.trading_shadow_outcomes (decision_id, evaluated_at, "
                "resolved_market_bucket, resolution_source, stored_metar_actual_c, "
                "aemet_physical_tmax_c, hypothetical_pnl_units, research_only) VALUES ("
                ":id, :at, :bucket, :source, :metar, NULL, :pnl, TRUE) "
                "ON CONFLICT (decision_id) DO NOTHING"
            ), {"id": int(row.id), "at": evaluated_at, "bucket": resolution["bucket_c"],
                "source": resolution["resolution_source"], "metar": actuals.get(target),
                "pnl": pnl})
            settled += int(result.rowcount or 0)
    return settled


def main() -> None:
    production_url = normalized_url(os.getenv("DATABASE_URL", ""))
    replay_url = normalized_url(os.getenv("REPLAY_DATABASE_URL", ""))
    if not production_url or not replay_url:
        raise SystemExit("DATABASE_URL and REPLAY_DATABASE_URL are required.")
    if database_identity(production_url) == database_identity(replay_url):
        raise SystemExit("Safety stop: Production and Replay database are identical.")
    generated = datetime.now(timezone.utc)
    target = generated.astimezone(ZoneInfo("Europe/Madrid")).date()
    production = create_engine(production_url, pool_pre_ping=True)
    replay = create_engine(replay_url, pool_pre_ping=True)
    try:
        with production.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                decisions = _capture_payloads(connection, target, generated)
            finally:
                transaction.rollback()
        with replay.begin() as connection:
            ensure_replay_schema(connection)
            inserted = _insert_decisions(connection, decisions)
        settled = _settle(production, replay, generated)
    finally:
        production.dispose()
        replay.dispose()
    print({
        "status": "success", "target_date": target.isoformat(),
        "candidate_decisions": len(decisions), "inserted_immutable_decisions": inserted,
        "settled_outcomes": settled, "challenger_version": TRADING_CHALLENGER_VERSION,
        "production_access": "read_only", "automatic_promotion": False,
    })


if __name__ == "__main__":
    main()
