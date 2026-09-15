"""Streamlit view for additive research tracks; performs no database reads."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from .actual_quality import settlement_grade_actuals
from .research_tracks import (
    REGIME_MATRIX_VERSION,
    TRADING_CHALLENGER_VERSION,
    active_regimes,
    build_trading_shadow_decision,
    positive_temperature_bucket,
    probability_ranking,
    score_regime_matrix,
)


def _latest_checkpoint(now: datetime, zone: str) -> str:
    local = now.astimezone(ZoneInfo(zone))
    if local.hour >= 16:
        return "Late Live @16:00"
    if local.hour >= 12:
        return "First Live @12:00"
    if local.hour >= 9:
        return "D0 Morning @09:00"
    return "D-1 Evening @20:00"


def _market_payload(markets: pd.DataFrame, now: datetime) -> dict:
    if markets.empty:
        return {"status": "unavailable", "markets": []}
    frame = markets.copy()
    frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    captured = frame.captured_at.max()
    selected = frame[frame.captured_at.eq(captured)]
    rows = []
    for row in selected.itertuples():
        low = pd.to_numeric(getattr(row, "bucket_low_c", None), errors="coerce")
        high = pd.to_numeric(getattr(row, "bucket_high_c", None), errors="coerce")
        bucket = int(low) if pd.notna(low) and low == high and float(low).is_integer() else None
        rows.append({
            "bucket_c": bucket,
            "best_bid": getattr(row, "best_bid", None),
            "best_ask": getattr(row, "best_ask", None),
            "spread": getattr(row, "spread", None),
        })
    age = max(0.0, (now - pd.Timestamp(captured).to_pydatetime()).total_seconds() / 60)
    return {
        "status": "available",
        "market_captured_at": pd.Timestamp(captured).isoformat(),
        "market_snapshot_age_minutes": round(age, 2),
        "markets": rows,
    }


def _matrix_records(snapshots, variants, actuals) -> list[dict]:
    if snapshots.empty or variants.empty or actuals.empty:
        return []
    fixed = snapshots[snapshots.checkpoint_label.notna()].copy()
    champions = variants[variants.variant.astype(str).eq("Champion")].copy()
    for frame in (fixed, champions):
        frame["target_date"] = pd.to_datetime(frame.target_date, errors="coerce").dt.date
        frame["captured_at"] = pd.to_datetime(frame.captured_at, utc=True, errors="coerce")
    merged = fixed.merge(
        champions[["target_date", "captured_at", "forecast_c", "probabilities_json"]],
        on=["target_date", "captured_at"], how="inner",
    )
    final = settlement_grade_actuals(actuals).copy()
    final["target_date"] = pd.to_datetime(final.target_date, errors="coerce").dt.date
    actual_map = dict(zip(final.target_date, final.max_temp_c, strict=False))
    records = []
    for row in merged.to_dict("records"):
        ranked = probability_ranking(row.get("probabilities_json")) + [(None, None)] * 3
        record = {
            "checkpoint": row.get("checkpoint_label"),
            "champion_center_c": row.get("forecast_c"),
            "modal_bucket": ranked[0][0], "top2_bucket": ranked[1][0],
            "top3_bucket": ranked[2][0],
            "top1_top2_gap_pp": (
                (ranked[0][1] - ranked[1][1]) * 100
                if ranked[0][1] is not None and ranked[1][1] is not None else None
            ),
            "raw_spread_c": row.get("raw_spread_c"),
            "taf_bucket": positive_temperature_bucket(row.get("taf_max_temp_c")),
            "stored_metar_actual_c": actual_map.get(row.get("target_date")),
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
        records.append(record)
    return records


def render_research_tracks(*, nowcast, snapshots, variants, actuals, markets, target, now, zone):
    """Render from cached cockpit frames only; never opens a Neon connection."""
    st.header("Research · Shadow Mode")
    st.caption(
        "Strictly separate from the productive Champion · RESEARCH ONLY · "
        "automatic promotion disabled."
    )
    checkpoint = _latest_checkpoint(now, zone)
    taf_bucket = positive_temperature_bucket(
        getattr(getattr(nowcast, "taf_guidance", None), "max_temp_c", None)
    )
    decision = build_trading_shadow_decision(
        target_date=target.isoformat(), checkpoint=checkpoint,
        generated_at=now.isoformat(), probabilities=nowcast.probabilities,
        taf_bucket=taf_bucket, market_checkpoint=_market_payload(markets, now),
        forecast_confidence=nowcast.forecast_confidence,
        regimes=[str(nowcast.day_status.label)],
    )
    decision["evidence_class"] = "live_preview_not_persisted"

    left, right = st.columns(2)
    with left:
        st.subheader("Trading Shadow")
        st.caption(f"{TRADING_CHALLENGER_VERSION} · {checkpoint}")
        if decision["status"] == "observe_only":
            st.info("Observe only: v0.1 has no fixed allocation rule for this checkpoint.")
        elif decision["selected_buckets"]:
            display = pd.DataFrame(decision["selected_buckets"])
            display["weight"] = display.weight.map(lambda value: f"{value:.1%}")
            display["estimated_edge"] = display.estimated_edge.map(
                lambda value: f"{value:+.1%}" if pd.notna(value) else "—"
            )
            st.dataframe(display.rename(columns={
                "bucket_c": "Bucket °C", "weight": "Weight",
                "champion_probability": "Champion p", "best_bid": "Bid",
                "best_ask": "Ask", "spread": "Spread", "estimated_edge": "Edge",
            }), hide_index=True, width="stretch")
        else:
            st.warning("No complete Champion distribution is available.")
        st.caption(
            f"Market freshness: {decision['market_freshness_band']} · "
            f"strategy: {decision['strategy_code']}. P&L is evaluated only after official "
            "market resolution; Stored METAR and AEMET remain separate targets."
        )

    with right:
        st.subheader("Regime Research")
        records = _matrix_records(snapshots, variants, actuals)
        matrix = pd.DataFrame(score_regime_matrix(records))
        current_record = {
            "champion_center_c": nowcast.final_forecast_mean,
            "raw_spread_c": nowcast.raw_model_spread,
            "top1_top2_gap_pp": (
                (probability_ranking(nowcast.probabilities)[0][1]
                 - probability_ranking(nowcast.probabilities)[1][1]) * 100
                if len(probability_ranking(nowcast.probabilities)) > 1 else None
            ),
            "taf_bucket": taf_bucket,
            "modal_bucket": probability_ranking(nowcast.probabilities)[0][0],
            "top2_bucket": (
                probability_ranking(nowcast.probabilities)[1][0]
                if len(probability_ranking(nowcast.probabilities)) > 1 else None
            ),
            "features_json": nowcast.live_features,
            "temp_anchor_adjustment_c": nowcast.adjustment_contributions.get("temperature_anchor"),
            "cloud_adjustment_c": nowcast.adjustment_contributions.get("cloud"),
            "radiation_adjustment_c": nowcast.adjustment_contributions.get("radiation"),
            "wind_adjustment_c": nowcast.adjustment_contributions.get("wind"),
            "late_dry_mixing_adjustment_c": nowcast.adjustment_contributions.get("late_dry_mixing"),
            "failed_convection_adjustment_c": nowcast.adjustment_contributions.get("failed_convection"),
            "clear_sky_override_adjustment_c": nowcast.adjustment_contributions.get("clear_sky_override"),
            "rapid_heat_ramp_active": bool(nowcast.live_features.get("rapid_heat_ramp_active")),
            "regional_cluster_active": bool(nowcast.live_features.get("regional_cluster_active")),
            "persistent_hot_active": bool(nowcast.live_features.get("persistent_hot_active")),
            "phase_vs_amplitude_active": bool(nowcast.live_features.get("phase_vs_amplitude_active")),
            "maritime_advection_active": bool(nowcast.live_features.get("maritime_advection_active")),
        }
        active = active_regimes(current_record)
        st.write("Active today: " + (", ".join(active) if active else "no v0.1 regime flag"))
        if not matrix.empty and active:
            shown = matrix[(matrix.checkpoint.eq("ALL")) & matrix.regime.isin(active)].copy()
            shown["Modal accuracy"] = shown.modal_bucket_accuracy.map(lambda value: f"{value:.0%}")
            shown["Top2"] = shown.top2_coverage.map(lambda value: f"{value:.0%}")
            shown["Top3"] = shown.top3_coverage.map(lambda value: f"{value:.0%}")
            shown["Bias"] = shown.center_bias_c.map(lambda value: f"{value:+.2f} K")
            st.dataframe(shown[["regime", "n", "Modal accuracy", "Top2", "Top3", "Bias"]].rename(
                columns={"regime": "Regime", "n": "N"}
            ), hide_index=True, width="stretch")
        else:
            st.info("Historical matrix needs final checkpoint/Actual pairs.")
        st.caption(
            f"{REGIME_MATRIX_VERSION}. Small samples remain labelled and no multi-regime "
            "combination is emitted below N=10. Historical replay is not sequential OOS."
        )
