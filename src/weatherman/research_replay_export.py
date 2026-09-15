"""Bounded read-only exports for the v0.1 Madrid research tracks."""
from __future__ import annotations

from datetime import date, datetime, timezone
from numbers import Number
from typing import Any

import pandas as pd
from sqlalchemy import select, tuple_

from . import __version__
from .db import DailyActual, ForecastSnapshot, ForecastVariantSnapshot, MarketSnapshot
from .market_replay_export import (
    AIRPORT,
    CHECKPOINT_LABELS,
    MAX_EXPORT_DAYS,
    _final_madrid_dates,
    build_market_replay_export,
    checkpoint_schedule,
)
from .research_tracks import (
    AUTOMATIC_PROMOTION,
    REGIME_MATRIX_VERSION,
    RESEARCH_ONLY,
    TRADING_CHALLENGER_VERSION,
    active_regimes,
    build_trading_shadow_decision,
    positive_temperature_bucket,
    probability_ranking,
    score_regime_matrix,
)


MAX_CHECKPOINT_ROWS = MAX_EXPORT_DAYS * len(CHECKPOINT_LABELS)
ESTIMATED_BYTES_PER_CHECKPOINT = 1_400
EXPORT_ENGINE_VERSION = "v10.7.11"
PROTECTED_FORECAST_BASELINE = "v10.7.10"

SNAPSHOT_FIELDS = (
    "airport", "target_date", "captured_at", "checkpoint_label", "checkpoint_at",
    "checkpoint_status", "checkpoint_reconstructed", "freshness_status", "evidence_class",
    "raw_model_mean_c", "bias_corrected_c", "metar_conditioned_c", "final_forecast_c",
    "raw_spread_c", "final_spread_c", "taf_max_temp_c", "taf_conflict",
    "temp_anchor_adjustment_c", "cloud_adjustment_c", "radiation_adjustment_c",
    "wind_adjustment_c", "late_dry_mixing_adjustment_c",
    "failed_convection_adjustment_c", "clear_sky_override_adjustment_c",
    "rapid_heat_ramp_active", "regional_cluster_active", "persistent_hot_active",
    "phase_vs_amplitude_active", "maritime_advection_active", "features_json",
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Number):
        return value.item() if hasattr(value, "item") else value
    return value


def _checkpoint_rows(connection, target_dates: list[date]) -> pd.DataFrame:
    if not target_dates:
        return pd.DataFrame()
    statement = (
        select(*(getattr(ForecastSnapshot, name) for name in SNAPSHOT_FIELDS))
        .where(
            ForecastSnapshot.airport == AIRPORT,
            ForecastSnapshot.target_date.in_(target_dates),
            ForecastSnapshot.checkpoint_label.in_(CHECKPOINT_LABELS),
        )
        .order_by(ForecastSnapshot.target_date.desc(), ForecastSnapshot.captured_at.desc())
        .limit(MAX_CHECKPOINT_ROWS + 1)
    )
    frame = pd.read_sql(statement, connection)
    if len(frame) > MAX_CHECKPOINT_ROWS:
        raise RuntimeError(
            f"Safety stop: checkpoint rows exceed MAX_CHECKPOINT_ROWS={MAX_CHECKPOINT_ROWS}."
        )
    if frame.empty:
        return frame
    frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    return frame.sort_values("captured_at").drop_duplicates(
        ["target_date", "checkpoint_label"], keep="last"
    )


def _champion_rows(connection, checkpoints: pd.DataFrame) -> pd.DataFrame:
    if checkpoints.empty:
        return pd.DataFrame()
    keys = [
        (row.target_date, pd.Timestamp(row.captured_at).to_pydatetime())
        for row in checkpoints.itertuples()
    ]
    statement = select(
        ForecastVariantSnapshot.target_date,
        ForecastVariantSnapshot.captured_at,
        ForecastVariantSnapshot.forecast_c,
        ForecastVariantSnapshot.spread_c,
        ForecastVariantSnapshot.probabilities_json,
        ForecastVariantSnapshot.forecast_confidence,
    ).where(
        ForecastVariantSnapshot.airport == AIRPORT,
        ForecastVariantSnapshot.variant == "Champion",
        tuple_(ForecastVariantSnapshot.target_date, ForecastVariantSnapshot.captured_at).in_(keys),
    ).limit(MAX_CHECKPOINT_ROWS + 1)
    frame = pd.read_sql(statement, connection)
    if len(frame) > MAX_CHECKPOINT_ROWS:
        raise RuntimeError("Safety stop: Champion rows exceed checkpoint cap.")
    if not frame.empty:
        frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
        frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    return frame


