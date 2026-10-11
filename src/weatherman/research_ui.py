"""Streamlit view for additive research tracks; performs no database reads."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from .actual_quality import settlement_grade_actuals
from .checkpoint_challengers import apply_checkpoint_research_challengers
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
from .shadow_engine_v1 import (
    OPPORTUNITY_VERSION, SHADOW_VERSION, build_shadow_mirror,
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
            "checkpoint_label": row.get("checkpoint_label"),
            "checkpoint_at": row.get("checkpoint_at"),
            "checkpoint_status": row.get("checkpoint_status"),
            "checkpoint_reconstructed": evidence_class == "reconstructed_research",
            "evidence_class": evidence_class,
            "champion_center_c": row.get("forecast_c"),
            "champion_probabilities": row.get("probabilities_json"),
            "champion_modal_bucket": ranked[0][0],
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
            "model_spread_c": row.get("raw_spread_c"),
            "model_history": {"consensus_movement": "not_loaded_in_cockpit_preview"},
            "taf_bucket": positive_temperature_bucket(row.get("taf_max_temp_c")),
            "stored_metar_actual_c": actual_map.get(row.get("target_date")),
            "actual_bucket": positive_temperature_bucket(
                actual_map.get(row.get("target_date"))
            ),
        }
        for field in (
            "temp_anchor_adjustment_c", "cloud_adjustment_c", "radiation_adjustment_c",
            "wind_adjustment_c", "heating_rate_adjustment_c",
            "late_dry_mixing_adjustment_c", "taf_modal_bucket_flip",
            "failed_convection_adjustment_c", "clear_sky_override_adjustment_c",
            "rapid_heat_ramp_active", "regional_cluster_active", "persistent_hot_active",
            "phase_vs_amplitude_active", "maritime_advection_active", "features_json",
        ):
            record[field] = row.get(field)
        record["active_regimes"] = active_regimes(record)
        record["taf_agreement"] = (
            "agreement" if record["taf_bucket"] in {
                record["modal_bucket"], record["top2_bucket"]
            } else "disagreement" if record["taf_bucket"] is not None
            else "unavailable"
        )
        record["taf_outside_top2"] = (
            record["taf_bucket"] is not None
            and record["taf_bucket"] not in {
                record["modal_bucket"], record["top2_bucket"]
            }
        )
        record["correction_contributions"] = {
            field: record.get(field) for field in (
                "temp_anchor_adjustment_c", "cloud_adjustment_c",
                "radiation_adjustment_c", "wind_adjustment_c",
                "heating_rate_adjustment_c",
                "late_dry_mixing_adjustment_c",
                "failed_convection_adjustment_c",
                "clear_sky_override_adjustment_c",
            )
        }
        record["correction_contributions"]["taf_adjustment_c"] = row.get(
            "taf_adjustment_c"
        )
        records.append(record)
    return records


def _render_checkpoint_forecast_research(records: list[dict]) -> None:
    st.subheader("Checkpoint Forecast Challengers")
    if not records:
        st.info("No stored fixed-checkpoint/Actual pairs are available yet.")
        return
    challenged, evaluation = apply_checkpoint_research_challengers(records)
    rows = []
    for checkpoint in (
        "D-1 Evening @20:00", "D0 Morning @09:00",
        "First Live @12:00", "Late Live @16:00",
    ):
        candidates = [
            row for row in challenged
            if row.get("checkpoint_label") == checkpoint
            and row.get("forecast_research_shadow")
        ]
        if not candidates:
            continue
        latest = sorted(candidates, key=lambda item: item["target_date"])[-1]
        shadow = latest["forecast_research_shadow"]
        champion_center = pd.to_numeric(
            latest.get("champion_center_c"), errors="coerce"
        )
        challenger_center = pd.to_numeric(
            shadow.get("challenger_center_c"), errors="coerce"
        )
        rows.append({
            "Checkpoint": checkpoint,
            "Version": shadow.get("challenger_version") or "Champion protected",
            "Champion": (
                f"{champion_center:.2f} °C · {latest.get('champion_modal_bucket')}"
                if pd.notna(champion_center) else "—"
            ),
            "Challenger": (
                f"{challenger_center:.2f} °C · {shadow.get('challenger_modal_bucket')}"
                if pd.notna(challenger_center) else "—"
            ),
            "Rule": shadow.get("selected_policy") or "none",
            "Prior N": shadow.get("prior_scheduled_causal_n"),
            "Status": shadow.get("status"),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    d0 = evaluation["d0_morning"]["comparison"]
    late = evaluation["late_live"]["comparison"]
    st.caption(
        f"D0 historical replay: modal Champion {d0['champion'].get('modal_accuracy', 0):.0%} "
        f"vs Challenger {d0['challenger'].get('modal_accuracy', 0):.0%}; "
        f"Late Live: {late['champion'].get('modal_accuracy', 0):.0%} vs "
        f"{late['challenger'].get('modal_accuracy', 0):.0%}."
    )
    st.caption(
        "Compact in-app preview from already cached frames; Workflow 12 remains the "
        "canonical export with causal model-run history. RESEARCH ONLY · historical "
        "walk-forward replay, not sequential OOS · automatic promotion disabled."
    )


def _render_forward_shadow_scorecard(journal: dict) -> None:
    st.subheader("Frozen Forward / Sequential-OOS")
    st.caption("RESEARCH ONLY – NO AUTOMATIC PROMOTION")
    if not journal:
        st.info(
            "Forward logging starts with App v1.0.19. The first immutable rows "
            "appear after the next real fixed checkpoint."
        )
        return
    decisions = journal.get("decisions", [])
    latest: dict[tuple[str, str], dict] = {}
    for decision in decisions:
        key = (decision.get("checkpoint_label"), decision.get("challenger_version"))
        if key not in latest or str(decision.get("generated_at")) > str(
            latest[key].get("generated_at")
        ):
            latest[key] = decision
    rows = []
    for score in journal.get("sequential_oos_scorecard", []):
        key = (score.get("checkpoint_label"), score.get("challenger_version"))
        last = latest.get(key, {})
        last_rule = last.get("active_challenger_rule", "—")
        rows.append({
            "Checkpoint": score.get("checkpoint_label"),
            "Version": score.get("challenger_version"),
            "Status": last.get("status", "FROZEN SHADOW"),
            "OOS N": score.get("n", 0),
            "Champion modal": score.get("champion_modal_accuracy"),
            "Challenger modal": score.get("challenger_modal_accuracy"),
            "Challenger Top2": score.get("challenger_top2"),
            "Challenger Top3": score.get("challenger_top3"),
            "Champion MAE": score.get("champion_mae_c"),
            "Champion bias": score.get("champion_bias_c"),
            "Challenger MAE": score.get("challenger_mae_c"),
            "Challenger bias": score.get("challenger_bias_c"),
            "Brier": score.get("challenger_brier_score"),
            "Log loss": score.get("challenger_log_loss"),
            "Improved / worsened / unchanged": (
                f"{score.get('improved', 0)} / {score.get('worsened', 0)} / "
                f"{score.get('unchanged', 0)}"
            ),
            "Last rule": last_rule,
            "Rule N": score.get("rule_sample_sizes", {}).get(last_rule, 0),
        })
    if not rows:
        pending = len(decisions)
        st.info(
            f"{pending} immutable live-shadow decision(s) stored; the Sequential-OOS "
            "scorecard starts after a final Stored-METAR Actual."
        )
        return
    shown = pd.DataFrame(rows)
    for column in (
        "Champion modal", "Challenger modal", "Challenger Top2", "Challenger Top3"
    ):
        shown[column] = shown[column].map(
            lambda value: f"{value:.1%}" if pd.notna(value) else "—"
        )
    for column in ("Champion MAE", "Challenger MAE"):
        shown[column] = shown[column].map(
            lambda value: f"{value:.3f} K" if pd.notna(value) else "—"
        )
    for column in ("Champion bias", "Challenger bias"):
        shown[column] = shown[column].map(
            lambda value: f"{value:+.3f} K" if pd.notna(value) else "—"
        )
    for column in ("Brier", "Log loss"):
        shown[column] = shown[column].map(
            lambda value: f"{value:.3f}" if pd.notna(value) else "—"
        )
    st.dataframe(shown, hide_index=True, width="stretch")
    st.caption(
        "Only decisions captured as live_shadow and later resolved from a final "
        "Stored-METAR Actual enter this scorecard as sequential_oos. Historical and "
        "reconstructed research are excluded. Aim: 20–30 new cases per challenger."
    )


def _display_probability(value: object) -> str:
    number = pd.to_numeric(value, errors="coerce")
    return f"{float(number):.1%}" if pd.notna(number) else "—"


def _render_shadow_engine_mirror(
    *, nowcast, checkpoint: str, taf_bucket: int | None,
    market_payload: dict, journal: dict,
) -> None:
    ranked = probability_ranking(nowcast.probabilities)
    current_record = {
        "checkpoint_label": checkpoint,
        "evidence_class": "live_preview",
        "champion_center_c": nowcast.final_forecast_mean,
        "champion_probabilities": nowcast.probabilities,
        "model_spread_c": nowcast.raw_model_spread,
        "taf_bucket": taf_bucket,
        "taf_agreement": (
            "agreement" if taf_bucket in {item[0] for item in ranked[:2]}
            else "disagreement" if taf_bucket is not None else "unavailable"
        ),
        "forecast_confidence": nowcast.forecast_confidence,
        "correction_contributions": dict(nowcast.adjustment_contributions),
        "features_json": nowcast.live_features,
        "top1_top2_gap_pp": (
            (ranked[0][1] - ranked[1][1]) * 100 if len(ranked) > 1 else None
        ),
        "raw_spread_c": nowcast.raw_model_spread,
        "temp_anchor_adjustment_c": nowcast.adjustment_contributions.get(
            "temperature_anchor"
        ),
        "cloud_adjustment_c": nowcast.adjustment_contributions.get("cloud"),
        "radiation_adjustment_c": nowcast.adjustment_contributions.get("radiation"),
        "wind_adjustment_c": nowcast.adjustment_contributions.get("wind"),
        "late_dry_mixing_adjustment_c": nowcast.adjustment_contributions.get(
            "late_dry_mixing"
        ),
        "failed_convection_adjustment_c": nowcast.adjustment_contributions.get(
            "failed_convection"
        ),
        "clear_sky_override_adjustment_c": nowcast.adjustment_contributions.get(
            "clear_sky_override"
        ),
        "rapid_heat_ramp_active": bool(
            nowcast.live_features.get("rapid_heat_ramp_active")
        ),
        "regional_cluster_active": bool(
            nowcast.live_features.get("regional_cluster_active")
        ),
        "persistent_hot_active": bool(
            nowcast.live_features.get("persistent_hot_active")
        ),
        "phase_vs_amplitude_active": bool(
            nowcast.live_features.get("phase_vs_amplitude_active")
        ),
        "maritime_advection_active": bool(
            nowcast.live_features.get("maritime_advection_active")
        ),
    }
    current_record["active_regimes"] = active_regimes(current_record)
    mirror = build_shadow_mirror(current_record, market=market_payload)
    production = mirror["production_ranking"] + [(None, None)] * 3
    shadow = mirror["shadow_ranking"] + [(None, None)] * 3
    signal = mirror["trading_opportunity"]

    st.subheader("Shadow Engine Mirror")
    st.caption(
        f"{SHADOW_VERSION} · {checkpoint} · RESEARCH ONLY – NO AUTOMATIC "
        "PROMOTION OR EXECUTION"
    )
    mirror_rows = [
        {
            "Field": "Forecast center",
            "Production": f"{mirror['production_center_c']:.2f} °C",
            "Shadow": f"{mirror['shadow_center_c']:.2f} °C",
            "Delta": f"{mirror['center_delta_c']:+.2f} K",
        },
        {
            "Field": "Modal bucket",
            "Production": f"{mirror['production_modal_bucket']} °C",
            "Shadow": f"{mirror['shadow_modal_bucket']} °C",
            "Delta": f"{mirror['modal_delta_buckets']:+d} bucket",
        },
    ]
    for index, label in enumerate(("Top1", "Top2", "Top3")):
        mirror_rows.append({
            "Field": label,
            "Production": f"{production[index][0]} °C · {_display_probability(production[index][1])}",
            "Shadow": f"{shadow[index][0]} °C · {_display_probability(shadow[index][1])}",
            "Delta": (
                f"{(shadow[index][1] - production[index][1]) * 100:+.1f} pp"
                if production[index][1] is not None and shadow[index][1] is not None
                else "—"
            ),
        })
    mirror_rows.extend([
        {
            "Field": "Model spread", "Production": f"{nowcast.raw_model_spread:.2f} K",
            "Shadow": f"{nowcast.raw_model_spread:.2f} K", "Delta": "0",
        },
        {
            "Field": "TAF TX", "Production": f"{taf_bucket} °C" if taf_bucket is not None else "—",
            "Shadow": f"{taf_bucket} °C" if taf_bucket is not None else "—", "Delta": "0",
        },
        {
            "Field": "Regimes", "Production": ", ".join(mirror["active_regimes"]) or "none",
            "Shadow": ", ".join(mirror["active_regimes"]) or "none", "Delta": "—",
        },
        {
            "Field": "Confidence", "Production": str(nowcast.forecast_confidence),
            "Shadow": "research confidence not independently calibrated", "Delta": "—",
        },
        {
            "Field": "Trading signal", "Production": "—",
            "Shadow": signal["classification"], "Delta": "research only",
        },
    ])
    st.dataframe(pd.DataFrame(mirror_rows), hide_index=True, width="stretch")

    left, right = st.columns(2)
    with left:
        st.markdown("**Correction comparison**")
        corrections = pd.DataFrame(mirror["correction_comparison"])
        for column in ("production_c", "shadow_c", "delta_c"):
            corrections[column] = corrections[column].map(lambda value: f"{value:+.3f} K")
        st.dataframe(corrections.rename(columns={
            "correction": "Correction", "production_c": "Production",
            "shadow_c": "Shadow", "delta_c": "Delta",
        }), hide_index=True, width="stretch")
    with right:
        icons = {
            "HIGH INTEREST": "🟢", "WATCH": "🟡", "NO TRADE": "🔴",
            "NO SIGNAL": "⚪",
        }
        st.markdown(
            f"### {icons.get(signal['classification'], '⚪')} "
            f"{signal['classification']}"
        )
        st.caption(f"{OPPORTUNITY_VERSION} · transparent rule set")
        st.write(f"Policy: **{mirror['selected_policy']}**")
        st.write(f"Raw edge: **{_display_probability(signal['raw_model_market_edge'])}**")
        st.write(f"Top1/Top2 gap: **{signal['top1_top2_gap_pp']:.1f} pp**" if signal["top1_top2_gap_pp"] is not None else "Top1/Top2 gap: —")
        st.write(f"Market freshness: **{signal['market_freshness']}**")
        for blocker in signal["blockers"]:
            st.write(f"• {blocker}")
        st.caption(
            "Raw edge is diagnostic only. Historical Replay did not show a monotonic "
            "edge-to-profit relation. No automatic execution."
        )

    with st.expander("Historical research evidence and market layer", expanded=False):
        evidence = signal["historical_evidence_used"]
        if evidence:
            evidence_frame = pd.DataFrame(evidence)
            evidence_frame["historical_roi"] = evidence_frame.historical_roi.map(
                _display_probability
            )
            st.dataframe(evidence_frame.rename(columns={
                "regime": "Regime", "historical_n": "Historical N",
                "historical_roi": "Historical ROI",
                "robustness_classification": "Robustness",
                "historical_max_drawdown": "Max drawdown",
                "seasonal_coverage": "Seasonal coverage",
            }), hide_index=True, width="stretch")
        else:
            st.info("No quantitative historical regime evidence for the active setup.")
        forecast_evidence = mirror.get("forecast_historical_evidence")
        if forecast_evidence:
            st.json(forecast_evidence)
        market_rows = []
        by_bucket = {row.get("bucket_c"): row for row in market_payload.get("markets", [])}
        for bucket, probability in shadow[:3]:
            row = by_bucket.get(bucket, {})
            ask = row.get("best_ask")
            price = ask if ask is not None else row.get("yes_price")
            market_rows.append({
                "Bucket": f"{bucket} °C" if bucket is not None else "—",
                "Shadow probability": _display_probability(probability),
                "Market price": _display_probability(price),
                "Raw edge": _display_probability(
                    probability - price if probability is not None and price is not None else None
                ),
                "Best bid": _display_probability(row.get("best_bid")),
                "Best ask": _display_probability(ask),
                "Spread": _display_probability(row.get("spread")),
                "Snapshot age": market_payload.get("market_snapshot_age_minutes"),
            })
        st.dataframe(pd.DataFrame(market_rows), hide_index=True, width="stretch")
        st.caption("Historical research evidence · Seasonal coverage: Apr–Oct 2026 only")

    persisted = [
        row for row in journal.get("shadow_engine_decisions", [])
        if row.get("checkpoint_label") == checkpoint
    ]
    if persisted:
        latest = sorted(persisted, key=lambda row: str(row.get("generated_at")))[-1]
        st.caption(
            f"Latest immutable {SHADOW_VERSION} decision: {latest.get('target_date')} · "
            f"hash {str(latest.get('decision_hash', ''))[:12]}…"
        )
    else:
        st.caption("Live preview; immutable row appears after Workflow 13 publishes this checkpoint.")


def render_research_tracks(
    *, nowcast, snapshots, variants, actuals, markets, forecasts,
    forward_shadow_journal, target, now, zone
):
    """Render from cached cockpit frames only; never opens a Neon connection."""
    st.header("Research · Shadow Mode")
    st.caption(
        "Strictly separate from the productive Champion · RESEARCH ONLY · "
        "automatic promotion disabled."
    )
    _render_forward_shadow_scorecard(forward_shadow_journal)
    checkpoint = _latest_checkpoint(now, zone)
    taf_bucket = positive_temperature_bucket(
        getattr(getattr(nowcast, "taf_guidance", None), "max_temp_c", None)
    )
    market_payload = _market_payload(markets, now)
    _render_shadow_engine_mirror(
        nowcast=nowcast,
        checkpoint=checkpoint,
        taf_bucket=taf_bucket,
        market_payload=market_payload,
        journal=forward_shadow_journal,
    )
    decision = build_trading_shadow_decision(
        target_date=target.isoformat(), checkpoint=checkpoint,
        generated_at=now.isoformat(), probabilities=nowcast.probabilities,
        taf_bucket=taf_bucket, market_checkpoint=market_payload,
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
    _render_checkpoint_forecast_research(records)
