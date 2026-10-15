from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

SRC = Path(__file__).resolve().parent / "src"
src_path = str(SRC)
if src_path in sys.path:
    sys.path.remove(src_path)
sys.path.insert(0, src_path)

import pandas as pd
import streamlit as st

from weatherman.opportunity_scanner import (
    APP_NAME,
    APP_VERSION,
    load_airport_config,
    load_display_rules,
    load_rule_pack,
)
from weatherman.opportunity_scanner_public import fetch_opportunity_scanner_journal


st.set_page_config(page_title=f"{APP_NAME} · {APP_VERSION}", page_icon="🔭", layout="wide")
st.title(f"🔭 {APP_NAME}")
st.caption("v0.1-shadow · NUR RESEARCH · KEINE AUTOMATISCHE PROMOTION · KEINE ECHTGELD-ORDERS")


STATUS_LABELS = {
    "HIGH INTEREST": "Hohes Interesse",
    "WATCH": "Beobachten",
    "NO TRADE": "Kein Trade",
    "NO SIGNAL": "Kein Signal",
    "DATA UNAVAILABLE": "Daten fehlen",
}
STATUS_ICONS = {
    "HIGH INTEREST": "🟢",
    "WATCH": "🟡",
    "NO TRADE": "🔴",
    "NO SIGNAL": "⚪",
    "DATA UNAVAILABLE": "⚠️",
}
WARNING_LABELS = {
    "missing_model": "Mindestens ein Wettermodell fehlt",
    "insufficient_calibration_history": "Zu wenig frühere Peak-Zeiten",
    "stale_weather_data": "Wetterbeobachtung fehlt oder ist zu alt",
    "checkpoint_missed": "Vorgesehenes Checkpoint-Fenster verpasst",
    "missing_market_match": "Kein passender Markt-Bucket gefunden",
    "best_ask_unavailable": "Kein aktueller Best Ask verfügbar",
    "market_already_resolved": "Markt bereits geschlossen oder aufgelöst",
    "market_spread_too_wide": "Bid-Ask-Spread zu breit",
    "non_positive_conservative_edge": "Kein positiver konservativer Edge",
    "market_quote_stale": "Marktquote ist veraltet",
    "market_quote_very_stale": "Marktquote ist stark veraltet",
    "market_quote_unavailable": "Alter der Marktquote nicht bestimmbar",
}
FIELD_LABELS = {
    "top1_probability": "Top1-Wahrscheinlichkeit",
    "top1_top2_gap": "Abstand Top1–Top2",
    "model_spread_c": "Modellstreuung",
    "regimes": "Wetterregime",
}
OPERATOR_LABELS = {"ge": "mindestens", "lt": "kleiner als", "contains": "enthält"}


def _pct(value: object) -> str:
    return f"{float(value):.1%}" if value is not None else "—"


def _temp(value: object) -> str:
    return f"{float(value):.1f} °C" if value is not None else "—"


def _money(value: object) -> str:
    return f"{float(value):+.2f} $" if value is not None else "—"


def _status(status: str) -> str:
    return f"{STATUS_ICONS.get(status, '⚠️')} {STATUS_LABELS.get(status, status)}"


def _checkpoint(value: object) -> str:
    text = str(value or "")
    for minutes in (120, 90, 60, 30):
        if text.endswith(f"-{minutes}m"):
            return f"{minutes} Min. vor Peak"
    return text or "—"


def _datetime(value: object, zone_name: str | None = None, *, include_date: bool = True) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if zone_name:
            parsed = parsed.astimezone(ZoneInfo(zone_name))
        return parsed.strftime("%d.%m. %H:%M" if include_date else "%H:%M")
    except (TypeError, ValueError, KeyError):
        return str(value)


def _reason_labels(values: list[object] | None) -> str:
    return ", ".join(WARNING_LABELS.get(str(value), str(value)) for value in (values or [])) or "—"


def _condition_value(condition: Mapping[str, Any]) -> str:
    value = condition.get("value")
    field = condition.get("field")
    if field in {"top1_probability", "top1_top2_gap"}:
        return _pct(value)
    if field == "model_spread_c":
        return _temp(value)
    return str(value)


