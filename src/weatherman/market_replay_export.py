"""Compact, causal Polymarket checkpoint export for Replay v2.

This module only reads stored market snapshots. It does not call Polymarket,
write to Neon, rebuild forecasts, or participate in the production champion.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import Select, func, literal, select, union_all

from . import __version__
from .actual_quality import settlement_grade_actuals
from .db import DailyActual, MarketSnapshot
from .settings import trading_airports


AIRPORT = "LEMD"
CHECKPOINT_LABELS = (
    "D-1 Evening @20:00",
    "D0 Morning @09:00",
    "First Live @12:00",
    "Late Live @16:00",
)
EXPORT_ENGINE_VERSION = "v10.7.11"
PROTECTED_FORECAST_BASELINE = "v10.7.10"
SCHEMA_VERSION = "1.0"
MAX_EXPORT_DAYS = 90
MAX_EXPORT_ROWS = 5_000
ESTIMATED_BYTES_PER_MARKET_ROW = 420
ESTIMATED_BUCKETS_PER_CHECKPOINT = 12

MARKET_COLUMNS = (
    "bucket_label",
    "bucket_low_c",
    "bucket_high_c",
    "yes_price",
    "best_bid",
    "best_ask",
    "spread",
    "volume",
    "liquidity",
    "closed",
    "yes_won",
    "resolution_source",
    "price_kind",
)


def _iso(value: object) -> str | None:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed).isoformat()


def _optional_number(value: object) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(parsed) else float(parsed)


def _optional_bool(value: object) -> bool | None:
    return None if value is None or pd.isna(value) else bool(value)


def _bucket_c(low: object, high: object) -> int | None:
    """Return an integer only for an exact-temperature market bucket."""
    low_value = _optional_number(low)
    high_value = _optional_number(high)
    if low_value is None or high_value is None or abs(low_value - high_value) > 1e-9:
        return None
    rounded = round(low_value)
    return int(rounded) if abs(low_value - rounded) < 1e-9 else None


def checkpoint_schedule(target: date) -> tuple[tuple[str, datetime], ...]:
    """Build the configured Madrid-local fixed checkpoints without forecast work."""
    airport = trading_airports()[AIRPORT]
    zone = ZoneInfo(str(airport["timezone"]))
    configured = {
        str(item.get("label")): item
        for item in airport.get("decision_checkpoints_local") or []
    }
    schedule: list[tuple[str, datetime]] = []
    for label in CHECKPOINT_LABELS:
        item = configured.get(label)
        if item is None:
            raise RuntimeError(f"Required Madrid checkpoint is not configured: {label}")
        hour, minute = (int(part) for part in str(item["time"]).split(":", 1))
        checkpoint_day = target - timedelta(days=int(item.get("target_day_offset", 0)))
        schedule.append(
            (
                label,
                datetime(
                    checkpoint_day.year,
                    checkpoint_day.month,
                    checkpoint_day.day,
                    hour,
                    minute,
                    tzinfo=zone,
                ).astimezone(timezone.utc),
            )
        )
    return tuple(schedule)


def _final_madrid_dates(connection, requested_days: int, end_date: date | None) -> list[date]:
    statement = select(
        DailyActual.airport,
        DailyActual.target_date,
        DailyActual.max_temp_c,
        DailyActual.source,
    ).where(DailyActual.airport == AIRPORT)
    if end_date is not None:
        statement = statement.where(DailyActual.target_date <= end_date)
    # The limit is deliberately applied in SQL.  Research export must never scan
    # the complete Actual table merely to find the newest final dates.
    actuals = pd.read_sql(
        statement.order_by(DailyActual.target_date.desc()).limit(requested_days * 4),
        connection,
    )
    final = settlement_grade_actuals(actuals)
    if final.empty:
        return []
    final["target_date"] = pd.to_datetime(final.target_date, errors="coerce").dt.date
    return list(
        final.dropna(subset=["target_date"])
        .drop_duplicates("target_date", keep="last")
        .sort_values("target_date")
        .tail(requested_days)
        .target_date
    )


def _checkpoint_market_statement(
    target: date,
    label: str,
    checkpoint_at: datetime,
) -> Select:
    latest_causal_capture = (
        select(func.max(MarketSnapshot.captured_at))
        .where(
            MarketSnapshot.airport == AIRPORT,
            MarketSnapshot.target_date == target,
            MarketSnapshot.captured_at <= checkpoint_at,
        )
        .scalar_subquery()
    )
    return select(
        literal(target).label("export_target_date"),
        literal(label).label("export_checkpoint_label"),
        literal(checkpoint_at).label("export_checkpoint_at"),
        MarketSnapshot.captured_at.label("market_captured_at"),
        *(getattr(MarketSnapshot, name) for name in MARKET_COLUMNS),
    ).where(
        MarketSnapshot.airport == AIRPORT,
        MarketSnapshot.target_date == target,
        MarketSnapshot.captured_at == latest_causal_capture,
    )


def _market_rows(connection, schedules: Iterable[tuple[date, str, datetime]]) -> pd.DataFrame:
    statements = [
        _checkpoint_market_statement(target, label, checkpoint_at)
        for target, label, checkpoint_at in schedules
    ]
    if not statements:
        return pd.DataFrame()
    return pd.read_sql(union_all(*statements), connection)


def build_market_replay_export(
    connection,
    *,
    days: int = 30,
    end_date: date | None = None,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Return a compact export using only snapshots available by each checkpoint."""
    requested_days = int(days)
    if not 1 <= requested_days <= MAX_EXPORT_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_EXPORT_DAYS}")
    generated = generated_at or datetime.now(timezone.utc)
    final_dates = _final_madrid_dates(connection, requested_days, end_date)
    schedules = [
        (target, label, checkpoint_at)
        for target in final_dates
        for label, checkpoint_at in checkpoint_schedule(target)
    ]
    rows = _market_rows(connection, schedules)
    if len(rows) > MAX_EXPORT_ROWS:
        raise RuntimeError(
            f"Safety stop: {len(rows)} market rows exceed MAX_EXPORT_ROWS={MAX_EXPORT_ROWS}."
        )
    if not rows.empty:
        rows["export_target_date"] = pd.to_datetime(
            rows.export_target_date, errors="coerce"
        ).dt.date
        rows["export_checkpoint_at"] = pd.to_datetime(
            rows.export_checkpoint_at, utc=True, errors="coerce"
        )
        rows["market_captured_at"] = pd.to_datetime(
            rows.market_captured_at, utc=True, errors="coerce"
        )

    checkpoints: list[dict[str, object]] = []
    available_count = 0
    for target, label, checkpoint_at in schedules:
        if rows.empty:
            selected = rows
        else:
            selected = rows[
                rows.export_target_date.eq(target)
                & rows.export_checkpoint_label.eq(label)
            ].copy()
        if selected.empty:
            checkpoints.append(
                {
                    "target_date": target.isoformat(),
                    "checkpoint_label": label,
                    "checkpoint_at": checkpoint_at.isoformat(),
                    "status": "unavailable",
                    "provenance": "unavailable_no_causal_market_snapshot",
                    "market_captured_at": None,
                    "market_snapshot_age_minutes": None,
                    "markets": [],
                }
            )
            continue

        captured_at = pd.Timestamp(selected.market_captured_at.iloc[0]).to_pydatetime()
        age_minutes = max(0.0, (checkpoint_at - captured_at).total_seconds() / 60)
        provenance = (
            "exact_checkpoint_market_snapshot"
            if age_minutes < 1 / 60
            else "nearest_available_before_checkpoint"
        )
        markets = []
        for row in selected.sort_values(
            ["bucket_low_c", "bucket_high_c", "bucket_label"], na_position="last"
        ).itertuples():
            markets.append(
                {
                    "bucket_label": str(row.bucket_label),
                    "bucket_c": _bucket_c(row.bucket_low_c, row.bucket_high_c),
                    "yes_price": _optional_number(row.yes_price),
                    "best_bid": _optional_number(row.best_bid),
                    "best_ask": _optional_number(row.best_ask),
                    "spread": _optional_number(row.spread),
                    "volume": _optional_number(row.volume),
                    "liquidity": _optional_number(row.liquidity),
                    "closed": bool(row.closed),
                    "yes_won": _optional_bool(row.yes_won),
                    "resolution_source": (
                        str(row.resolution_source) if row.resolution_source else None
                    ),
                    "price_kind": str(row.price_kind),
                }
            )
        available_count += 1
        checkpoints.append(
            {
                "target_date": target.isoformat(),
                "checkpoint_label": label,
                "checkpoint_at": checkpoint_at.isoformat(),
                "status": "available",
                "provenance": provenance,
                "market_captured_at": _iso(captured_at),
                "market_snapshot_age_minutes": round(age_minutes, 3),
                "markets": markets,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "application_version": __version__,
        "export_engine_version": EXPORT_ENGINE_VERSION,
        "protected_forecast_baseline": PROTECTED_FORECAST_BASELINE,
        "airport": AIRPORT,
        "timezone": "Europe/Madrid",
        "generated_at": generated.astimezone(timezone.utc).isoformat(),
        "requested_final_days": requested_days,
        "exported_final_days": len(final_dates),
        "checkpoint_count": len(checkpoints),
        "available_checkpoint_count": available_count,
        "exported_market_rows": int(len(rows)),
        "maximum_export_rows": MAX_EXPORT_ROWS,
        "research_only": True,
        "automatic_promotion": False,
        "causality_policy": (
            "latest stored market snapshot with captured_at <= checkpoint_at; "
            "future snapshots are never used"
        ),
        "limitations": [
            "No order-book depth is exported.",
            "Historical trade-price samples do not reconstruct executable bid/ask quotes.",
            "For historical trade-price samples, exact refers to the stored sample timestamp, "
            "not a guaranteed exchange trade timestamp.",
        ],
        "checkpoints": checkpoints,
    }
