"""Read-only source snapshot; append new decisions/outcomes in a separate Git store."""

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from weatherman.madrid_late_shadow_inputs import build_input, capture_due
from weatherman.madrid_late_shadow_journal import (
    append_record, build_journal, decision, outcome, records,
)
from weatherman.madrid_late_shadow_model import CHECKPOINT, MODELS, ZONE, bounds, cutoff, instant


def rows(connection, query, params):
    def clean(value):
        if isinstance(value, datetime):
            return instant(value).isoformat()
        return value
    return [{k: clean(v) for k, v in row.items()} for row in
            connection.execute(text(query), params).mappings()]


def read_sources(connection, day):
    start, end = bounds(day)
    params = {"day": date.fromisoformat(day), "start": start, "end": end, "oldest": start - timedelta(hours=48)}
    observations = rows(connection, "SELECT observed_at,temp_c,dewpoint_c,wind_kph,wind_direction,cloud_cover "
                        "FROM observations WHERE airport='LEMD' AND observed_at>=:start AND observed_at<:end "
                        "ORDER BY observed_at LIMIT 1000", params)
    forecasts = rows(connection, "SELECT id,model,run_at,model_run_at,available_at,fetched_at,max_temp_c,provenance_status "
                     "FROM forecasts WHERE airport='LEMD' AND target_date=:day AND run_at>=:oldest "
                     "ORDER BY fetched_at DESC LIMIT 1000", params)
    hourly = rows(connection, "SELECT model,run_at,valid_at,temp_c FROM hourly_forecasts "
                  "WHERE airport='LEMD' AND valid_at>=:start AND valid_at<:end AND run_at>=:oldest "
                  "ORDER BY run_at DESC,model,valid_at LIMIT 10000", params)
    bulletins = rows(connection, "SELECT id,issue_time,valid_from,valid_to,max_temp_c,max_temp_at,content_hash,"
                     "COALESCE(first_seen_at,collected_at) AS available_at FROM taf_reports "
                     "WHERE airport='LEMD' AND valid_from<:end AND valid_to>:start "
                     "ORDER BY issue_time DESC LIMIT 100", params)
    snapshots = rows(connection, "SELECT captured_at,features_json FROM forecast_snapshots "
                      "WHERE airport='LEMD' AND target_date=:day AND captured_at>=:start "
                      "ORDER BY captured_at DESC LIMIT 1", params)
    drivers = None
    if snapshots:
        r = snapshots[0]
        features = json.loads(r["features_json"])
        drivers = {"captured_at": r["captured_at"], "features": {
            k: features.get(k) for k in
            ("remaining_model_rise_c", "model_radiation_wm2", "future_radiation_max_wm2")}}
    return {"observations": observations, "forecasts": forecasts, "hourly": hourly,
            "bulletins": bulletins, "drivers": drivers}


def read_champion(connection, day):
    result = rows(connection, "SELECT s.id,s.captured_at,s.checkpoint_at,s.checkpoint_status,"
                  "s.evidence_class,v.probabilities_json FROM forecast_snapshots s JOIN forecast_variant_snapshots v "
                  "ON v.airport=s.airport AND v.target_date=s.target_date AND v.captured_at=s.captured_at "
                  "WHERE s.airport='LEMD' AND s.target_date=:day AND s.checkpoint_label=:checkpoint "
                  "AND s.checkpoint_reconstructed=FALSE AND v.variant='Champion' "
                  "ORDER BY s.captured_at,s.id LIMIT 2", {"day": date.fromisoformat(day), "checkpoint": CHECKPOINT})
    if len(result) != 1:
        return None
    r = result[0]
    if ("scheduled" not in str(r["checkpoint_status"]).lower()
            or instant(r["checkpoint_at"]) != cutoff(day)):
        return None
    r["probabilities"] = json.loads(r.pop("probabilities_json"))
    return r


def record(connection, store, *, mode, now, run_id):
    day = now.astimezone(ZONE).date().isoformat()
    existing = {r["target_date"] for r in records(store, "decisions")}
    inserted = settled = 0
    if mode == "audit" or (capture_due(now) and day not in existing):
        sources = read_sources(connection, day)
        captured = datetime.now(timezone.utc)
        if mode == "audit":
            print(json.dumps({"status": "read_only_source_audit", "target_date": day,
                              "models_present": sorted({r["model"] for r in sources["forecasts"]} & set(MODELS)),
                              "rows": {k: len(v) for k, v in sources.items() if isinstance(v, list)},
                              "writes_production_database": False, "backfill": False}))
        else:
            row = build_input(day, captured, **sources)
            item = decision(row, captured_at=captured, generated_at=datetime.now(timezone.utc), run_id=run_id)
            inserted = int(append_record(store, "decisions", item))
    # Settle measured day maxima only after the next day's 06:00 closeout.
    local = now.astimezone(ZONE)
    if mode != "audit" and 6 <= local.hour < 9:
        done = {r["target_date"] for r in records(store, "outcomes")}
        pending = [r for r in records(store, "decisions") if r["target_date"] not in done]
        for r in pending:
            if r["target_date"] >= day:
                continue
            values = rows(connection, "SELECT max_temp_c,source FROM daily_actuals "
                          "WHERE airport='LEMD' AND target_date=:day AND source='stored-metar-station' LIMIT 2",
                          {"day": date.fromisoformat(r["target_date"])})
            if len(values) != 1:
                continue
            result = outcome(r, values[0]["max_temp_c"], read_champion(connection, r["target_date"]), evaluated_at=now)
            settled += int(append_record(store, "outcomes", result))
    return inserted, settled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--mode", choices=("auto", "audit"), default="auto")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    inserted = settled = 0
    if args.mode == "audit" or capture_due(now) or 6 <= now.astimezone(ZONE).hour < 9:
        url = os.environ["DATABASE_URL"]
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        engine = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on -c statement_timeout=20000"})
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                    inserted, settled = record(connection, args.store, mode=args.mode, now=now,
                                               run_id=os.getenv("GITHUB_RUN_ID", "local"))
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()
    payload = build_journal(args.store)
    args.store.mkdir(parents=True, exist_ok=True)
    (args.store / "journal.json").write_text(json.dumps(payload, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n")
    print(json.dumps({"status": "complete", "inserted": inserted, "settled": settled,
                      "prospective_curve_days": payload["curve_evidence"]["frozen_days"],
                      "fallback_only_days": payload["fallback_only"]["frozen_days"],
                      "writes_production_database": False, "automatic_promotion": False}))


if __name__ == "__main__":
    try:
        main()
    except SQLAlchemyError:
        # SQLAlchemy exception text can include connection details; never publish it.
        raise SystemExit("Read-only database access failed; no forecast published.") from None