def _is_current_target_state(state: dict, zone_name: str, *, now: datetime) -> bool:
    forecast = state.get("forecast") or {}
    target_date = state.get("target_date") or forecast.get("target_date")
    if not target_date:
        return False
    local_date = now.astimezone(ZoneInfo(zone_name)).date().isoformat()
    return str(target_date)[:10] == local_date


def _is_relevant_display_state(state: dict, zone_name: str, *, now: datetime) -> bool:
    return (
        _is_current_target_state(state, zone_name, now=now)
        and state.get("opportunity_status") != "DATA UNAVAILABLE"
    )


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
    code: state
    for code, state in states.items()
    if code in airports and _is_current_target_state(state, airports[code]["timezone"], now=now)
}
display_states = {
    code: state
    for code, state in today_states.items()
    if _is_relevant_display_state(state, airports[code]["timezone"], now=now)
}

page = st.sidebar.radio(
    "Ansicht",
    ["Heute", "Ergebnisse & Performance", "Regellogik", "Datenstatus"],
)
if payload.get("generated_at"):
    st.sidebar.caption(f"Journal aktualisiert: {_datetime(payload['generated_at'])} UTC")
st.sidebar.caption("Die Madrid-Produktionsengine bleibt unverändert.")

if not payload:
    st.warning(
        "Es ist kein validiertes Scanner-Journal verfügbar. Das ist ein technischer Ausfall und "
        "nicht dasselbe wie ‚Kein Signal‘."
    )


