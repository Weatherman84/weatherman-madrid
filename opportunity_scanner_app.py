from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SRC = Path(__file__).resolve().parent / "src"
src_path = str(SRC)
if src_path in sys.path:
    sys.path.remove(src_path)
sys.path.insert(0, src_path)

import pandas as pd
import streamlit as st

from weatherman.opportunity_scanner import (
    APP_NAME, APP_VERSION, load_airport_config, load_display_rules, load_rule_pack,
)
from weatherman.opportunity_scanner_public import (
    fetch_opportunity_scanner_journal, is_current_target_state, is_relevant_display_state,
)


st.set_page_config(page_title=f"{APP_NAME} · {APP_VERSION}", page_icon="🔭", layout="wide")
st.title(f"🔭 {APP_NAME}")
st.caption("v0.1-shadow · RESEARCH ONLY · NO AUTOMATIC PROMOTION · NO REAL-MONEY ORDERS")


def _pct(value: object) -> str:
    return f"{float(value):.1%}" if value is not None else "—"


def _temp(value: object) -> str:
    return f"{float(value):.1f} °C" if value is not None else "—"


def _status_icon(status: str) -> str:
    return {"HIGH INTEREST": "🟢", "WATCH": "🟡", "NO TRADE": "🔴", "NO SIGNAL": "⚪"}.get(status, "⚠️")


@st.cache_data(show_spinner=False)
def _local_payload(path: str) -> dict:
    try:
        return __import__("json").loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


url = os.getenv("OPPORTUNITY_SCANNER_JOURNAL_URL", "")
local_path = os.getenv("OPPORTUNITY_SCANNER_JOURNAL_PATH", "")
payload = fetch_opportunity_scanner_journal(url) if url else (_local_payload(local_path) if local_path else {})
airports = load_airport_config()["airports"]
rule_pack = load_rule_pack()
states = {row["airport"]: row for row in payload.get("airport_states", [])}
now = datetime.now(timezone.utc)
today_states = {
    code: state for code, state in states.items()
    if code in airports and is_current_target_state(state, airports[code]["timezone"], now=now)
}
display_states = {
    code: state for code, state in today_states.items()
    if is_relevant_display_state(state, airports[code]["timezone"], now=now)
}
page = st.sidebar.radio("View", ["Today's Opportunities", "Shadow Evidence", "Frozen Rules", "Health"])

if not payload:
    st.warning("No validated scanner journal is available. This is operationally distinct from NO SIGNAL.")