def _actual_map(connection, target_dates: list[date]) -> dict[date, float]:
    if not target_dates:
        return {}
    frame = pd.read_sql(
        select(DailyActual.target_date, DailyActual.max_temp_c, DailyActual.source).where(
            DailyActual.airport == AIRPORT,
            DailyActual.target_date.in_(target_dates),
        ).limit(MAX_EXPORT_DAYS * 4),
        connection,
    )
    if frame.empty:
        return {}
    frame = frame[frame.source.fillna("").astype(str).str.contains("metar|station", case=False)]
    return {
        pd.Timestamp(row.target_date).date(): float(row.max_temp_c)
        for row in frame.itertuples()
    }


def _resolution_map(connection, target_dates: list[date]) -> dict[date, dict[str, Any]]:
    if not target_dates:
        return {}
    statement = select(
        MarketSnapshot.target_date,
        MarketSnapshot.bucket_low_c,
        MarketSnapshot.bucket_high_c,
        MarketSnapshot.resolution_source,
        MarketSnapshot.captured_at,
    ).where(
        MarketSnapshot.airport == AIRPORT,
        MarketSnapshot.target_date.in_(target_dates),
        MarketSnapshot.closed.is_(True),
        MarketSnapshot.yes_won.is_(True),
    ).order_by(MarketSnapshot.captured_at.desc()).limit(MAX_EXPORT_DAYS * 4)
    frame = pd.read_sql(statement, connection)
    result: dict[date, dict[str, Any]] = {}
    for row in frame.itertuples():
        target = pd.Timestamp(row.target_date).date()
        if target in result or row.bucket_low_c != row.bucket_high_c:
            continue
        result[target] = {
            "bucket_c": positive_temperature_bucket(row.bucket_low_c),
            "resolution_source": row.resolution_source,
        }
    return result


