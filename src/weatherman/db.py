from __future__ import annotations

import hashlib

from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .settings import ROOT, settings


class Base(DeclarativeBase):
    pass


class Forecast(Base):
    __tablename__ = "forecasts"
    __table_args__ = (UniqueConstraint("airport", "model", "run_at", "target_date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    model: Mapped[str] = mapped_column(String(80), index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    max_temp_c: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), default="forecast")
    horizon: Mapped[str] = mapped_column(String(20), default="Live", index=True)
    model_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provenance_status: Mapped[str | None] = mapped_column(String(120), nullable=True)


class HourlyForecast(Base):
    __tablename__ = "hourly_forecasts"
    __table_args__ = (UniqueConstraint("airport", "model", "run_at", "valid_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The four-column unique index already serves the airport-first reads and
    # upserts used by Weatherman. Separate one-column indexes consumed about
    # 17 MiB without improving any application query.
    airport: Mapped[str] = mapped_column(String(4))
    model: Mapped[str] = mapped_column(String(80))
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    temp_c: Mapped[float] = mapped_column(Float)
    dewpoint_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    cloud_cover: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_kph: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_direction: Mapped[float | None] = mapped_column(Float, nullable=True)
    radiation_wm2: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_850hpa_c: Mapped[float | None] = mapped_column(Float, nullable=True)


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (UniqueConstraint("airport", "observed_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    temp_c: Mapped[float] = mapped_column(Float)
    dewpoint_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_kph: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_direction: Mapped[float | None] = mapped_column(Float, nullable=True)
    cloud_cover: Mapped[float | None] = mapped_column(Float, nullable=True)
    cloud_base_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[str | None] = mapped_column(String(500), nullable=True)


class TafReport(Base):
    __tablename__ = "taf_reports"
    __table_args__ = (UniqueConstraint("airport", "issue_time", "raw_taf"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    issue_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    bulletin_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    raw_taf: Mapped[str] = mapped_column(Text)
    is_amended: Mapped[bool] = mapped_column(Boolean, default=False)
    is_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    max_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_temp_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    min_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_temp_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    periods_json: Mapped[str] = mapped_column(Text, default="[]")
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    source: Mapped[str] = mapped_column(String(50), default="aviationweather.gov")
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    revision_of_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    target_local_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    backfilled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    coverage_status: Mapped[str] = mapped_column(String(40), default="observed", index=True)


class DailyActual(Base):
    __tablename__ = "daily_actuals"
    __table_args__ = (UniqueConstraint("airport", "target_date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    max_temp_c: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), default="open-meteo")


class MarketPrice(Base):
    __tablename__ = "market_prices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    bucket_c: Mapped[int] = mapped_column(Integer)
    yes_price: Mapped[float] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (UniqueConstraint("market_id", "captured_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    event_slug: Mapped[str] = mapped_column(String(250), index=True)
    market_id: Mapped[str] = mapped_column(String(100), index=True)
    market_slug: Mapped[str] = mapped_column(String(300))
    token_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bucket_label: Mapped[str] = mapped_column(String(80))
    bucket_low_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    bucket_high_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    yes_price: Mapped[float] = mapped_column(Float)
    best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    yes_won: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    resolution_source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    price_kind: Mapped[str] = mapped_column(String(50), default="live")
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class SignalSnapshot(Base):
    __tablename__ = "signal_snapshots"
    __table_args__ = (UniqueConstraint("market_id", "captured_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    event_slug: Mapped[str] = mapped_column(String(250), index=True)
    market_id: Mapped[str] = mapped_column(String(100), index=True)
    bucket_label: Mapped[str] = mapped_column(String(80))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timing: Mapped[str] = mapped_column(String(30), index=True)
    model_probability: Mapped[float] = mapped_column(Float)
    market_probability: Mapped[float] = mapped_column(Float)
    buy_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal: Mapped[str] = mapped_column(String(30), index=True)
    day_phase: Mapped[str] = mapped_column(String(20))
    model_count: Mapped[int] = mapped_column(Integer)


class ForecastSnapshot(Base):
    """Immutable point forecasts for each step of the forecast ladder."""

    __tablename__ = "forecast_snapshots"
    __table_args__ = (UniqueConstraint("airport", "target_date", "captured_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timing: Mapped[str] = mapped_column(String(30), index=True)
    raw_model_mean_c: Mapped[float] = mapped_column(Float)
    weighted_raw_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    bias_corrected_equal_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    bias_corrected_c: Mapped[float] = mapped_column(Float)
    metar_conditioned_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_forecast_c: Mapped[float] = mapped_column(Float)
    raw_spread_c: Mapped[float] = mapped_column(Float)
    weighted_raw_spread_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    bias_corrected_equal_spread_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    bias_corrected_spread_c: Mapped[float] = mapped_column(Float)
    metar_conditioned_spread_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_spread_c: Mapped[float] = mapped_column(Float)
    observed_max_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    latest_metar_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_peak_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hours_to_peak: Mapped[float | None] = mapped_column(Float, nullable=True)
    day_phase: Mapped[str] = mapped_column(String(20), index=True)
    model_count: Mapped[int] = mapped_column(Integer)
    taf_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    taf_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    temp_anchor_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    dryness_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    dewpoint_trend_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    cloud_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    heating_rate_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    recent_error_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    radiation_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    wind_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    run_trend_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    late_dry_mixing_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    failed_convection_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    clear_sky_override_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    rapid_heat_ramp_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    regional_cluster_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    persistent_hot_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    phase_anchor_delta_c: Mapped[float] = mapped_column(Float, default=0.0)
    maritime_advection_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    rapid_heat_ramp_active: Mapped[bool] = mapped_column(Boolean, default=False)
    regional_cluster_active: Mapped[bool] = mapped_column(Boolean, default=False)
    persistent_hot_active: Mapped[bool] = mapped_column(Boolean, default=False)
    phase_vs_amplitude_active: Mapped[bool] = mapped_column(Boolean, default=False)
    maritime_advection_active: Mapped[bool] = mapped_column(Boolean, default=False)
    maritime_low_range_active: Mapped[bool] = mapped_column(Boolean, default=False)
    post_convective_active: Mapped[bool] = mapped_column(Boolean, default=False)
    post_convective_reports: Mapped[int] = mapped_column(Integer, default=0)
    post_convective_spread_multiplier: Mapped[float] = mapped_column(
        Float,
        default=1.0,
    )
    model_ceiling_reached_early: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )
    live_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    features_json: Mapped[str] = mapped_column(Text, default="{}")
    peak_lock_json: Mapped[str] = mapped_column(Text, default="{}")
    checkpoint_label: Mapped[str | None] = mapped_column(
        String(40), nullable=True, index=True
    )
    checkpoint_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    checkpoint_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    source_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    checkpoint_gap_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    checkpoint_reconstructed: Mapped[bool] = mapped_column(Boolean, default=False)
    checkpoint_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    freshness_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    evidence_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_age_at_checkpoint_minutes: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    source_age_min_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_age_median_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_age_max_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_model_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_model_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available_model_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fresh_model_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_model_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_coverage_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_models_json: Mapped[str] = mapped_column(Text, default="[]")
    available_models_json: Mapped[str] = mapped_column(Text, default="[]")
    used_models_json: Mapped[str] = mapped_column(Text, default="[]")
    extra_models_json: Mapped[str] = mapped_column(Text, default="[]")
    forecast_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    forecast_available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    forecast_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_provenance_json: Mapped[str] = mapped_column(Text, default="[]")
    post_peak_diagnostic_json: Mapped[str] = mapped_column(Text, default="{}")
    market_snapshot_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    market_snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    market_bucket_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    taf_report_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    taf_issue_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    taf_first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    taf_max_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    taf_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pre_taf_modal_bucket_c: Mapped[int | None] = mapped_column(Integer, nullable=True)
    champion_modal_bucket_c: Mapped[int | None] = mapped_column(Integer, nullable=True)
    taf_modal_bucket_flip: Mapped[bool] = mapped_column(Boolean, default=False)


class ForecastVariantSnapshot(Base):
    """Champion and one-factor-disabled challengers from the same information set."""

    __tablename__ = "forecast_variant_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "airport",
            "target_date",
            "captured_at",
            "variant",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timing: Mapped[str] = mapped_column(String(30), index=True)
    variant: Mapped[str] = mapped_column(String(80), index=True)
    factor: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    forecast_c: Mapped[float] = mapped_column(Float)
    spread_c: Mapped[float] = mapped_column(Float)
    probabilities_json: Mapped[str] = mapped_column(Text)
    forecast_confidence: Mapped[int] = mapped_column(Integer)
    day_phase: Mapped[str] = mapped_column(String(20), index=True)


class RegimeMemorySnapshot(Base):
    """Explainable early-warning and analog-memory state at one information set."""

    __tablename__ = "regime_memory_snapshots"
    __table_args__ = (
        UniqueConstraint("airport", "target_date", "captured_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timing: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    label: Mapped[str] = mapped_column(String(80), index=True)
    confidence: Mapped[int] = mapped_column(Integer)
    analog_count: Mapped[int] = mapped_column(Integer, default=0)
    best_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    center_adjustment_c: Mapped[float] = mapped_column(Float, default=0.0)
    suggested_forecast_c: Mapped[float] = mapped_column(Float)
    suggested_spread_c: Mapped[float] = mapped_column(Float)
    shadow_only: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    applied_to_champion: Mapped[bool] = mapped_column(Boolean, default=False)
    promotion_status: Mapped[str] = mapped_column(String(40), index=True)
    promotion_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    oos_days: Mapped[int] = mapped_column(Integer, default=0)
    regimes_json: Mapped[str] = mapped_column(Text, default="[]")
    analogs_json: Mapped[str] = mapped_column(Text, default="[]")
    pro_signals_json: Mapped[str] = mapped_column(Text, default="[]")
    contra_signals_json: Mapped[str] = mapped_column(Text, default="[]")
    explanation: Mapped[str] = mapped_column(Text)
    feature_signature_json: Mapped[str] = mapped_column(Text, default="{}")


class StrategySnapshot(Base):
    """One hypothetical consensus-bucket entry per strategy and information set."""

    __tablename__ = "strategy_snapshots"
    __table_args__ = (
        UniqueConstraint("airport", "target_date", "captured_at", "timing", "strategy"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timing: Mapped[str] = mapped_column(String(30), index=True)
    strategy: Mapped[str] = mapped_column(String(60), index=True)
    market_id: Mapped[str] = mapped_column(String(100), index=True)
    bucket_label: Mapped[str] = mapped_column(String(80))
    model_bucket_c: Mapped[int] = mapped_column(Integer)
    model_probability: Mapped[float] = mapped_column(Float)
    market_probability: Mapped[float] = mapped_column(Float)
    buy_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_basis: Mapped[str] = mapped_column(String(40), default="live best ask")
    day_phase: Mapped[str] = mapped_column(String(20))


class ShadowEvaluation(Base):
    """Fee- and depth-aware paper evaluation; never an executable order."""

    __tablename__ = "shadow_evaluations"
    __table_args__ = (UniqueConstraint("market_id", "captured_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    event_slug: Mapped[str] = mapped_column(String(250), index=True)
    market_id: Mapped[str] = mapped_column(String(100), index=True)
    token_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bucket_label: Mapped[str] = mapped_column(String(80))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timing: Mapped[str] = mapped_column(String(30), index=True)
    fair_probability: Mapped[float] = mapped_column(Float)
    raw_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    forecast_snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    displayed_probability: Mapped[float] = mapped_column(Float)
    best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    all_in_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_rate: Mapped[float] = mapped_column(Float, default=0.05)
    estimated_fee_usdc: Mapped[float | None] = mapped_column(Float, nullable=True)
    stake_usdc: Mapped[float] = mapped_column(Float, default=10.0)
    shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_cost_usdc: Mapped[float | None] = mapped_column(Float, nullable=True)
    available_depth_usdc: Mapped[float | None] = mapped_column(Float, nullable=True)
    depth_at_best_usdc: Mapped[float | None] = mapped_column(Float, nullable=True)
    fully_fillable: Mapped[bool] = mapped_column(Boolean, default=False)
    gross_edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    safety_margin: Mapped[float] = mapped_column(Float, default=0.02)
    forecast_confidence: Mapped[int] = mapped_column(Integer)
    day_phase: Mapped[str] = mapped_column(String(20))
    book_hash: Mapped[str | None] = mapped_column(String(100), nullable=True)
    book_age_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    blockers_json: Mapped[str] = mapped_column(Text, default="[]")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")


class BasketSnapshot(Base):
    """One simultaneous event-level basket assembled from executable shadow rows."""

    __tablename__ = "basket_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "airport",
            "target_date",
            "captured_at",
            "strategy",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(4), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    event_slug: Mapped[str] = mapped_column(String(250), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timing: Mapped[str] = mapped_column(String(30), index=True)
    strategy: Mapped[str] = mapped_column(String(60), index=True)
    market_ids_json: Mapped[str] = mapped_column(Text)
    bucket_labels_json: Mapped[str] = mapped_column(Text)
    market_count: Mapped[int] = mapped_column(Integer)
    fair_probability: Mapped[float] = mapped_column(Float)
    total_cost: Mapped[float] = mapped_column(Float)
    net_edge: Mapped[float] = mapped_column(Float)
    top_model_bucket: Mapped[str | None] = mapped_column(String(80), nullable=True)
    top_model_included: Mapped[bool] = mapped_column(Boolean, default=False)
    middle_bucket_excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), index=True)
    forecast_confidence: Mapped[int] = mapped_column(Integer)
    day_phase: Mapped[str] = mapped_column(String(20))
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")


class AirportMarketUniverse(Base):
    """Polymarket temperature cities discovered independently of station mapping."""

    __tablename__ = "airport_market_universe"
    __table_args__ = (UniqueConstraint("market_city"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_city: Mapped[str] = mapped_column(String(120), index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    airport: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    mapping_status: Mapped[str] = mapped_column(String(40), index=True)
    market_unit: Mapped[str | None] = mapped_column(String(5), nullable=True)
    resolution_source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    latest_event_slug: Mapped[str] = mapped_column(String(300))
    latest_target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class ProviderCall(Base):
    """One durable provider attempt for a named local decision checkpoint."""

    __tablename__ = "provider_calls"
    __table_args__ = (
        UniqueConstraint(
            "airport",
            "provider",
            "local_date",
            "checkpoint_label",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    airport: Mapped[str] = mapped_column(String(8), index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    local_date: Mapped[date] = mapped_column(Date, index=True)
    checkpoint_label: Mapped[str] = mapped_column(String(80), index=True)
    target_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class CollectionRun(Base):
    """Auditable status for one scheduled invocation of the consolidated collector."""

    __tablename__ = "collection_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    queue_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collector_version: Mapped[str] = mapped_column(String(30))
    trigger: Mapped[str] = mapped_column(String(30), default="schedule")
    overall_status: Mapped[str] = mapped_column(String(30), index=True)
    scheduler_drift_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    queue_delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    execution_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    airports_json: Mapped[str] = mapped_column(Text, default="[]")
    source_status_json: Mapped[str] = mapped_column(Text, default="{}")
    rows_read_json: Mapped[str] = mapped_column(Text, default="{}")
    rows_written_json: Mapped[str] = mapped_column(Text, default="{}")
    source_age_json: Mapped[str] = mapped_column(Text, default="{}")
    provider_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    airport_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    persistence_status: Mapped[str] = mapped_column(String(30), default="pending_commit")
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class CollectionCoverage(Base):
    """Per-airport/source explanation for data present, absent, delayed or rebuilt."""

    __tablename__ = "collection_coverage"
    __table_args__ = (UniqueConstraint("run_id", "airport", "data_type"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(120), index=True)
    airport: Mapped[str] = mapped_column(String(8), index=True)
    data_type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    latest_source_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rows_read: Mapped[int] = mapped_column(Integer, default=0)
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    source_age_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


def engine():
    if settings.database_url.startswith("sqlite:///"):
        path = ROOT / settings.database_url.removeprefix("sqlite:///")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(f"sqlite:///{path}")
    database_url = settings.database_url
    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix(
            "postgres://"
        )
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix(
            "postgresql://"
        )
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=2,
        pool_recycle=300,
        connect_args={"connect_timeout": 10},
    )


ENGINE = engine()
Session = sessionmaker(ENGINE, expire_on_commit=False)


def refresh_database_connections() -> None:
    """Drop pooled handles so a replaced SQLite snapshot is opened afresh.

    Streamlit can keep SQLAlchemy's pooled connection alive after GitHub deploys a
    newer database file. Disposing the pool is safe between requests and avoids an
    app reboot merely to see a newly committed METAR snapshot.
    """
    if ENGINE.dialect.name == "sqlite":
        ENGINE.dispose()


def init_db() -> None:
    Base.metadata.create_all(ENGINE)
    if ENGINE.dialect.name == "sqlite":
        with ENGINE.begin() as connection:
            columns = {row[1] for row in connection.execute(text("PRAGMA table_info(forecasts)"))}
            if "horizon" not in columns:
                connection.execute(
                    text("ALTER TABLE forecasts ADD COLUMN horizon VARCHAR(20) DEFAULT 'Legacy'")
                )
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_forecasts_horizon ON forecasts (horizon)")
            )
            observation_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(observations)"))
            }
            if "wind_direction" not in observation_columns:
                connection.execute(text("ALTER TABLE observations ADD COLUMN wind_direction FLOAT"))
            if "cloud_cover" not in observation_columns:
                connection.execute(text("ALTER TABLE observations ADD COLUMN cloud_cover FLOAT"))
            if "cloud_base_ft" not in observation_columns:
                connection.execute(text("ALTER TABLE observations ADD COLUMN cloud_base_ft FLOAT"))

            def add_columns(table: str, definitions: dict[str, str]) -> None:
                existing = {
                    row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))
                }
                for name, definition in definitions.items():
                    if name not in existing:
                        connection.execute(
                            text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                        )

            add_columns(
                "forecasts",
                {
                    "model_run_at": "DATETIME",
                    "available_at": "DATETIME",
                    "fetched_at": "DATETIME",
                    "provenance_status": "VARCHAR(120)",
                },
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_forecasts_model_run_at "
                    "ON forecasts (model_run_at)"
                )
            )
            add_columns(
                "forecast_snapshots",
                {
                    "weighted_raw_c": "FLOAT",
                    "bias_corrected_equal_c": "FLOAT",
                    "weighted_raw_spread_c": "FLOAT",
                    "bias_corrected_equal_spread_c": "FLOAT",
                    "temp_anchor_adjustment_c": "FLOAT DEFAULT 0",
                    "dryness_adjustment_c": "FLOAT DEFAULT 0",
                    "dewpoint_trend_adjustment_c": "FLOAT DEFAULT 0",
                    "cloud_adjustment_c": "FLOAT DEFAULT 0",
                    "heating_rate_adjustment_c": "FLOAT DEFAULT 0",
                    "recent_error_adjustment_c": "FLOAT DEFAULT 0",
                    "radiation_adjustment_c": "FLOAT DEFAULT 0",
                    "wind_adjustment_c": "FLOAT DEFAULT 0",
                    "run_trend_adjustment_c": "FLOAT DEFAULT 0",
                    "late_dry_mixing_adjustment_c": "FLOAT DEFAULT 0",
                    "failed_convection_adjustment_c": "FLOAT DEFAULT 0",
                    "clear_sky_override_adjustment_c": "FLOAT DEFAULT 0",
                    "rapid_heat_ramp_adjustment_c": "FLOAT DEFAULT 0",
                    "regional_cluster_adjustment_c": "FLOAT DEFAULT 0",
                    "persistent_hot_adjustment_c": "FLOAT DEFAULT 0",
                    "phase_anchor_delta_c": "FLOAT DEFAULT 0",
                    "maritime_advection_adjustment_c": "FLOAT DEFAULT 0",
                    "rapid_heat_ramp_active": "BOOLEAN DEFAULT 0",
                    "regional_cluster_active": "BOOLEAN DEFAULT 0",
                    "persistent_hot_active": "BOOLEAN DEFAULT 0",
                    "phase_vs_amplitude_active": "BOOLEAN DEFAULT 0",
                    "maritime_advection_active": "BOOLEAN DEFAULT 0",
                    "maritime_low_range_active": "BOOLEAN DEFAULT 0",
                    "post_convective_active": "BOOLEAN DEFAULT 0",
                    "post_convective_reports": "INTEGER DEFAULT 0",
                    "post_convective_spread_multiplier": "FLOAT DEFAULT 1",
                    "model_ceiling_reached_early": "BOOLEAN DEFAULT 0",
                    "live_adjustment_c": "FLOAT DEFAULT 0",
                    "features_json": "TEXT DEFAULT '{}'",
                    "peak_lock_json": "TEXT DEFAULT '{}'",
                    "checkpoint_label": "VARCHAR(40)",
                    "checkpoint_at": "DATETIME",
                    "checkpoint_recorded_at": "DATETIME",
                    "source_captured_at": "DATETIME",
                    "checkpoint_gap_minutes": "FLOAT",
                    "checkpoint_reconstructed": "BOOLEAN DEFAULT 0",
                    "checkpoint_status": "VARCHAR(40)",
                    "freshness_status": "VARCHAR(20)",
                    "evidence_class": "VARCHAR(20)",
                    "source_age_at_checkpoint_minutes": "FLOAT",
                    "source_age_min_minutes": "FLOAT",
                    "source_age_median_minutes": "FLOAT",
                    "source_age_max_minutes": "FLOAT",
                    "expected_model_count": "INTEGER",
                    "source_model_count": "INTEGER",
                    "available_model_count": "INTEGER",
                    "fresh_model_count": "INTEGER",
                    "used_model_count": "INTEGER",
                    "source_coverage_ratio": "FLOAT",
                    "expected_models_json": "TEXT DEFAULT '[]'",
                    "available_models_json": "TEXT DEFAULT '[]'",
                    "used_models_json": "TEXT DEFAULT '[]'",
                    "extra_models_json": "TEXT DEFAULT '[]'",
                    "forecast_run_at": "DATETIME",
                    "forecast_available_at": "DATETIME",
                    "forecast_fetched_at": "DATETIME",
                    "source_provenance_json": "TEXT DEFAULT '[]'",
                    "post_peak_diagnostic_json": "TEXT DEFAULT '{}'",
                    "market_snapshot_status": "VARCHAR(30)",
                    "market_snapshot_at": "DATETIME",
                    "market_bucket_count": "INTEGER",
                    "taf_report_id": "INTEGER",
                    "taf_issue_time": "DATETIME",
                    "taf_first_seen_at": "DATETIME",
                    "taf_max_temp_c": "FLOAT",
                    "taf_content_hash": "VARCHAR(64)",
                    "pre_taf_modal_bucket_c": "INTEGER",
                    "champion_modal_bucket_c": "INTEGER",
                    "taf_modal_bucket_flip": "BOOLEAN DEFAULT 0",
                },
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_forecast_snapshots_checkpoint_label "
                    "ON forecast_snapshots (checkpoint_label)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_forecast_snapshots_checkpoint_at "
                    "ON forecast_snapshots (checkpoint_at)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_forecast_snapshots_checkpoint_recorded_at "
                    "ON forecast_snapshots (checkpoint_recorded_at)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_forecast_snapshots_taf_issue_time "
                    "ON forecast_snapshots (taf_issue_time)"
                )
            )
            add_columns(
                "collection_runs",
                {
                    "event_created_at": "DATETIME",
                    "queue_started_at": "DATETIME",
                    "trigger_delay_seconds": "FLOAT",
                    "queue_delay_seconds": "FLOAT",
                    "execution_seconds": "FLOAT",
                    "provider_metrics_json": "TEXT DEFAULT '{}'",
                    "airport_metrics_json": "TEXT DEFAULT '{}'",
                },
            )
            add_columns(
                "collection_coverage",
                {
                    "duration_seconds": "FLOAT",
                    "attempts": "INTEGER",
                    "metrics_json": "TEXT DEFAULT '{}'",
                },
            )
            add_columns(
                "taf_reports",
                {
                    "content_hash": "VARCHAR(64)",
                    "revision_of_hash": "VARCHAR(64)",
                    "first_seen_at": "DATETIME",
                    "fetched_at": "DATETIME",
                    "target_local_date": "DATE",
                    "backfilled": "BOOLEAN DEFAULT 0",
                    "coverage_status": "VARCHAR(40) DEFAULT 'observed'",
                },
            )
            legacy_tafs = connection.execute(
                text(
                    "SELECT id, raw_taf, collected_at, content_hash "
                    "FROM taf_reports ORDER BY airport, issue_time, collected_at"
                )
            ).fetchall()
            for report_id, raw_taf, collected_at, content_hash in legacy_tafs:
                digest = content_hash or hashlib.sha256(
                    str(raw_taf or "").strip().encode("utf-8")
                ).hexdigest()
                connection.execute(
                    text(
                        "UPDATE taf_reports SET content_hash = :digest, "
                        "first_seen_at = COALESCE(first_seen_at, collected_at), "
                        "fetched_at = COALESCE(fetched_at, collected_at), "
                        "backfilled = COALESCE(backfilled, 0), "
                        "coverage_status = COALESCE(coverage_status, 'legacy-observed') "
                        "WHERE id = :report_id"
                    ),
                    {"digest": digest, "report_id": report_id},
                )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_taf_reports_airport_content_hash "
                    "ON taf_reports (airport, content_hash)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_taf_reports_first_seen_at "
                    "ON taf_reports (first_seen_at)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_taf_reports_target_local_date "
                    "ON taf_reports (target_local_date)"
                )
            )
            add_columns(
                "market_snapshots",
                {"price_kind": "VARCHAR(50) DEFAULT 'live'"},
            )
            add_columns(
                "shadow_evaluations",
                {
                    "raw_probability": "FLOAT",
                    "forecast_snapshot_at": "DATETIME",
                },
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_shadow_evaluations_forecast_snapshot_at "
                    "ON shadow_evaluations (forecast_snapshot_at)"
                )
            )