if page == "Heute":
    active = [row for row in display_states.values() if row.get("rule_triggered")]
    high = [row for row in active if row.get("opportunity_status") == "HIGH INTEREST"]
    watch = [row for row in active if row.get("opportunity_status") == "WATCH"]
    best = sorted(
        active,
        key=lambda row: (
            {"HIGH INTEREST": 3, "WATCH": 2, "NO TRADE": 1}.get(row.get("opportunity_status"), 0),
            row.get("conservative_rule_edge") or -99,
        ),
        reverse=True,
    )
    forward_by_rule = {row["rule_id"]: row for row in payload.get("forward_rule_metrics", [])}
    next_checkpoint = min(
        (row.get("next_checkpoint") for row in today_states.values() if row.get("next_checkpoint")),
        default=None,
    )

    cols = st.columns(5)
    cols[0].metric("Ausgelöste Regeln", len(active))
    cols[1].metric("Hohes Interesse", len(high))
    cols[2].metric("Beobachten", len(watch))
    cols[3].metric("Nächstes Signalfenster", f"{_datetime(next_checkpoint)} UTC" if next_checkpoint else "—")
    cols[4].metric(
        "Stärkstes aktuelles Signal",
        f"{best[0]['airport']} · {STATUS_LABELS.get(best[0].get('opportunity_status'), '—')}" if best else "—",
    )

    with st.expander("So liest du die Statusanzeige"):
        st.markdown(
            "- 🟢 **Hohes Interesse:** Regel, Marktqualität und positiver konservativer Edge passen.\n"
            "- 🟡 **Beobachten:** Regel passt, aber Marktpreis oder Marktaktualität reichen noch nicht.\n"
            "- 🔴 **Kein Trade:** Regel passt, aber mindestens ein Handelsfilter scheitert.\n"
            "- ⚪ **Kein Signal:** Daten sind valide, aktuell passt aber keine eingefrorene HC-A/B-Regel.\n"
            "- ⚠️ **Daten fehlen:** Eine belastbare Auswertung war technisch nicht möglich."
        )

    st.subheader("Aktueller Überblick über sechs Flughäfen")
    rows = []
    for code, config in airports.items():
        zone_name = config["timezone"]
        state = today_states.get(code, {})
        state_forecast = state.get("forecast", {})
        display_state = display_states.get(code, {})
        forecast = display_state.get("forecast", {})
        peak = forecast.get("expected_peak") or {}
        forward = forward_by_rule.get(display_state.get("rule_id"), {})
        status = state.get("opportunity_status", "DATA UNAVAILABLE")
        rows.append(
            {
                "Flughafen": code,
                "Ortszeit": now.astimezone(ZoneInfo(zone_name)).strftime("%d.%m. %H:%M"),
                "Zieltag": state_forecast.get("target_date") or state.get("target_date") or "—",
                "Datenstand": _datetime(state_forecast.get("generated_at"), zone_name),
                "Status": _status(status),
                "Checkpoint": _checkpoint(display_state.get("checkpoint") or forecast.get("checkpoint")),
                "Aktuell": _temp(forecast.get("current_temperature_c")),
                "Prognosezentrum": _temp(forecast.get("forecast_center_c")),
                "Top-Bucket": forecast.get("modal_bucket", "—"),
                "Top1": _pct(forecast.get("top1_probability")),
                "Abstand Top1–2": _pct(forecast.get("top1_top2_gap")),
                "Zweiter Bucket": f"{forecast.get('top2_bucket', '—')} · {_pct(forecast.get('top2_probability'))}",
                "Modellstreuung": _temp(forecast.get("model_spread_c")),
                "Marktpreis (Ask)": _pct(display_state.get("best_ask")),
                "Konservativer Edge": _pct(display_state.get("conservative_rule_edge")),
                "Erwarteter Peak": _datetime(peak.get("expected_peak_local"), zone_name),
                "Regime": ", ".join(forecast.get("regimes", [])) or "—",
                "Klasse": display_state.get("hc_class") or "—",
                "Regel": display_state.get("rule_id") or "—",
                "Historie N / Treffer": (
                    f"{display_state['historical_rule_evidence']['validation_n']} / "
                    f"{_pct(display_state['historical_rule_evidence']['validation_accuracy'])}"
                    if display_state.get("historical_rule_evidence")
                    else "—"
                ),
                "Forward N / Treffer": (
                    f"{forward.get('forward_n', 0)} / {_pct(forward.get('accuracy'))}"
                    if display_state.get("rule_id")
                    else "—"
                ),
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption(
        "Top-Bucket und Wahrscheinlichkeiten beziehen sich auf den angezeigten Zieltag. "
        "Modellstreuung ist die Uneinigkeit der Wettermodelle – nicht der Bid-Ask-Spread des Marktes."
    )

    st.subheader("Ausgelöste Regeln im Detail")
    if not active:
        st.info("Im aktuellen validierten Journal wurde keine HC-A/B-Regel ausgelöst.")
    for row in best:
        forecast = row.get("forecast", {})
        label = STATUS_LABELS.get(row.get("opportunity_status"), row.get("opportunity_status", "—"))
        with st.expander(
            f"{STATUS_ICONS.get(row.get('opportunity_status'), '⚠️')} {row['airport']} · "
            f"{label} · {row['hc_class']}",
            expanded=row is best[0],
        ):
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Top-Bucket", f"{row['bucket']} °C")
            c2.metric("Top1-Wahrscheinlichkeit", _pct(forecast.get("top1_probability")))
            c3.metric("Historische Trefferquote", _pct(row["historical_rule_evidence"]["validation_accuracy"]))
            c4.metric("Marktpreis (Ask)", _pct(row.get("best_ask")))
            c5.metric("Konservativer Edge", _pct(row.get("conservative_rule_edge")))
            st.write("**Regel:**", row.get("rule_id") or "—")
            st.write("**Wetterregime:**", ", ".join(forecast.get("regimes", [])) or "—")
            st.write("**Erwarteter Peak:**", _datetime((forecast.get("expected_peak") or {}).get("expected_peak_local")))
            st.write("**Begründung/Blocker:**", _reason_labels(row.get("opportunity_reason_codes")))
            st.caption("Historische Forschung und echte sequenzielle Forward-Ergebnisse werden strikt getrennt.")


elif page == "Ergebnisse & Performance":
    st.subheader("Ergebnisse seit Aktivierung")
    st.write(
        "Hier wird geprüft, ob angezeigte Gelegenheiten im echten Forward-Betrieb tatsächlich "
        "den richtigen Markt-Bucket getroffen hätten. Historische Tests werden nicht zu diesen Zahlen addiert."
    )
    metrics = payload.get("forward_rule_metrics", [])
    if metrics:
        metric_rows = []
        for row in metrics:
            metric_rows.append(
                {
                    "Regel": row.get("rule_id"),
                    "Fälle": row.get("forward_n", 0),
                    "Gewinne": row.get("wins", 0),
                    "Verluste": row.get("losses", 0),
                    "Trefferquote": _pct(row.get("accuracy")),
                    "Ø Einstiegspreis": _pct(row.get("average_entry_price")),
                    "Ø Edge": _pct(row.get("average_edge")),
                    "P&L (Shadow)": _money(row.get("pnl_usd")),
                    "ROI": _pct(row.get("roi")),
                    "Max. Drawdown": _money(-float(row["max_drawdown_usd"])) if row.get("max_drawdown_usd") is not None else "—",
                }
            )
        st.dataframe(pd.DataFrame(metric_rows), hide_index=True, width="stretch")
    else:
        st.info(
            "Noch keine offiziell aufgelösten Shadow Trades. Ergebnisse erscheinen erst nach "
            "offizieller Marktauflösung und Import des Settlement-Buckets."
        )

    st.subheader("Signalhistorie")
    history = pd.DataFrame(payload.get("opportunity_signals", []))
    if history.empty:
        st.info("Seit der Aktivierung wurde noch kein unveränderliches Forward-Signal gespeichert.")
    else:
        trade_by_signal = {row["signal_id"]: row for row in payload.get("shadow_trades", [])}
        resolution_by_trade = {row["shadow_trade_id"]: row for row in payload.get("shadow_resolutions", [])}
        history["Ergebnis"] = history.signal_id.map(
            lambda signal_id: (
                "Gewonnen"
                if resolution_by_trade.get((trade_by_signal.get(signal_id) or {}).get("shadow_trade_id"), {}).get("win") is True
                else "Verloren"
                if resolution_by_trade.get((trade_by_signal.get(signal_id) or {}).get("shadow_trade_id"), {}).get("win") is False
                else "Offen"
            )
        )
        filters = st.columns(6)
        airport = filters[0].selectbox("Flughafen", ["Alle", *sorted(history.airport.unique())])
        rule = filters[1].selectbox("Regel", ["Alle", *sorted(history.rule_id.unique())])
        hc = filters[2].selectbox("Klasse", ["Alle", *sorted(history.hc_class.unique())])
        season = filters[3].selectbox("Saison", ["Alle", *sorted(history.season.unique())])
        checkpoint = filters[4].selectbox("Checkpoint", ["Alle", *sorted(history.checkpoint.unique())])
        outcome = filters[5].selectbox("Ergebnis", ["Alle", *sorted(history["Ergebnis"].unique())])
        shown = history
        for column, value in (
            ("airport", airport),
            ("rule_id", rule),
            ("hc_class", hc),
            ("season", season),
            ("checkpoint", checkpoint),
            ("Ergebnis", outcome),
        ):
            if value != "Alle":
                shown = shown[shown[column] == value]
        shown = shown[
            ["target_date", "airport", "checkpoint", "rule_id", "hc_class", "opportunity_status", "Ergebnis", "triggered_at"]
        ].rename(
            columns={
                "target_date": "Zieltag",
                "airport": "Flughafen",
                "checkpoint": "Checkpoint",
                "rule_id": "Regel",
                "hc_class": "Klasse",
                "opportunity_status": "Status",
                "triggered_at": "Ausgelöst am",
            }
        )
        shown["Checkpoint"] = shown["Checkpoint"].map(_checkpoint)
        shown["Status"] = shown["Status"].map(_status)
        shown["Ausgelöst am"] = shown["Ausgelöst am"].map(_datetime)
        st.dataframe(shown, hide_index=True, width="stretch")

    portfolio = payload.get("portfolio_metrics", {})
    st.subheader("Portfolio-Überblick")
    portfolio_cols = st.columns(5)
    portfolio_cols[0].metric("Tage mit Opportunity", portfolio.get("days_with_opportunity", 0))
    portfolio_cols[1].metric("Tage mit hohem Interesse", portfolio.get("days_with_high_interest", 0))
    portfolio_cols[2].metric("Tage mit ≥2 Opportunities", portfolio.get("days_with_two_or_more_opportunities", 0))
    portfolio_cols[3].metric("Opportunity-Tage · 30 Tage", portfolio.get("opportunity_days_30", 0))
    portfolio_cols[4].metric("Shadow Trades · 30 Tage", portfolio.get("trades_30", 0))
    contributions = portfolio.get("airport_contribution", [])
    if contributions:
        contribution_rows = pd.DataFrame(contributions).rename(
            columns={
                "airport": "Flughafen",
                "opportunity_days": "Opportunity-Tage",
                "unique_opportunity_days": "Nur durch diesen Airport",
                "overlapping_days": "Tage mit Überschneidung",
                "portfolio_days_lost_without_airport": "Verlorene Portfolio-Tage ohne Airport",
            }
        )
        st.dataframe(contribution_rows, hide_index=True, width="stretch")

    st.subheader("Vom Signal bis zum Ergebnis")
    funnel = payload.get("evidence_funnel", {})
    periods = {
        "Heute": funnel.get("today", {}),
        "Letzte 30 Tage": funnel.get("last_30_days", {}),
        "Gesamt": funnel.get("all", {}),
    }
    stages = [
        ("forecasts", "Prognosen mit passender Regel"),
        ("hc_ab_triggers", "HC-A/B-Auslösungen"),
        ("market_matches", "Passende Bucket-Märkte"),
        ("fresh_quotes", "Ausreichend frische Marktpreise"),
        ("positive_edge", "Positiver konservativer Edge"),
        ("shadow_trades", "Erfasste Shadow Trades"),
        ("resolved", "Offiziell aufgelöst"),
        ("wins", "Gewonnen"),
    ]
    funnel_rows = []
    for key, label in stages:
        funnel_rows.append({"Stufe": label, **{period: values.get(key, 0) for period, values in periods.items()}})
    st.dataframe(pd.DataFrame(funnel_rows), hide_index=True, width="stretch")


elif page == "Regellogik":
    st.subheader("Eingefrorene Opportunity-Regeln v1")
    st.write(
        "Diese Regeln wurden vor dem Forward-Test aus der historischen Analyse ausgewählt. "
        "Sie bleiben unverändert, damit spätere Ergebnisse echte Out-of-Sample-Evidenz darstellen."
    )
    rules = pd.DataFrame(rule_pack["rules"])
    summary = st.columns(4)
    summary[0].metric("Aktive Regeln", len(rules))
    summary[1].metric("HC-A", int((rules.hc_class == "HC-A").sum()))
    summary[2].metric("HC-B", int((rules.hc_class == "HC-B").sum()))
    summary[3].metric("HC-C · nur Anzeige", len(load_display_rules()))
    readable_rules = rules[
        ["rule_id", "airport", "checkpoint", "season", "hc_class", "validation_n", "validation_accuracy"]
    ].rename(
        columns={
            "rule_id": "Regel",
            "airport": "Flughafen",
            "checkpoint": "Checkpoint",
            "season": "Saison",
            "hc_class": "Klasse",
            "validation_n": "Validierungsfälle",
            "validation_accuracy": "Historische Trefferquote",
        }
    )
    readable_rules["Checkpoint"] = readable_rules["Checkpoint"].map(_checkpoint)
    readable_rules["Historische Trefferquote"] = readable_rules["Historische Trefferquote"].map(_pct)
    st.dataframe(readable_rules, hide_index=True, width="stretch")

    selected_id = st.selectbox("Regel im Detail", [rule["rule_id"] for rule in rule_pack["rules"]])
    selected = next(rule for rule in rule_pack["rules"] if rule["rule_id"] == selected_id)
    st.write(
        f"**{selected['airport']} · {_checkpoint(selected['checkpoint'])} · "
        f"{selected['season']} · {selected['hc_class']}**"
    )
    condition_rows = [
        {
            "Merkmal": FIELD_LABELS.get(condition.get("field"), condition.get("field")),
            "Bedingung": OPERATOR_LABELS.get(condition.get("op"), condition.get("op")),
            "Wert": _condition_value(condition),
        }
        for condition in selected.get("selector_conditions", [])
    ]
    st.dataframe(pd.DataFrame(condition_rows), hide_index=True, width="stretch")

    forward = next(
        (row for row in payload.get("forward_rule_metrics", []) if row["rule_id"] == selected_id),
        {"forward_n": 0, "accuracy": None, "pnl_usd": None, "roi": None},
    )
    evidence_rows = [
        {
            "Evidenz": "Historische Discovery",
            "Fälle": selected["discovery_n"],
            "Trefferquote": _pct(selected["discovery_accuracy"]),
            "95%-Intervall": " – ".join(_pct(value) for value in selected["discovery_accuracy_ci95"]),
        },
        {
            "Evidenz": "Historische Validierung",
            "Fälle": selected["validation_n"],
            "Trefferquote": _pct(selected["validation_accuracy"]),
            "95%-Intervall": " – ".join(_pct(value) for value in selected["validation_accuracy_ci95"]),
        },
        {
            "Evidenz": "Sequenzieller Forward Shadow",
            "Fälle": forward.get("forward_n", 0),
            "Trefferquote": _pct(forward.get("accuracy")),
            "95%-Intervall": "—",
        },
    ]
    st.dataframe(pd.DataFrame(evidence_rows), hide_index=True, width="stretch")
    st.caption("Historische Trefferquoten sind Forschungswerte. Sie werden nicht als Forward-Ergebnisse ausgegeben.")

    with st.expander("Technische Prüfdaten"):
        st.write("**Regelpaket-Fingerabdruck (SHA-256):**")
        st.code(payload.get("rule_set_sha256") or rule_pack["canonical_rules_sha256"])
        st.write("**Regel-ID:**", selected_id)
        st.caption("Der Fingerabdruck belegt, dass das eingefrorene Regelpaket nicht verändert wurde.")

    with st.expander("12 HC-C-Regeln · nur Forschungsanzeige"):
        display = pd.DataFrame(load_display_rules())
        rename = {
            "rule_id": "Regel",
            "airport": "Flughafen",
            "checkpoint": "Checkpoint",
            "season": "Saison",
            "hc_class": "Klasse",
            "validation_n": "Validierungsfälle",
            "validation_accuracy": "Historische Trefferquote",
        }
        display = display.rename(columns=rename)
        if "Checkpoint" in display:
            display["Checkpoint"] = display["Checkpoint"].map(_checkpoint)
        if "Historische Trefferquote" in display:
            display["Historische Trefferquote"] = display["Historische Trefferquote"].map(_pct)
        preferred = [column for column in rename.values() if column in display.columns]
        st.dataframe(display[preferred], hide_index=True, width="stretch")


else:
    st.subheader("Datenstatus")
    st.write(
        "Diese Seite zeigt, ob Wettermodelle, METAR-Beobachtungen und Marktpreise rechtzeitig "
        "vorlagen. Sie bewertet nicht die meteorologische Qualität der Prognose."
    )
    rows = []
    valid = 0
    unavailable = 0
    for code, config in airports.items():
        zone_name = config["timezone"]
        state = today_states.get(code, {})
        forecast = state.get("forecast", {})
        status = state.get("opportunity_status") or "DATA UNAVAILABLE"
        if status == "DATA UNAVAILABLE":
            unavailable += 1
        else:
            valid += 1
        rows.append(
            {
                "Flughafen": code,
                "Zieltag": forecast.get("target_date") or state.get("target_date") or "—",
                "Letztes Forecast-Update": _datetime(forecast.get("generated_at"), zone_name),
                "Letzter Wetterwert": _datetime(forecast.get("weather_observed_at"), zone_name),
                "Alter Wetterwert": (
                    f"{float(forecast['weather_source_age_minutes']):.0f} Min."
                    if forecast.get("weather_source_age_minutes") is not None
                    else "—"
                ),
                "Letzter Marktpreis": _datetime((state.get("market_snapshot") or {}).get("snapshot_at"), zone_name),
                "Nächster Checkpoint": _datetime(state.get("next_checkpoint"), zone_name),
                "Status": _status(status),
                "Hinweise": _reason_labels(forecast.get("data_quality_warnings") or state.get("opportunity_reason_codes")),
            }
        )
    health_cols = st.columns(3)
    health_cols[0].metric("Flughäfen mit valider Auswertung", valid)
    health_cols[1].metric("Flughäfen mit fehlenden Daten", unavailable)
    health_cols[2].metric("Journal-Stand", f"{_datetime(payload.get('generated_at'))} UTC")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.info(
        "‚Kein Signal‘ bedeutet: Daten vollständig, aber keine Regel passt. "
        "‚Daten fehlen‘ bedeutet: Der Scanner konnte keine belastbare Entscheidung treffen."
    )
    st.caption("Der Opportunity Scanner besitzt keine Verbindung zur Madrid-Produktionsdatenbank.")
