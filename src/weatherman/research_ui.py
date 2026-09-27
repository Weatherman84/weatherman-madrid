"""Streamlit view for additive research tracks; performs no database reads."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from .actual_quality import settlement_grade_actuals
from .d1_evening_export import model_history_features
from .d1_evening_research import (
    D1_CHECKPOINT,
    D1_CHALLENGER_VERSION,
    build_d1_walk_forward_challenger,
    compare_d1_challenger,
)
from .research_tracks import (
    REGIME_MATRIX_VERSION,
    TRADING_CHALLENGER_VERSION,
    active_regimes,
    build_trading_shadow_decision,
    checkpoint_evidence_class,
    positive_temperature_bucket,
    probability_ranking,
    regime_matrix_views,
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
        evidence_class = checkpoint_evidence_class(
            row.get("checkpoint_status"), row.get("checkpoint_reconstructed", False)
        )
        record = {
            "target_date": row.get("target_date").isoformat(),
            "checkpoint": row.get("checkpoint_label"),
            "checkpoint_at": row.get("checkpoint_at"),
            "checkpoint_status": row.get("checkpoint_status"),
            "checkpoint_reconstructed": evidence_class == "reconstructed_research",
            "evidence_class": evidence_class,
            "champion_center_c": row.get("forecast_c"),
            "champion_probabilities": row.get("probabilities_json"),
            "modal_bucket": ranked[0][0], "top2_bucket": ranked[1][0],
            "top3_bucket": ranked[2][0],
            "top1_probability": ranked[0][1],
            "top2_probability": ranked[1][1],
            "top3_probability": ranked[2][1],
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


def _render_d1_research(records: list[dict], forecasts: pd.DataFrame) -> None:
    st.subheader("D−1 Evening Research")
    d1_records = [row for row in records if row.get("checkpoint") == D1_CHECKPOINT]
    if not d1_records:
        st.info("No stored D−1 Evening checkpoint/Actual pairs are available yet.")
        return
    challenged = build_d1_walk_forward_challenger(d1_records)
    scheduled = [
        row for row in challenged if row.get("evidence_class") == "scheduled_causal"
    ]
    comparison = compare_d1_challenger(scheduled)
    latest = challenged[-1]
    checkpoint_at = pd.to_datetime(latest.get("checkpoint_at"), utc=True, errors="coerce")
    target_date = pd.to_datetime(latest.get("target_date"), errors="coerce")
    model_signal = "unavailable"
    if pd.notna(checkpoint_at) and pd.notna(target_date) and not forecasts.empty:
        history = model_history_features(
            forecasts,
            target_date.date(),
            pd.Timestamp(checkpoint_at).to_pydatetime(),
        )
        latest["consensus_run_trend_c"] = history["consensus_run_trend_c"]
        model_signal = history["consensus_movement"]
    challenger = latest["challenger"]
    champion_center = pd.to_numeric(latest.get("champion_center_c"), errors="coerce")
    challenger_center = pd.to_numeric(challenger.get("center_c"), errors="coerce")
    champion_label = (
        f"{float(champion_center):.2f} °C · {latest.get('modal_bucket')} °C"
        if pd.notna(champion_center) else "—"
    )
    challenger_label = (
        f"{float(challenger_center):.2f} °C · {challenger.get('modal_bucket')} °C"
        if pd.notna(challenger_center) else "—"
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Champion center / modal", champion_label)
    c2.metric("Challenger center / modal", challenger_label)
    c3.metric("TAF signal", ", ".join(code for code in challenger["reason_codes"] if code.startswith("taf_")) or "neutral")
    c4.metric("Model trend", model_signal)
    champion_rank = probability_ranking(latest.get("champion_probabilities"))[:3]
    challenger_rank = probability_ranking(challenger.get("probabilities"))[:3]
    rows = []
    for rank in range(3):
        champion_item = champion_rank[rank] if rank < len(champion_rank) else (None, None)
        challenger_item = challenger_rank[rank] if rank < len(challenger_rank) else (None, None)
        rows.append({
            "Rank": rank + 1,
            "Champion": (
                f"{champion_item[0]} °C · {champion_item[1]:.1%}"
                if champion_item[0] is not None else "—"
            ),
            "Challenger": (
                f"{challenger_item[0]} °C · {challenger_item[1]:.1%}"
                if challenger_item[0] is not None else "—"
            ),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    champion_metrics = comparison.get("champion", {})
    challenger_metrics = comparison.get("challenger", {})
    st.caption(
        f"{D1_CHALLENGER_VERSION} · scheduled-causal N={champion_metrics.get('n', 0)} · "
        f"modal accuracy Champion {champion_metrics.get('modal_accuracy', 0):.0%} vs "
        f"Challenger {challenger_metrics.get('modal_accuracy', 0):.0%} · "
        f"reason codes: {', '.join(challenger['reason_codes'])}."
    )
    st.caption(
        "Active regimes: "
        f"{', '.join(latest.get('active_regimes') or []) or 'none'} · "
        f"center MAE Champion {champion_metrics.get('center_mae_c') or 0:.2f} K vs "
        f"Challenger {challenger_metrics.get('center_mae_c') or 0:.2f} K."
    )
    st.caption(
        "RESEARCH ONLY · historical expanding-window replay, not sequential OOS. "
        "Reconstructed cases remain excluded from the default comparison; automatic "
        "promotion is disabled."
    )


def render_research_tracks(
    *, nowcast, snapshots, variants, actuals, markets, forecasts, target, now, zone
):
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
        matrix_views = regime_matrix_views(records)
        view_labels = {
            "scheduled_causal_only": "Scheduled-causal only",
            "reconstructed_research": "Reconstructed research",
            "all_research_evidence": "All research evidence",
        }
        selected_view = st.selectbox(
            "Evidence view",
            list(view_labels),
            format_func=view_labels.get,
            index=0,
            help=(
                "Scheduled-causal is the default. Reconstructed checkpoints remain a "
                "separate research class and are never presented as sequential OOS."
            ),
        )
        matrix = pd.DataFrame(matrix_views[selected_view]["matrix"])
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
            "combination is emitted below N=10. Reconstructed research and scheduled-causal "
            "evidence are separated; neither is labelled sequential OOS by this view."
        )
    _render_d1_research(records, forecasts)