def build_research_replay_export(
    connection,
    *,
    days: int = 30,
    end_date: date | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build both v0.1 tracks from one bounded extraction pass."""
    if not 1 <= int(days) <= MAX_EXPORT_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_EXPORT_DAYS}")
    generated = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    target_dates = _final_madrid_dates(connection, int(days), end_date)
    snapshots = _checkpoint_rows(connection, target_dates)
    champions = _champion_rows(connection, snapshots)
    if not snapshots.empty and not champions.empty:
        snapshots = snapshots.merge(
            champions,
            on=["target_date", "captured_at"],
            how="left",
            suffixes=("", "_champion"),
        )
    actuals = _actual_map(connection, target_dates)
    resolutions = _resolution_map(connection, target_dates)
    market_export = build_market_replay_export(
        connection, days=days, end_date=end_date, generated_at=generated
    )
    market_by_key = {
        (item["target_date"], item["checkpoint_label"]): item
        for item in market_export["checkpoints"]
    }

    checkpoint_records: list[dict[str, Any]] = []
    shadow_decisions: list[dict[str, Any]] = []
    for row in snapshots.sort_values(["target_date", "checkpoint_at"]).to_dict("records"):
        ranked = probability_ranking(row.get("probabilities_json"))
        top = ranked + [(None, None)] * 3
        target = row["target_date"]
        checkpoint_at = pd.to_datetime(row.get("checkpoint_at"), utc=True, errors="coerce")
        if pd.isna(checkpoint_at):
            checkpoint_at = dict(checkpoint_schedule(target))[row["checkpoint_label"]]
        record: dict[str, Any] = {
            "target_date": target.isoformat(),
            "checkpoint": row.get("checkpoint_label"),
            "checkpoint_at": pd.Timestamp(checkpoint_at).isoformat(),
            "captured_at": pd.Timestamp(row.get("captured_at")).isoformat(),
            "evidence_class": row.get("evidence_class"),
            "checkpoint_status": row.get("checkpoint_status"),
            "checkpoint_reconstructed": (
                bool(row.get("checkpoint_reconstructed"))
                if pd.notna(row.get("checkpoint_reconstructed")) else False
            ),
            "freshness_status": row.get("freshness_status"),
            "raw_c": row.get("raw_model_mean_c"),
            "bias_c": row.get("bias_corrected_c"),
            "live_c": row.get("metar_conditioned_c"),
            "champion_center_c": row.get("forecast_c", row.get("final_forecast_c")),
            "modal_bucket": top[0][0],
            "top1_probability": top[0][1],
            "top2_bucket": top[1][0],
            "top2_probability": top[1][1],
            "top3_bucket": top[2][0],
            "top3_probability": top[2][1],
            "top1_top2_gap_pp": (
                round((top[0][1] - top[1][1]) * 100, 8)
                if top[0][1] is not None and top[1][1] is not None else None
            ),
            "raw_spread_c": row.get("raw_spread_c"),
            "final_spread_c": row.get("spread_c", row.get("final_spread_c")),
            "forecast_confidence": row.get("forecast_confidence"),
            "taf_bucket": positive_temperature_bucket(row.get("taf_max_temp_c")),
            "taf_conflict": bool(row.get("taf_conflict")),
            "stored_metar_actual_c": actuals.get(target),
            "aemet_physical_tmax_c": None,
            "aemet_provenance": "not_read_from_production_neon",
        }
        for field in (
            "temp_anchor_adjustment_c", "cloud_adjustment_c", "radiation_adjustment_c",
            "wind_adjustment_c", "late_dry_mixing_adjustment_c",
            "failed_convection_adjustment_c", "clear_sky_override_adjustment_c",
            "rapid_heat_ramp_active", "regional_cluster_active", "persistent_hot_active",
            "phase_vs_amplitude_active", "maritime_advection_active", "features_json",
        ):
            record[field] = row.get(field)
        record["active_regimes"] = active_regimes(record)
        checkpoint_records.append(record)

        market_checkpoint = market_by_key.get((target.isoformat(), row["checkpoint_label"]))
        decision = build_trading_shadow_decision(
            target_date=target.isoformat(),
            checkpoint=str(row["checkpoint_label"]),
            generated_at=generated.isoformat(),
            probabilities=row.get("probabilities_json"),
            taf_bucket=record["taf_bucket"],
            market_checkpoint=market_checkpoint,
            forecast_confidence=record["forecast_confidence"],
            regimes=record["active_regimes"],
        )
        resolution = resolutions.get(target, {})
        decision.update(
            {
                "resolved_market_bucket": resolution.get("bucket_c"),
                "resolution_source": resolution.get("resolution_source"),
                "stored_metar_actual_c": actuals.get(target),
                "aemet_physical_tmax_c": None,
            }
        )
        asks = [pd.to_numeric(item.get("best_ask"), errors="coerce") for item in decision["selected_buckets"]]
        if (
            decision["status"] == "actionable_shadow"
            and resolution.get("bucket_c") is not None
            and asks
            and all(pd.notna(ask) and float(ask) > 0 for ask in asks)
        ):
            payout = sum(
                float(item["weight"]) / float(ask)
                for item, ask in zip(decision["selected_buckets"], asks, strict=True)
                if item["bucket_c"] == resolution["bucket_c"]
            )
            decision["hypothetical_pnl_units"] = round(payout - 1.0, 8)
        else:
            decision["hypothetical_pnl_units"] = None
        shadow_decisions.append(decision)

    cumulative = 0.0
    peak = 100.0
    max_drawdown = 0.0
    for decision in shadow_decisions:
        pnl = decision.get("hypothetical_pnl_units")
        if pnl is not None:
            cumulative += float(pnl)
            bankroll = 100.0 + cumulative
            peak = max(peak, bankroll)
            max_drawdown = max(max_drawdown, peak - bankroll)
            decision["cumulative_pnl_units"] = round(cumulative, 8)
            decision["bankroll_units"] = round(bankroll, 8)
            decision["max_drawdown_units"] = round(max_drawdown, 8)
        else:
            decision["cumulative_pnl_units"] = None
            decision["bankroll_units"] = None
            decision["max_drawdown_units"] = None

    public_records = [
        {key: value for key, value in record.items() if key != "features_json"}
        for record in checkpoint_records
    ]
    return _json_safe({
        "schema_version": "1.0",
        "application_version": __version__,
        "export_engine_version": EXPORT_ENGINE_VERSION,
        "protected_forecast_baseline": PROTECTED_FORECAST_BASELINE,
        "generated_at": generated.isoformat(),
        "airport": AIRPORT,
        "requested_final_days": int(days),
        "exported_final_days": len(target_dates),
        "checkpoint_rows": len(public_records),
        "maximum_checkpoint_rows": MAX_CHECKPOINT_ROWS,
        "research_only": RESEARCH_ONLY,
        "automatic_promotion": AUTOMATIC_PROMOTION,
        "evidence_policy": {
            "historical_export": "historical_replay",
            "reconstructed": "reconstructed_research",
            "sequential_oos": "not_claimed_by_this_export",
            "live_shadow": "only records captured after challenger deployment qualify",
        },
        "trading_challenger": {
            "version": TRADING_CHALLENGER_VERSION,
            "decisions": shadow_decisions,
            "pnl_unit": "one hypothetical currency unit per actionable checkpoint",
            "initial_bankroll_units": 100.0,
        },
        "regime_research": {
            "version": REGIME_MATRIX_VERSION,
            "definitions": "explicit versioned v0.1 rules in research_tracks.py",
            "checkpoint_records": public_records,
            "matrix": score_regime_matrix(checkpoint_records),
            "combinations": [],
            "combination_policy": "not emitted until each combination has N >= 10",
        },
        "transfer_policy": {
            "production_access": "read_only_transaction",
            "full_table_scans": False,
            "intraday_arrays": False,
            "raw_provider_payloads_exported": False,
            "aemet_from_neon": False,
        },
    })
