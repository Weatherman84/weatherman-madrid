from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session as OrmSession

from prepare_replay_lab import database_identity, ensure_replay_schema, normalized_url
from weatherman.actual_quality import settlement_grade_actuals
from weatherman.db import DailyActual, ENGINE
from weatherman.service import (
    _build_nowcast_from_session,
    _checkpoint_provenance,
    _research_checkpoint_schedule,
)
from weatherman.settings import trading_airports


AIRPORT = "LEMD"
FORMULA_BASELINE = "v10.7.11"


def replay_evidence(metadata: dict[str, object], cutoff: datetime) -> str:
    """Separate true stored snapshots from later causal reconstruction."""
    if metadata.get("checkpoint_status") == "unavailable":
        return "unavailable"
    try:
        provenance = json.loads(str(metadata.get("source_provenance_json") or "[]"))
    except json.JSONDecodeError:
        return "unavailable"
    relevant = [row for row in provenance if row.get("relevant_to_checkpoint")]
    if len(relevant) < 2:
        return "unavailable"
    cutoff_at = pd.Timestamp(cutoff).tz_convert("UTC")
    fetched = pd.to_datetime(
        [row.get("fetched_at") for row in relevant],
        utc=True,
        errors="coerce",
    )
    if bool(fetched.notna().all()) and bool((fetched <= cutoff_at).all()):
        return "historical-causal"
    return "reconstructed-research"


def metric_payload(
    *,
    metadata: dict[str, object],
    evidence: str,
    checkpoint_at: datetime,
    forecast: float | None,
    actual: float,
    blocked_reason: str | None,
) -> dict[str, object]:
    error = float(forecast) - actual if forecast is not None else None
    return {
        "formula_baseline": FORMULA_BASELINE,
        "checkpoint_at": checkpoint_at.isoformat(),
        "evidence_class": evidence,
        "freshness_status": metadata.get("freshness_status"),
        "expected_model_count": metadata.get("expected_model_count"),
        "available_model_count": metadata.get("available_model_count"),
        "fresh_model_count": metadata.get("fresh_model_count"),
        "source_age_max_minutes": metadata.get("source_age_max_minutes"),
        "blocked_reason": blocked_reason,
        "error_c": error,
        "absolute_error_c": abs(error) if error is not None else None,
        "exact_bucket": abs(error) < 0.5 if error is not None else None,
        "within_1c": abs(error) <= 1.0 if error is not None else None,
        "automatic_promotion": False,
    }