if page == "Today's Opportunities":
    active = [row for row in display_states.values() if row.get("rule_triggered")]
    high = [row for row in active if row.get("opportunity_status") == "HIGH INTEREST"]
    watch = [row for row in active if row.get("opportunity_status") == "WATCH"]
    best = sorted(active, key=lambda row: ({"HIGH INTEREST": 3, "WATCH": 2, "NO TRADE": 1}.get(row.get("opportunity_status"), 0),
                                           row.get("conservative_rule_edge") or -99), reverse=True)
    forward_by_rule = {row["rule_id"]: row for row in payload.get("forward_rule_metrics", [])}
    cols = st.columns(5)
    cols[0].metric("Active Opportunities", len(active))
    cols[1].metric("HIGH INTEREST", len(high))
    cols[2].metric("WATCH", len(watch))
    cols[3].metric("Next Signal Window", min((row.get("next_checkpoint") for row in today_states.values() if row.get("next_checkpoint")), default="—"))
    cols[4].metric("Best Opportunity Today", best[0]["airport"] if best else "—")
    st.subheader("Six-Airport Scanner")
    rows = []
    for code, config in airports.items():
        state = today_states.get(code, {})
        display_state = display_states.get(code, {})
        forecast = display_state.get("forecast", {})
        peak = forecast.get("expected_peak") or {}
        forward = forward_by_rule.get(display_state.get("rule_id"), {})
        status = state.get("opportunity_status", "DATA UNAVAILABLE")
        rows.append({
            "Airport": code, "Local time": now.astimezone(ZoneInfo(config["timezone"])).strftime("%d.%m. %H:%M"),
            "Status": f"{_status_icon(status)} {status}", "Rule": display_state.get("rule_id") or "—",
            "Checkpoint": display_state.get("checkpoint") or "—", "Current": _temp(forecast.get("current_temperature_c")),
            "Center": _temp(forecast.get("forecast_center_c")), "Bucket": forecast.get("modal_bucket", "—"),
            "Top1": _pct(forecast.get("top1_probability")), "Gap": _pct(forecast.get("top1_top2_gap")),
            "Top2": f"{forecast.get('top2_bucket', '—')} · {_pct(forecast.get('top2_probability'))}",
            "Spread": _temp(forecast.get("model_spread_c")), "Market": _pct(display_state.get("best_ask")),
            "Edge": _pct(display_state.get("conservative_rule_edge")),
            "Expected peak": peak.get("expected_peak_local") or "—",
            "Regimes": ", ".join(forecast.get("regimes", [])) or "—",
            "HC": display_state.get("hc_class") or "—",
            "Historical N / Acc": (f"{display_state['historical_rule_evidence']['validation_n']} / {_pct(display_state['historical_rule_evidence']['validation_accuracy'])}" if display_state.get("historical_rule_evidence") else "—"),
            "Forward N / Acc": (f"{forward.get('forward_n', 0)} / {_pct(forward.get('accuracy'))}" if display_state.get("rule_id") else "—"),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.subheader("Active Opportunities")
    if not active:
        st.info("No active HC-A/B signal in the current validated journal.")
    for row in best:
        forecast = row.get("forecast", {})
        with st.expander(f"{_status_icon(row['opportunity_status'])} {row['airport']} · {row['hc_class']} · {row['rule_id']}", expanded=row is best[0]):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Modal Bucket", f"{row['bucket']} °C")
            c2.metric("Top1 Probability", _pct(forecast.get("top1_probability")))
            c3.metric("Historical Validation", _pct(row["historical_rule_evidence"]["validation_accuracy"]))
            c4.metric("Conservative Edge", _pct(row.get("conservative_rule_edge")))
            st.write("Regimes:", ", ".join(forecast.get("regimes", [])) or "—")
            st.write("Expected peak:", (forecast.get("expected_peak") or {}).get("expected_peak_local") or "unavailable")
            st.write("Reason codes:", ", ".join(row.get("opportunity_reason_codes", [])))
            st.caption("Historical Research Evidence and sequential_oos_shadow are displayed separately.")

elif page == "Shadow Evidence":
    st.subheader("Forward Shadow Evidence")
    metrics = payload.get("forward_rule_metrics", [])
    if metrics:
        st.dataframe(pd.DataFrame(metrics), hide_index=True, width="stretch")
    else:
        st.info("No resolved sequential_oos_shadow trades yet. Historical validation is not added to Forward N.")
    st.subheader("Signal History")
    history = pd.DataFrame(payload.get("opportunity_signals", []))
    if history.empty:
        st.info("No immutable forward signal has been recorded.")
    else:
        trade_by_signal = {row["signal_id"]: row for row in payload.get("shadow_trades", [])}
        resolution_by_trade = {row["shadow_trade_id"]: row for row in payload.get("shadow_resolutions", [])}
        history["outcome"] = history.signal_id.map(lambda signal_id: (
            "WIN" if resolution_by_trade.get((trade_by_signal.get(signal_id) or {}).get("shadow_trade_id"), {}).get("win") is True
            else "LOSS" if resolution_by_trade.get((trade_by_signal.get(signal_id) or {}).get("shadow_trade_id"), {}).get("win") is False
            else "UNRESOLVED"
        ))
        filters = st.columns(6)
        airport = filters[0].selectbox("Airport", ["ALL", *sorted(history.airport.unique())])
        rule = filters[1].selectbox("Rule", ["ALL", *sorted(history.rule_id.unique())])
        hc = filters[2].selectbox("HC Class", ["ALL", *sorted(history.hc_class.unique())])
        season = filters[3].selectbox("Season", ["ALL", *sorted(history.season.unique())])
        checkpoint = filters[4].selectbox("Checkpoint", ["ALL", *sorted(history.checkpoint.unique())])
        outcome = filters[5].selectbox("Outcome", ["ALL", *sorted(history.outcome.unique())])
        shown = history
        for column, value in (("airport", airport), ("rule_id", rule), ("hc_class", hc), ("season", season), ("checkpoint", checkpoint), ("outcome", outcome)):
            if value != "ALL":
                shown = shown[shown[column] == value]
        st.dataframe(shown[["target_date", "airport", "checkpoint", "rule_id", "hc_class", "opportunity_status", "outcome", "triggered_at"]], hide_index=True, width="stretch")
    st.subheader("Portfolio")
    st.json(payload.get("portfolio_metrics", {}), expanded=False)
    st.subheader("Evidence Funnel")
    st.json(payload.get("evidence_funnel", {}), expanded=True)

elif page == "Frozen Rules":
    st.subheader("Frozen Opportunity Rule Set v1")
    st.code(payload.get("rule_set_sha256") or rule_pack["canonical_rules_sha256"])
    st.caption("24 active HC-A/B rules are immutable in v1. HC-C is display-only; rejected rules never trigger.")
    rules = pd.DataFrame(rule_pack["rules"])
    st.dataframe(rules[["rule_id", "airport", "checkpoint", "season", "hc_class", "validation_n", "validation_accuracy"]], hide_index=True, width="stretch")
    selected_id = st.selectbox("Rule detail", [rule["rule_id"] for rule in rule_pack["rules"]])
    selected = next(rule for rule in rule_pack["rules"] if rule["rule_id"] == selected_id)
    historical = {"Discovery": {"N": selected["discovery_n"], "Accuracy": selected["discovery_accuracy"], "CI95": selected["discovery_accuracy_ci95"]},
                  "Validation": {"N": selected["validation_n"], "Accuracy": selected["validation_accuracy"], "CI95": selected["validation_accuracy_ci95"]}}
    forward = next((row for row in payload.get("forward_rule_metrics", []) if row["rule_id"] == selected_id), {"forward_n": 0, "evidence_class": "sequential_oos_shadow"})
    triggers = [row for row in payload.get("opportunity_signals", []) if row["rule_id"] == selected_id]
    market_ids = {row["signal_id"] for row in payload.get("market_snapshots", [])}
    st.write("Conditions (read-only)")
    st.json(selected["selector_conditions"], expanded=True)
    c1, c2 = st.columns(2)
    c1.write("Historical Research Evidence")
    c1.json(historical)
    c2.write("Forward Shadow Evidence")
    c2.json({**forward, "trigger_count": len(triggers), "market_match_count": sum(row["signal_id"] in market_ids for row in triggers)})
    with st.expander("12 HC-C research-display rules"):
        display_rules = pd.DataFrame(load_display_rules())
        display_rules["selector_conditions"] = display_rules["selector_conditions"].map(
            lambda value: __import__("json").dumps(value, ensure_ascii=False, sort_keys=True)
        )
        st.dataframe(display_rules, hide_index=True, width="stretch")

else:
    st.subheader("Scanner Health")
    rows = []
    for code in airports:
        state = states.get(code, {})
        forecast = state.get("forecast", {})
        rows.append({"Airport": code, "Last Forecast Update": forecast.get("generated_at") or "—",
                     "Last Weather Input": forecast.get("weather_observed_at") or "—",
                     "Last Market Snapshot": (state.get("market_snapshot") or {}).get("snapshot_at") or "—",
                     "Next Expected Checkpoint": state.get("next_checkpoint") or "—",
                     "Current Status": state.get("opportunity_status") or "DATA UNAVAILABLE",
                     "Warnings": ", ".join(forecast.get("data_quality_warnings", [])) or "—"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("DATA UNAVAILABLE is never represented as NO SIGNAL. The app does not connect to Production Neon.")
