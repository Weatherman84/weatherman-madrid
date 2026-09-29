"""External, immutable Forward/Shadow journal for frozen research challengers.

The journal is built from bounded read-only checkpoint queries and published to
Cloudflare KV. It never writes to Production Neon and never changes the Champion.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

import pandas as pd
from sqlalchemy import select

from . import __version__
from .checkpoint_challengers import (
    D0_CHECKPOINT,
    FIRST_LIVE_CHECKPOINT,
    LATE_LIVE_CHECKPOINT,
    build_d0_walk_forward_challenger,
    build_late_live_walk_forward_challenger,
)
from .checkpoint_research_export import (
    EXPORT_ENGINE_VERSION,
    PROTECTED_FORECAST_BASELINE,
    build_checkpoint_research_export,
)
from .d1_evening_export import model_history_features
from .d1_evening_research import (
    D1_CHECKPOINT,
    build_d1_walk_forward_challenger,
    build_d1_walk_forward_challenger_v2,
)
from .db import DailyActual, Forecast, ForecastSnapshot, ForecastVariantSnapshot
from .research_tracks import active_regimes, positive_temperature_bucket, probability_ranking


SCHEMA_VERSION = "1.0"
JOURNAL_VERSION = "frozen_forward_shadow_v0.1"
FORWARD_START_DATE = date(2026, 9, 29)
MAX_SEED_DAYS = 30
MAX_FORWARD_DAYS = 120
MAX_SNAPSHOT_ROWS = MAX_FORWARD_DAYS * 4
MAX_MODEL_ROWS_PER_TARGET = 180
OPERATIONAL_LOOKBACK_DAYS = 2
MAX_OPERATIONAL_SNAPSHOT_ROWS = (OPERATIONAL_LOOKBACK_DAYS + 1) * 4
DECISION_EVIDENCE = "live_shadow"
OUTCOME_EVIDENCE = "sequential_oos"
FROZEN_STATUS = "FROZEN SHADOW"


def _json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    return value.item() if hasattr(value, "item") else value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _safe(value), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _settlement_actual(source: object) -> bool:
    value = str(source or "").strip().casefold()
    return (
        value in {"stored-metar-station", "airport metar"}
        or (("metar" in value or "station" in value) and "provisional" not in value)
        or "official" in value
        or "manual" in value
    )


def _score(probabilities: object, actual: int) -> tuple[float | None, float | None]:
    ranked = probability_ranking(probabilities)
    if not ranked:
        return None, None
    values = dict(ranked)
    buckets = set(values) | {actual}
    brier = sum(
        (values.get(bucket, 0.0) - (1.0 if bucket == actual else 0.0)) ** 2
        for bucket in buckets
    )
    return brier, -math.log(max(1e-12, values.get(actual, 0.0)))


def _base_record(
    snapshot: ForecastSnapshot,
    champion: ForecastVariantSnapshot,
    model_history: dict[str, Any],
    actual_c: float | None,
) -> dict[str, Any]:
    probabilities = _json(champion.probabilities_json, {})
    ranked = probability_ranking(probabilities)
    center = float(champion.forecast_c)
    boundary_distance = abs((center - math.floor(center)) - 0.5)
    features = _json(snapshot.features_json, {})
    compact_features = {
        key: features[key]
        for key in (
            "observed_heating_rate_60m_cph",
            "observed_dryness_c",
        )
        if key in features
    }
    record = {
        "target_date": snapshot.target_date.isoformat(),
        "checkpoint_label": snapshot.checkpoint_label,
        "checkpoint_at": _safe(snapshot.checkpoint_at),
        "generated_at": _safe(snapshot.checkpoint_recorded_at or snapshot.captured_at),
        "evidence_class": "scheduled_causal",
        "checkpoint_source_evidence_class": snapshot.evidence_class,
        "forecast_snapshot_id": snapshot.id,
        "champion_center_c": center,
        "champion_modal_bucket": ranked[0][0] if ranked else snapshot.champion_modal_bucket_c,
        "champion_probabilities": probabilities,
        "modal_bucket": ranked[0][0] if ranked else snapshot.champion_modal_bucket_c,
        "top1_probability": ranked[0][1] if ranked else None,
        "top2_bucket": ranked[1][0] if len(ranked) > 1 else None,
        "top2_probability": ranked[1][1] if len(ranked) > 1 else None,
        "top3_bucket": ranked[2][0] if len(ranked) > 2 else None,
        "top3_probability": ranked[2][1] if len(ranked) > 2 else None,
        "top1_top2_gap_pp": (
            (ranked[0][1] - ranked[1][1]) * 100 if len(ranked) > 1 else None
        ),
        "distance_to_bucket_boundary_c": boundary_distance,
        "model_spread_c": snapshot.raw_spread_c,
        "raw_spread_c": snapshot.raw_spread_c,
        "model_history": model_history,
        "consensus_run_trend_c": model_history.get("consensus_run_trend_c"),
        "taf_bucket": positive_temperature_bucket(snapshot.taf_max_temp_c),
        "taf_report_id": snapshot.taf_report_id,
        "taf_issue_time": _safe(snapshot.taf_issue_time),
        "taf_content_hash": snapshot.taf_content_hash,
        "taf_modal_bucket_flip": bool(snapshot.taf_modal_bucket_flip),
        "stored_metar_actual_c": actual_c,
        "actual_bucket": positive_temperature_bucket(actual_c),
        "temp_anchor_adjustment_c": snapshot.temp_anchor_adjustment_c,
        "clear_sky_override_adjustment_c": snapshot.clear_sky_override_adjustment_c,
        "correction_contributions": {
            "temp_anchor_adjustment_c": snapshot.temp_anchor_adjustment_c,
            "clear_sky_override_adjustment_c": snapshot.clear_sky_override_adjustment_c,
            "taf_adjustment_c": snapshot.taf_adjustment_c,
            "late_dry_mixing_adjustment_c": snapshot.late_dry_mixing_adjustment_c,
            "cloud_adjustment_c": snapshot.cloud_adjustment_c,
            "radiation_adjustment_c": snapshot.radiation_adjustment_c,
            "heating_rate_adjustment_c": snapshot.heating_rate_adjustment_c,
        },
        "features_json": compact_features,
        "cloud_adjustment_c": snapshot.cloud_adjustment_c,
        "radiation_adjustment_c": snapshot.radiation_adjustment_c,
        "wind_adjustment_c": snapshot.wind_adjustment_c,
        "late_dry_mixing_adjustment_c": snapshot.late_dry_mixing_adjustment_c,
        "failed_convection_adjustment_c": snapshot.failed_convection_adjustment_c,
        "rapid_heat_ramp_active": bool(snapshot.rapid_heat_ramp_active),
        "regional_cluster_active": bool(snapshot.regional_cluster_active),
        "persistent_hot_active": bool(snapshot.persistent_hot_active),
        "phase_vs_amplitude_active": bool(snapshot.phase_vs_amplitude_active),
        "maritime_advection_active": bool(snapshot.maritime_advection_active),
        "freshness_summary": {
            "status": snapshot.freshness_status,
            "expected_models": snapshot.expected_model_count,
            "available_models": snapshot.available_model_count,
            "fresh_models": snapshot.fresh_model_count,
            "used_models": snapshot.used_model_count,
            "source_age_min_minutes": snapshot.source_age_min_minutes,
            "source_age_median_minutes": snapshot.source_age_median_minutes,
            "source_age_max_minutes": snapshot.source_age_max_minutes,
        },
    }
    top2 = {record["champion_modal_bucket"], record["top2_bucket"]}
    taf_bucket = record["taf_bucket"]
    record["taf_agreement"] = (
        "agreement" if taf_bucket in top2
        else "disagreement" if taf_bucket is not None
        else "unavailable"
    )
    record["taf_outside_top2"] = taf_bucket is not None and taf_bucket not in top2
    record["active_regimes"] = active_regimes(record)
    return _safe(record)


def _model_history(session, target: date, cutoff: datetime) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(Forecast)
            .where(
                Forecast.airport == "LEMD",
                Forecast.target_date == target,
                Forecast.run_at >= cutoff - timedelta(hours=48),
                Forecast.run_at <= cutoff,
            )
            .order_by(Forecast.run_at.desc())
            .limit(MAX_MODEL_ROWS_PER_TARGET)
        )
    )
    frame = pd.DataFrame([{
        "model": row.model,
        "run_at": row.run_at,
        "target_date": row.target_date,
        "max_temp_c": row.max_temp_c,
        "source": row.source,
        "model_run_at": row.model_run_at,
        "available_at": row.available_at,
        "fetched_at": row.fetched_at,
        "provenance_status": row.provenance_status,
    } for row in rows])
    if not frame.empty:
        for column in ("run_at", "model_run_at", "available_at", "fetched_at"):
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    return _safe(model_history_features(frame, target, cutoff))


def _seed_records(session, start_date: date) -> list[dict[str, Any]]:
    payload = build_checkpoint_research_export(
        session.connection(),
        days=MAX_SEED_DAYS,
        end_date=start_date - timedelta(days=1),
    )
    fields = {
        "target_date", "checkpoint_label", "checkpoint_at", "evidence_class",
        "champion_center_c", "champion_modal_bucket", "champion_probabilities",
        "modal_bucket", "top1_probability", "top2_bucket", "top2_probability",
        "top3_bucket", "top3_probability", "top1_top2_gap_pp",
        "distance_to_bucket_boundary_c", "model_spread_c", "raw_spread_c",
        "model_history", "consensus_run_trend_c", "taf_bucket", "taf_agreement",
        "taf_outside_top2", "taf_modal_bucket_flip", "stored_metar_actual_c",
        "actual_bucket", "temp_anchor_adjustment_c",
        "clear_sky_override_adjustment_c", "correction_contributions",
        "features_json", "active_regimes", "rapid_heat_ramp_active",
        "regional_cluster_active", "persistent_hot_active",
        "phase_vs_amplitude_active", "maritime_advection_active",
    }
    result = []
    for source in payload.get("records", []):
        if source.get("evidence_class") != "scheduled_causal":
            continue
        record = {key: value for key, value in source.items() if key in fields}
        history = record.get("model_history") or {}
        record["model_history"] = {
            key: history.get(key)
            for key in (
                "consensus_run_trend_c",
                "consensus_movement",
                "warming_model_count",
                "cooling_model_count",
                "trend_agreement_count",
            )
            if key in history
        }
        record.pop("features_json", None)
        result.append(record)
    return result


def _compact_snapshot_model_signal(snapshot: ForecastSnapshot) -> dict[str, Any]:
    adjustment = float(snapshot.run_trend_adjustment_c or 0.0)
    movement = (
        "warming" if adjustment >= 0.2
        else "cooling" if adjustment <= -0.2
        else "stable"
    )
    return {
        "consensus_run_trend_c": adjustment,
        "consensus_movement": movement,
        "source": "stored_checkpoint_run_trend_adjustment",
        "raw_model_rows_read": 0,
    }


def _forward_records(
    session,
    start_date: date,
    end_date: date,
    existing_keys: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    snapshots = list(
        session.scalars(
            select(ForecastSnapshot)
            .where(
                ForecastSnapshot.airport == "LEMD",
                ForecastSnapshot.target_date >= start_date,
                ForecastSnapshot.target_date <= end_date,
                ForecastSnapshot.checkpoint_status == "scheduled-causal",
                ForecastSnapshot.checkpoint_reconstructed.is_(False),
                ForecastSnapshot.checkpoint_label.in_([
                    D1_CHECKPOINT, D0_CHECKPOINT, FIRST_LIVE_CHECKPOINT,
                    LATE_LIVE_CHECKPOINT,
                ]),
            )
            .order_by(ForecastSnapshot.target_date, ForecastSnapshot.checkpoint_at)
            .limit(MAX_OPERATIONAL_SNAPSHOT_ROWS + 1)
        )
    )
    if len(snapshots) > MAX_OPERATIONAL_SNAPSHOT_ROWS:
        raise RuntimeError("Safety stop: operational checkpoint rows exceed configured cap")
    actual_rows = list(
        session.scalars(
            select(DailyActual).where(
                DailyActual.airport == "LEMD",
                DailyActual.target_date >= start_date,
                DailyActual.target_date <= end_date,
            )
        )
    )
    actuals = {
        row.target_date.isoformat(): float(row.max_temp_c)
        for row in actual_rows if _settlement_actual(row.source)
    }
    result = []
    for snapshot in snapshots:
        key = (snapshot.target_date.isoformat(), snapshot.checkpoint_label)
        if key in existing_keys:
            continue
        champion = session.scalar(
            select(ForecastVariantSnapshot).where(
                ForecastVariantSnapshot.airport == "LEMD",
                ForecastVariantSnapshot.target_date == snapshot.target_date,
                ForecastVariantSnapshot.captured_at == snapshot.captured_at,
                ForecastVariantSnapshot.variant == "Champion",
            )
        )
        if champion is None:
            continue
        cutoff = snapshot.checkpoint_at or snapshot.captured_at
        model_history = (
            _model_history(session, snapshot.target_date, cutoff)
            if snapshot.checkpoint_label == D1_CHECKPOINT
            else _compact_snapshot_model_signal(snapshot)
        )
        result.append(_base_record(
            snapshot,
            champion,
            model_history,
            actuals.get(snapshot.target_date.isoformat()),
        ))
    return result, actuals


def _candidate_decisions(
    record: dict[str, Any], history: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    checkpoint = record["checkpoint_label"]
    rows = [
        item for item in history
        if item.get("checkpoint_label") == checkpoint
        and item.get("actual_bucket") is not None
    ] + [record]
    if checkpoint == D1_CHECKPOINT:
        first = build_d1_walk_forward_challenger(rows)[-1]["challenger"]
        second = build_d1_walk_forward_challenger_v2(rows)[-1]["challenger_v0_2"]
        return [first, second]
    if checkpoint == D0_CHECKPOINT:
        return [build_d0_walk_forward_challenger(rows)[-1]["forecast_research_shadow"]]
    if checkpoint == LATE_LIVE_CHECKPOINT:
        return [
            build_late_live_walk_forward_challenger(rows)[-1][
                "forecast_research_shadow"
            ]
        ]
    if checkpoint == FIRST_LIVE_CHECKPOINT:
        return [{
            "version": "first_live_champion_protected",
            "status": "champion_protected_no_active_forecast_challenger",
            "center_c": record["champion_center_c"],
            "modal_bucket": record["champion_modal_bucket"],
            "probabilities": record["champion_probabilities"],
            "center_adjustment_c": 0.0,
            "reason_codes": [
                "champion_protected_no_active_forecast_challenger",
                "uncertainty_and_trading_shadow_separate",
            ],
            "calibration": {"prior_n": len(rows) - 1},
        }]
    return []


def _expected_versions(checkpoint: str) -> tuple[str, ...]:
    return {
        D1_CHECKPOINT: (
            "d1_evening_challenger_v0.1",
            "d1_evening_challenger_v0.2",
        ),
        D0_CHECKPOINT: ("d0_morning_challenger_v0.1",),
        FIRST_LIVE_CHECKPOINT: ("first_live_champion_protected",),
        LATE_LIVE_CHECKPOINT: ("late_live_ablation_challenger_v0.1",),
    }.get(checkpoint, ())


def _candidate_value(candidate: Mapping[str, Any], preferred: str, fallback: str) -> Any:
    return candidate.get(preferred, candidate.get(fallback))


def _decision(record: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    version = str(_candidate_value(candidate, "challenger_version", "version"))
    calibration = candidate.get("walk_forward_calibration") or candidate.get(
        "calibration", {}
    )
    active_rule = candidate.get("selected_policy") or calibration.get(
        "selected_bias_policy"
    ) or candidate.get("status", "none")
    immutable = {
        "target_date": record["target_date"],
        "checkpoint_label": record["checkpoint_label"],
        "checkpoint_at": record["checkpoint_at"],
        "generated_at": record["generated_at"],
        "evidence_class": DECISION_EVIDENCE,
        "decision_evidence_class": DECISION_EVIDENCE,
        "checkpoint_source_evidence_class": record.get(
            "checkpoint_source_evidence_class"
        ),
        "challenger_version": version,
        "status": (
            candidate.get("status")
            if record["checkpoint_label"] == FIRST_LIVE_CHECKPOINT
            else FROZEN_STATUS
        ),
        "forecast_snapshot_id": record["forecast_snapshot_id"],
        "taf_snapshot": {
            "report_id": record.get("taf_report_id"),
            "issue_time": record.get("taf_issue_time"),
            "content_hash": record.get("taf_content_hash"),
        },
        "model_freshness_summary": record.get("freshness_summary"),
        "champion_center_c": record["champion_center_c"],
        "champion_modal_bucket": record["champion_modal_bucket"],
        "champion_top1": {
            "bucket": record["champion_modal_bucket"],
            "probability": record.get("top1_probability"),
        },
        "champion_top2": {
            "bucket": record.get("top2_bucket"),
            "probability": record.get("top2_probability"),
        },
        "champion_top3": {
            "bucket": record.get("top3_bucket"),
            "probability": record.get("top3_probability"),
        },
        "champion_probabilities": record["champion_probabilities"],
        "challenger_center_c": _candidate_value(
            candidate, "challenger_center_c", "center_c"
        ),
        "challenger_modal_bucket": _candidate_value(
            candidate, "challenger_modal_bucket", "modal_bucket"
        ),
        "challenger_probabilities": candidate.get("probabilities")
        or record["champion_probabilities"],
        "applied_delta_c": _candidate_value(
            candidate, "center_adjustment_c", "center_adjustment_c"
        ) or 0.0,
        "active_challenger_rule": active_rule,
        "reason_codes": candidate.get("reason_codes", []),
        "active_regimes": record.get("active_regimes", []),
        "taf_signal": {
            "bucket": record.get("taf_bucket"),
            "agreement": record.get("taf_agreement"),
            "outside_top2": record.get("taf_outside_top2"),
        },
        "bucket_boundary_signal": {
            "distance_c": record.get("distance_to_bucket_boundary_c"),
            "active": (
                record.get("distance_to_bucket_boundary_c") is not None
                and record["distance_to_bucket_boundary_c"] <= 0.15
            ),
        },
        "model_trend_signal": record.get("model_history", {}).get(
            "consensus_movement", "unavailable"
        ),
        "anchor_signal_c": record.get("temp_anchor_adjustment_c"),
        "clear_sky_signal_c": record.get("clear_sky_override_adjustment_c"),
        "other_compact_features": {
            "prior_scheduled_causal_n": calibration.get("prior_n"),
            "correction_deltas": candidate.get("correction_deltas", {}),
            "bucket_boundary_taf_interaction": candidate.get(
                "bucket_boundary_taf_interaction"
            ),
        },
        "research_only": True,
        "automatic_promotion": False,
        "logic_frozen": True,
    }
    return {
        "decision_id": (
            f"LEMD:{record['target_date']}:{record['checkpoint_label']}:"
            f"{version}"
        ),
        **_safe(immutable),
        "decision_hash": _hash(immutable),
        "outcome": None,
    }


def _validate_stored_decision(decision: Mapping[str, Any]) -> None:
    immutable = {
        key: value
        for key, value in decision.items()
        if key not in {"decision_id", "decision_hash", "outcome"}
    }
    if decision.get("decision_hash") != _hash(immutable):
        raise RuntimeError(
            f"Immutable decision mismatch: {decision.get('decision_id', 'unknown')}"
        )


def _outcome(decision: dict[str, Any], actual_c: float) -> dict[str, Any]:
    actual_bucket = positive_temperature_bucket(actual_c)
    champion_center = float(decision["champion_center_c"])
    challenger_center = float(decision["challenger_center_c"])
    champion_error = champion_center - actual_c
    challenger_error = challenger_center - actual_c
    champion_rank = probability_ranking(decision["champion_probabilities"])
    challenger_rank = probability_ranking(decision["challenger_probabilities"])
    champion_brier, champion_log = _score(
        decision["champion_probabilities"], actual_bucket
    )
    challenger_brier, challenger_log = _score(
        decision["challenger_probabilities"], actual_bucket
    )
    comparison = (
        "improved" if abs(challenger_error) < abs(champion_error) - 1e-12
        else "worsened" if abs(challenger_error) > abs(champion_error) + 1e-12
        else "unchanged"
    )
    return _safe({
        "outcome_evidence_class": OUTCOME_EVIDENCE,
        "stored_metar_actual": actual_c,
        "stored_metar_actual_bucket": actual_bucket,
        "aemet_tmax": None,
        "resolved_market_bucket": None,
        "champion": {
            "center_error_c": champion_error,
            "absolute_error_c": abs(champion_error),
            "bucket_error": int(decision["champion_modal_bucket"]) - actual_bucket,
            "modal_hit": int(decision["champion_modal_bucket"]) == actual_bucket,
            "top2_hit": actual_bucket in {item[0] for item in champion_rank[:2]},
            "top3_hit": actual_bucket in {item[0] for item in champion_rank[:3]},
            "brier_score": champion_brier,
            "log_loss": champion_log,
        },
        "challenger": {
            "center_error_c": challenger_error,
            "absolute_error_c": abs(challenger_error),
            "bucket_error": int(decision["challenger_modal_bucket"]) - actual_bucket,
            "modal_hit": int(decision["challenger_modal_bucket"]) == actual_bucket,
            "top2_hit": actual_bucket in {item[0] for item in challenger_rank[:2]},
            "top3_hit": actual_bucket in {item[0] for item in challenger_rank[:3]},
            "brier_score": challenger_brier,
            "log_loss": challenger_log,
        },
        "outcome_vs_champion": comparison,
    })


def _scorecard(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved = [
        row for row in decisions
        if (row.get("outcome") or {}).get("outcome_evidence_class")
        == OUTCOME_EVIDENCE
    ]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in resolved:
        groups.setdefault(
            (row["checkpoint_label"], row["challenger_version"]), []
        ).append(row)
    result = []
    for (checkpoint, version), rows in sorted(groups.items()):
        champion = [row["outcome"]["champion"] for row in rows]
        challenger = [row["outcome"]["challenger"] for row in rows]
        outcomes = Counter(row["outcome"]["outcome_vs_champion"] for row in rows)
        rules = Counter(row["active_challenger_rule"] for row in rows)
        result.append({
            "checkpoint_label": checkpoint,
            "challenger_version": version,
            "n": len(rows),
            "champion_modal_accuracy": sum(item["modal_hit"] for item in champion) / len(rows),
            "challenger_modal_accuracy": sum(item["modal_hit"] for item in challenger) / len(rows),
            "challenger_top2": sum(item["top2_hit"] for item in challenger) / len(rows),
            "challenger_top3": sum(item["top3_hit"] for item in challenger) / len(rows),
            "champion_mae_c": sum(item["absolute_error_c"] for item in champion) / len(rows),
            "champion_bias_c": sum(item["center_error_c"] for item in champion) / len(rows),
            "challenger_mae_c": sum(item["absolute_error_c"] for item in challenger) / len(rows),
            "challenger_bias_c": sum(item["center_error_c"] for item in challenger) / len(rows),
            "challenger_brier_score": sum(item["brier_score"] for item in challenger) / len(rows),
            "challenger_log_loss": sum(item["log_loss"] for item in challenger) / len(rows),
            "improved": outcomes["improved"],
            "worsened": outcomes["worsened"],
            "unchanged": outcomes["unchanged"],
            "rule_sample_sizes": dict(rules),
        })
    return result


def build_forward_shadow_journal(
    session,
    *,
    existing: dict[str, Any] | None = None,
    start_date: date = FORWARD_START_DATE,
    end_date: date | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Add missing causal decisions and outcomes without changing prior decisions."""
    generated = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = end_date or generated.date() + timedelta(days=1)
    if (end - start_date).days > MAX_FORWARD_DAYS:
        raise RuntimeError("Safety stop: configured forward horizon exceeds 120 days")
    query_start = max(start_date, end - timedelta(days=OPERATIONAL_LOOKBACK_DAYS))
    journal = dict(existing or {})
    seed = journal.get("calibration_seed")
    seed_created = not isinstance(seed, list)
    if not isinstance(seed, list):
        seed = _seed_records(session, start_date)
    stored_records = {
        (row["target_date"], row["checkpoint_label"]): row
        for row in journal.get("forward_records", [])
    }
    new_records, current_actuals = _forward_records(
        session, query_start, end, set(stored_records)
    )
    for row in new_records:
        key = (row["target_date"], row["checkpoint_label"])
        stored_records[key] = row
    for stored in stored_records.values():
        actual = current_actuals.get(stored["target_date"])
        if stored.get("actual_bucket") is None and actual is not None:
            # Outcome enrichment is allowed; checkpoint inputs remain frozen.
            stored["stored_metar_actual_c"] = actual
            stored["actual_bucket"] = positive_temperature_bucket(actual)
    forward_records = sorted(
        stored_records.values(),
        key=lambda item: (item["target_date"], item["checkpoint_label"]),
    )
    existing_decisions = journal.get("decisions", [])
    for stored_decision in existing_decisions:
        _validate_stored_decision(stored_decision)
    decision_map = {row["decision_id"]: row for row in existing_decisions}
    history = [*seed]
    for record in forward_records:
        expected_ids = {
            f"LEMD:{record['target_date']}:{record['checkpoint_label']}:{version}"
            for version in _expected_versions(record["checkpoint_label"])
        }
        if not expected_ids.issubset(decision_map):
            for candidate in _candidate_decisions(record, history):
                decision = _decision(record, candidate)
                existing_decision = decision_map.get(decision["decision_id"])
                if existing_decision is None:
                    decision_map[decision["decision_id"]] = decision
                elif existing_decision.get("decision_hash") != decision["decision_hash"]:
                    raise RuntimeError(
                        f"Immutable decision mismatch: {decision['decision_id']}"
                    )
        # The v1.0.19 calibration cohort is frozen at release. New forward
        # outcomes are scored below, never fed back into the challenger fit.
    actuals = {
        row["target_date"]: float(row["stored_metar_actual_c"])
        for row in forward_records if row.get("actual_bucket") is not None
    }
    for decision in decision_map.values():
        actual = actuals.get(decision["target_date"])
        if decision.get("outcome") is None and actual is not None:
            decision["outcome"] = _outcome(decision, actual)
    decisions = sorted(
        decision_map.values(),
        key=lambda item: (
            item["target_date"], item["checkpoint_label"], item["challenger_version"]
        ),
    )
    return _safe({
        "schema_version": SCHEMA_VERSION,
        "journal_version": JOURNAL_VERSION,
        "application_version": __version__,
        "export_engine_version": EXPORT_ENGINE_VERSION,
        "protected_forecast_baseline": PROTECTED_FORECAST_BASELINE,
        "airport": "LEMD",
        "classification": "READ-ONLY FROZEN FORWARD SHADOW JOURNAL",
        "generated_at": generated.isoformat(),
        "forward_start_date": start_date.isoformat(),
        "research_only": True,
        "automatic_promotion": False,
        "writes_production_database": False,
        "contains_credentials": False,
        "challenger_versions_frozen": [
            "d1_evening_challenger_v0.1",
            "d1_evening_challenger_v0.2",
            "d0_morning_challenger_v0.1",
            "first_live_champion_protected",
            "late_live_ablation_challenger_v0.1",
        ],
        "evidence_policy": {
            "decision": DECISION_EVIDENCE,
            "resolved_outcome": OUTCOME_EVIDENCE,
            "historical_replay_in_scorecard": False,
            "reconstructed_research_in_scorecard": False,
        },
        "calibration_seed": seed,
        "forward_records": forward_records,
        "decisions": decisions,
        "sequential_oos_scorecard": _scorecard(decisions),
        "log": {
            "calibration_seed_rows": len(seed),
            "forward_checkpoint_rows": len(forward_records),
            "decision_rows": len(decisions),
            "resolved_decision_rows": sum(row.get("outcome") is not None for row in decisions),
            "maximum_seed_days": MAX_SEED_DAYS,
            "maximum_forward_days": MAX_FORWARD_DAYS,
            "maximum_snapshot_rows": MAX_SNAPSHOT_ROWS,
            "maximum_operational_snapshot_rows": MAX_OPERATIONAL_SNAPSHOT_ROWS,
            "maximum_model_rows_per_target": MAX_MODEL_ROWS_PER_TARGET,
            "operational_lookback_days": OPERATIONAL_LOOKBACK_DAYS,
            "new_checkpoint_rows_this_run": len(new_records),
            "maximum_new_decision_rows_per_target_day": 5,
            "database_queries_executed_this_run": (
                2
                + len(new_records)
                + sum(
                    row["checkpoint_label"] == D1_CHECKPOINT
                    for row in new_records
                )
                + (9 if seed_created else 0)
            ),
            "production_access": "read_only_transaction",
            "automatic_backfill": False,
            "full_table_scans": False,
        },
    })