def report_summary(rows: list[dict[str, object]], run_id: int) -> dict[str, object]:
    frame = pd.DataFrame(rows)
    summaries: list[dict[str, object]] = []
    if not frame.empty:
        usable = frame[
            frame.evidence_class.isin(
                ["historical-causal", "reconstructed-research"]
            )
            & frame.forecast_c.notna()
        ].copy()
        usable["error_c"] = usable.forecast_c.astype(float) - usable.actual_c.astype(float)
        for (checkpoint, stage, evidence), group in usable.groupby(
            ["checkpoint", "stage", "evidence_class"], sort=False
        ):
            errors = group.error_c.astype(float)
            summaries.append(
                {
                    "checkpoint": checkpoint,
                    "stage": stage,
                    "evidence_class": evidence,
                    "n": int(len(errors)),
                    "bias_c": round(float(errors.mean()), 3),
                    "mae_c": round(float(errors.abs().mean()), 3),
                    "exact_bucket": round(float((errors.abs() < 0.5).mean()), 4),
                    "within_1c": round(float((errors.abs() <= 1.0).mean()), 4),
                }
            )
    evidence_counts = (
        frame.evidence_class.value_counts().astype(int).to_dict()
        if not frame.empty
        else {}
    )
    return {
        "run_id": run_id,
        "airport": AIRPORT,
        "formula_baseline": FORMULA_BASELINE,
        "research_only": True,
        "automatic_promotion": False,
        "evidence_counts": evidence_counts,
        "summaries": summaries,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--report", type=Path, default=Path("madrid-replay-report.json"))
    args = parser.parse_args()
    requested_days = max(1, min(90, int(args.days)))

    production_url = normalized_url(os.getenv("DATABASE_URL", ""))
    replay_url = normalized_url(os.getenv("REPLAY_DATABASE_URL", ""))
    if not production_url or not replay_url:
        raise SystemExit("DATABASE_URL and REPLAY_DATABASE_URL are both required.")
    if database_identity(production_url) == database_identity(replay_url):
        raise SystemExit("Safety stop: production and replay use the same database.")

    replay_engine = create_engine(replay_url, pool_pre_ping=True)
    scope = f"Madrid {requested_days}-day fixed-checkpoint pilot"
    with replay_engine.begin() as connection:
        ensure_replay_schema(connection)
        run_id = int(
            connection.scalar(
                text(
                    "INSERT INTO replay_lab.runs (scope, status, evidence_policy) "
                    "VALUES (:scope, 'running', :policy) RETURNING id"
                ),
                {
                    "scope": scope,
                    "policy": (
                        "historical-causal, reconstructed-research and unavailable "
                        "remain separate; no production writes or promotion"
                    ),
                },
            )
        )

    airport = trading_airports()[AIRPORT]
    result_rows: list[dict[str, object]] = []
    try:
        with ENGINE.connect() as production_connection:
            transaction = production_connection.begin()
            try:
                production_connection.execute(text("SET TRANSACTION READ ONLY"))
                with OrmSession(bind=production_connection) as production_session:
                    actual_frame = pd.read_sql(
                        select(DailyActual).where(DailyActual.airport == AIRPORT),
                        production_connection,
                    )
                    actual_frame = settlement_grade_actuals(actual_frame)
                    actual_frame["target_date"] = pd.to_datetime(
                        actual_frame.target_date, errors="coerce"
                    ).dt.date
                    actual_frame = actual_frame.sort_values("target_date")
                    if args.end_date is not None:
                        actual_frame = actual_frame[
                            actual_frame.target_date <= args.end_date
                        ]
                    actual_frame = actual_frame.drop_duplicates(
                        "target_date", keep="last"
                    ).tail(requested_days)
                    if actual_frame.empty:
                        raise RuntimeError("No final Madrid station Actuals are available.")

                    for actual_row in actual_frame.itertuples():
                        target = actual_row.target_date
                        actual = float(actual_row.max_temp_c)
                        for checkpoint_label, checkpoint_at in _research_checkpoint_schedule(
                            target, airport
                        ):
                            cutoff = checkpoint_at.astimezone(timezone.utc)
                            metadata = _checkpoint_provenance(
                                production_session,
                                code=AIRPORT,
                                target=target,
                                checkpoint_at=cutoff,
                                current_time=datetime.now(timezone.utc),
                                label=checkpoint_label,
                                expected_models=[*airport.get("models", []), "meteoblue"],
                            )
                            evidence = replay_evidence(metadata, cutoff)
                            nowcast = _build_nowcast_from_session(
                                production_session,
                                AIRPORT,
                                airport,
                                target,
                                cutoff,
                                [],
                            )
                            blocked_reason = None
                            if nowcast is None:
                                blocked_reason = "no causal nowcast"
                                evidence = "unavailable"
                            elif bool(nowcast.forecast_data_stale):
                                blocked_reason = "fewer than two fresh causal models"
                                evidence = "unavailable"
                            stages = {
                                "Raw ensemble": (
                                    float(nowcast.raw_model_mean) if nowcast else None
                                ),
                                "Bias-corrected": (
                                    float(nowcast.corrected.mean) if nowcast else None
                                ),
                                "Live weather-adjusted": (
                                    float(nowcast.metar_conditioned_mean)
                                    if nowcast and target == cutoff.astimezone(
                                        checkpoint_at.tzinfo
                                    ).date()
                                    else None
                                ),
                                "Champion": (
                                    float(nowcast.final_forecast_mean) if nowcast else None
                                ),
                            }
                            if evidence == "unavailable":
                                stages = {stage: None for stage in stages}
                            for stage, forecast in stages.items():
                                result_rows.append(
                                    {
                                        "airport": AIRPORT,
                                        "target_date": target,
                                        "checkpoint": checkpoint_label,
                                        "stage": stage,
                                        "evidence_class": evidence,
                                        "forecast_c": forecast,
                                        "actual_c": actual,
                                        "metrics_json": metric_payload(
                                            metadata=metadata,
                                            evidence=evidence,
                                            checkpoint_at=cutoff,
                                            forecast=forecast,
                                            actual=actual,
                                            blocked_reason=blocked_reason,
                                        ),
                                    }
                                )
            finally:
                transaction.rollback()

        with replay_engine.begin() as connection:
            for row in result_rows:
                connection.execute(
                    text(
                        "INSERT INTO replay_lab.results ("
                        "run_id, airport, target_date, checkpoint, stage, evidence_class, "
                        "forecast_c, actual_c, metrics_json) VALUES ("
                        ":run_id, :airport, :target_date, :checkpoint, :stage, "
                        ":evidence_class, :forecast_c, :actual_c, "
                        "CAST(:metrics_json AS JSONB))"
                    ),
                    {
                        **{key: value for key, value in row.items() if key != "metrics_json"},
                        "run_id": run_id,
                        "metrics_json": json.dumps(row["metrics_json"], separators=(",", ":")),
                    },
                )
            connection.execute(
                text("UPDATE replay_lab.runs SET status = 'completed' WHERE id = :run_id"),
                {"run_id": run_id},
            )
    except Exception:
        with replay_engine.begin() as connection:
            connection.execute(
                text("UPDATE replay_lab.runs SET status = 'failed' WHERE id = :run_id"),
                {"run_id": run_id},
            )
        raise

    report = report_summary(result_rows, run_id)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
