from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime_bootstrap import discard_stale_weatherman_modules

discard_stale_weatherman_modules("1.0.7")

import pandas as pd
import streamlit as st

from weatherman.aemet_live import (
    archive_path as aemet_archive_path,
    curve_rows as aemet_curve_rows,
    fetch_public_aemet_json,
    normalized_public_base_url,
)
from weatherman.aemet_metar_shadow import build_shadow_diagnostics
from weatherman.analytics import (
    detect_market_model_conflict,
    fixed_checkpoint_reliability,
)
from weatherman.catalog import trading_airports
from weatherman.db import (
    DailyActual,
    Forecast,
    ForecastSnapshot,
    ForecastVariantSnapshot,
    HourlyForecast,
    MarketSnapshot,
    Observation,
    ProviderCall,
    Session,
    SignalSnapshot,
    TafReport,
    init_db,
)
from weatherman.decision import (
    build_trade_decision,
    latest_prior_probabilities,
)
from weatherman.history import read_archive_live
from weatherman.service import (
    build_current_live_nowcast,
    collect_live_trading_refresh,
    collect_research_checkpoints,
)
from weatherman.settings import settings
from weatherman.terminology import EVIDENCE_GLOSSARY, FRESHNESS_GLOSSARY


st.set_page_config(
    page_title="Weatherman Madrid",
    page_icon="🌡️",
    layout="wide",
)


def local_time(value: object, zone: str, *, seconds: bool = False) -> str:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return "—"
    pattern = "%d.%m.%Y %H:%M:%S LT" if seconds else "%d.%m.%Y %H:%M LT"
    return pd.Timestamp(parsed).tz_convert(zone).strftime(pattern)


def temperature(value: object, *, digits: int = 1) -> str:
    number = pd.to_numeric(value, errors="coerce")
    return f"{float(number):.{digits}f} °C" if pd.notna(number) else "—"


def percentage(value: object, *, digits: int = 0) -> str:
    number = pd.to_numeric(value, errors="coerce")
    return f"{float(number):.{digits}%}" if pd.notna(number) else "—"


def latest_market_frame(markets: pd.DataFrame) -> pd.DataFrame:
    if markets.empty:
        return markets
    result = markets.copy()
    result["captured_at"] = pd.to_datetime(result.captured_at, utc=True, errors="coerce")
    return result.sort_values("captured_at").drop_duplicates("market_id", keep="last")


def load_madrid_data(target, airport: str, zone: str) -> dict[str, pd.DataFrame]:
    target_start = datetime(target.year, target.month, target.day, tzinfo=ZoneInfo(zone))
    target_start_utc = target_start.astimezone(timezone.utc)
    target_end_utc = target_start_utc + timedelta(days=1)
    with Session() as session:
        bind = session.connection()
        return {
            "forecasts": read_archive_live(
                Forecast,
                bind,
                filters={"airport": airport},
                minimums={"target_date": target - timedelta(days=90)},
            ),
            "actuals": read_archive_live(
                DailyActual,
                bind,
                filters={"airport": airport},
                minimums={"target_date": target - timedelta(days=400)},
            ),
            "observations": read_archive_live(
                Observation,
                bind,
                filters={"airport": airport},
                minimums={"observed_at": target_start_utc - timedelta(days=2)},
                maximums={"observed_at": target_end_utc},
            ),
            "hourly": read_archive_live(
                HourlyForecast,
                bind,
                filters={"airport": airport},
                minimums={"valid_at": target_start_utc},
                maximums={"valid_at": target_end_utc},
            ),
            "markets": read_archive_live(
                MarketSnapshot,
                bind,
                filters={"airport": airport, "target_date": target},
            ),
            "signals": read_archive_live(
                SignalSnapshot,
                bind,
                filters={"airport": airport, "target_date": target},
            ),
            "snapshots": read_archive_live(
                ForecastSnapshot,
                bind,
                filters={"airport": airport},
                minimums={"target_date": target - timedelta(days=120)},
            ),
            "variants": read_archive_live(
                ForecastVariantSnapshot,
                bind,
                filters={"airport": airport},
                minimums={"target_date": target - timedelta(days=90)},
            ),
            "tafs": read_archive_live(
                TafReport,
                bind,
                filters={"airport": airport},
                minimums={"issue_time": target_start_utc - timedelta(days=2)},
            ),
            "provider_calls": read_archive_live(
                ProviderCall,
                bind,
                filters={"airport": airport},
                minimums={"attempted_at": target_start_utc - timedelta(days=1)},
            ),
        }


def checkpoint_rows(
    snapshots: pd.DataFrame,
    *,
    target,
    zone: str,
    labels: list[str],
) -> pd.DataFrame:
    if snapshots.empty:
        selected = pd.DataFrame()
    else:
        selected = snapshots.copy()
        selected["target_date"] = pd.to_datetime(
            selected.target_date, errors="coerce"
        ).dt.date
        selected["captured_at"] = pd.to_datetime(
            selected.captured_at, utc=True, errors="coerce"
        )
        selected = selected[
            selected.target_date.eq(target) & selected.checkpoint_label.isin(labels)
        ].sort_values("captured_at").drop_duplicates("checkpoint_label", keep="last")
    by_label = {
        str(row.checkpoint_label): row for row in selected.itertuples()
    } if not selected.empty else {}
    rows = []
    for label in labels:
        row = by_label.get(label)
        recorded_at = getattr(row, "checkpoint_recorded_at", None) if row else None
        if recorded_at is None or bool(pd.isna(recorded_at)):
            recorded_at = getattr(row, "captured_at", None) if row else None
        reconstructed = bool(
            row and getattr(row, "checkpoint_reconstructed", False)
        )
        hours_to_peak = pd.to_numeric(
            getattr(row, "hours_to_peak", None) if row else None,
            errors="coerce",
        )
        late_post_peak = bool(
            row
            and label.startswith(("First Live", "Late Live"))
            and pd.notna(hours_to_peak)
            and float(hours_to_peak) < 0
        )
        rows.append(
            {
                "Checkpoint": label,
                "Champion": temperature(
                    getattr(row, "final_forecast_c", None) if row else None
                ),
                "Recorded": local_time(
                    recorded_at,
                    zone,
                ),
                "METAR used": local_time(
                    getattr(row, "latest_metar_at", None) if row else None,
                    zone,
                ),
                "Evidence": (
                    "reconstructed"
                    if reconstructed
                    else "late/post-peak"
                    if late_post_peak
                    else "scheduled"
                    if row and "scheduled" in str(getattr(row, "checkpoint_status", ""))
                    else "missing"
                ),
                "Freshness": (
                    str(getattr(row, "freshness_status", None) or "unavailable")
                    if row
                    else "unavailable"
                ),
            }
        )
    return pd.DataFrame(rows)


@st.cache_data(ttl=60, show_spinner=False)
def load_aemet_live(base_url: str) -> dict:
    return fetch_public_aemet_json(base_url, "aemet-live.json")


@st.cache_data(max_entries=24, show_spinner=False)
def load_aemet_curve(base_url: str, path: str, version: str) -> dict:
    del version  # The observation timestamp is deliberately part of the cache key.
    return fetch_public_aemet_json(base_url, path)


@st.fragment(run_every=timedelta(minutes=5))
def render_aemet_station_panel(
    target_date,
    metar_records: list[dict],
    timezone_name: str,
) -> None:
    st.subheader("AEMET 3129 · Physical station observations")
    base_url = normalized_public_base_url(settings.aemet_public_base_url)
    if base_url is None:
        st.info(
            "AEMET live data is not configured yet. Add the credential-free Worker "
            "origin as AEMET_PUBLIC_BASE_URL in Streamlit Secrets."
        )
        return

    today_local = datetime.now(ZoneInfo(timezone_name)).date()
    live: dict = {}
    try:
        if target_date == today_local:
            live = load_aemet_live(base_url)
            version = str(
                (live.get("latest_observation") or {}).get("observed_at")
                or live.get("last_successful_fetch_at")
                or "initial"
            )
            day = load_aemet_curve(base_url, "aemet-today.json", version)
        else:
            path = aemet_archive_path(target_date)
            day = load_aemet_curve(base_url, path, target_date.isoformat())
    except Exception as exc:
        st.warning(f"AEMET data unavailable: {type(exc).__name__}: {exc}")
        return

    latest = live.get("latest_observation") or day.get("latest_observation") or {}
    physical_max = day.get("physical_tmax") or live.get("physical_tmax") or {}
    diagnostics = build_shadow_diagnostics(day, metar_records)
    ground_truth = diagnostics["ground_truth"]
    provider_status = str(live.get("provider_status") or "archived")
    freshness = str(live.get("freshness_status") or "archived")
    age = pd.to_numeric(live.get("data_age_minutes"), errors="coerce")
    age_label = f"{float(age):.0f} min" if pd.notna(age) else "—"

    a1, a2, a3, a4, a5, a6 = st.columns(6)
    a1.metric("AEMET now", temperature(latest.get("temperature_c")))
    a2.metric("Physical Tmax", temperature(physical_max.get("value_c")))
    a3.metric(
        "Tmax time",
        local_time(physical_max.get("observed_at"), timezone_name),
    )
    a4.metric("Latest observation", local_time(latest.get("observed_at"), timezone_name))
    a5.metric("Data age", age_label)
    a6.metric("AEMET status", f"{freshness} · {provider_status}")

    metar_max = (ground_truth.get("stored_metar_max") or {}).get("value_c")
    daily_gap = ground_truth.get("daily_max_series_gap_c")
    g1, g2, g3 = st.columns(3)
    g1.metric("Stored METAR max", temperature(metar_max, digits=0))
    g2.metric("AEMET physical Tmax", temperature(physical_max.get("value_c")))
    g3.metric("Daily max series gap", temperature(daily_gap))
    st.caption(
        "The daily max series gap is AEMET physical Tmax minus the highest stored "
        "LEMD METAR value. It can combine station, sensor, timing and reporting "
        "differences and is not a calibrated sensor bias."
    )

    physical_shadow = diagnostics["physical_stall"]
    persistence_shadow = diagnostics["metar_bucket_persistence"]
    s1, s2 = st.columns(2)
    s1.metric(
        "Physical stall · SHADOW",
        str(physical_shadow.get("stall_level") or "insufficient_data"),
    )
    s1.caption(str(physical_shadow.get("interpretation") or "—"))
    s2.metric(
        "METAR bucket persistence · SHADOW",
        str(persistence_shadow.get("persistence_state") or "insufficient_data"),
    )
    s2.caption(str(persistence_shadow.get("interpretation") or "—"))
    next_metar_at = persistence_shadow.get("next_nominal_metar_at")
    if next_metar_at:
        s2.caption(f"Next nominal routine METAR: {local_time(next_metar_at, timezone_name)}")
    st.caption(
        "Both diagnostics are uncalibrated RESEARCH ONLY signals. Probability is "
        "intentionally unavailable until sufficient sequential OOS evidence exists; "
        "Champion impact is always 0.0 °C."
    )
    comparisons = ground_truth.get("time_aligned_series_comparisons") or []
    if comparisons:
        with st.expander("Time-aligned AEMET/METAR series comparisons"):
            comparison_rows = []
            for item in comparisons:
                comparison_rows.append(
                    {
                        "METAR time": local_time(
                            item.get("metar_observed_at"), timezone_name
                        ),
                        "METAR °C": item.get("metar_temperature_c"),
                        "AEMET time": local_time(
                            item.get("aemet_observed_at"), timezone_name
                        ),
                        "AEMET °C": item.get("aemet_temperature_c"),
                        "Time gap min": item.get("timestamp_gap_minutes"),
                        "AEMET − METAR K": item.get("aemet_minus_metar_c"),
                    }
                )
            st.dataframe(pd.DataFrame(comparison_rows), hide_index=True, width="stretch")
            st.caption(
                "These are nearby readings from distinct data series, not sensor "
                "calibration pairs."
            )
    st.info(
        "Market bucket boundary unavailable: the Polymarket resolution source and "
        "rounding rule have not been confirmed."
    )

    if provider_status == "failed":
        st.warning(
            "The latest AEMET fetch failed; the last successful physical observation "
            "remains visible. Other providers and the Champion are unaffected."
        )
    st.caption(
        "Last successful AEMET fetch: "
        f"{local_time(live.get('last_successful_fetch_at'), timezone_name)} · "
        "last attempt: "
        f"{local_time(live.get('last_attempt_at'), timezone_name)}."
    )

    chart_rows = aemet_curve_rows(
        day,
        metar_records,
        timezone_name=timezone_name,
    )
    if chart_rows:
        chart_spec = {
            "data": {"values": chart_rows},
            "height": 320,
            "layer": [
                {
                    "transform": [
                        {"filter": "datum.series === 'AEMET 3129 (physical)'"}
                    ],
                    "mark": {"type": "line", "point": True, "color": "#e45756"},
                    "encoding": {
                        "x": {
                            "field": "timestamp",
                            "type": "temporal",
                            "title": "Madrid local time",
                            "axis": {"format": "%H:%M"},
                        },
                        "y": {
                            "field": "temperature_c",
                            "type": "quantitative",
                            "title": "Temperature (°C)",
                            "scale": {"zero": False},
                        },
                        "tooltip": [
                            {"field": "timestamp", "type": "temporal", "title": "Time"},
                            {
                                "field": "temperature_c",
                                "type": "quantitative",
                                "title": "AEMET °C",
                                "format": ".1f",
                            },
                        ],
                    },
                },
                {
                    "transform": [
                        {"filter": "datum.series === 'LEMD METAR (integer)'"}
                    ],
                    "mark": {
                        "type": "point",
                        "filled": True,
                        "shape": "diamond",
                        "size": 65,
                        "color": "#4c78a8",
                    },
                    "encoding": {
                        "x": {"field": "timestamp", "type": "temporal"},
                        "y": {"field": "temperature_c", "type": "quantitative"},
                        "tooltip": [
                            {"field": "timestamp", "type": "temporal", "title": "Time"},
                            {
                                "field": "temperature_c",
                                "type": "quantitative",
                                "title": "METAR °C",
                                "format": ".0f",
                            },
                        ],
                    },
                },
                {
                    "transform": [
                        {"filter": "datum.series === 'AEMET physical Tmax'"}
                    ],
                    "mark": {
                        "type": "point",
                        "filled": True,
                        "shape": "triangle-up",
                        "size": 180,
                        "color": "#f2cf5b",
                        "stroke": "#7a5b00",
                    },
                    "encoding": {
                        "x": {"field": "timestamp", "type": "temporal"},
                        "y": {"field": "temperature_c", "type": "quantitative"},
                        "tooltip": [
                            {"field": "timestamp", "type": "temporal", "title": "Tmax time"},
                            {
                                "field": "temperature_c",
                                "type": "quantitative",
                                "title": "Physical Tmax °C",
                                "format": ".1f",
                            },
                        ],
                    },
                },
            ],
        }
        st.vega_lite_chart(chart_spec, width="stretch")
    else:
        st.info("No AEMET curve observations are available for this date.")

    st.caption(
        "AEMET 3129 is an independent physical station series with decimal values. "
        "It may differ from the LEMD METAR sensor, site, timing or processing. It does "
        "not replace LEMD METAR and is not used as the Polymarket resolution Actual "
        "until the market source and rounding rule are confirmed. This panel "
        "updates every five minutes without recalculating the Champion."
    )


if not settings.database_url.startswith(("postgres://", "postgresql://")):
    st.error(
        "Safety stop: this Madrid deployment requires the pooled Neon PostgreSQL "
        "DATABASE_URL in Streamlit Secrets. A local SQLite fallback is intentionally "
        "not used in production."
    )
    st.stop()

try:
    init_db()
except Exception as exc:
    st.error(
        "Database connection failed. Check DATABASE_URL in Streamlit Secrets. "
        f"Technical detail: {type(exc).__name__}: {exc}"
    )
    st.stop()

catalog = trading_airports()
if set(catalog) != {"LEMD"}:
    st.error("Safety stop: this deployment must contain LEMD as its only trading airport.")
    st.stop()

airport_code = "LEMD"
airport = catalog[airport_code]
timezone_name = str(airport["timezone"])
local_today = datetime.now(ZoneInfo(timezone_name)).date()

st.title("Weatherman Madrid")
st.caption(
    "One-airport production cockpit · Engine v10.7.11 with cadence-aware model "
    "freshness · Neon/PostgreSQL persistence"
)

target = st.sidebar.date_input("Target date", value=local_today)
st.sidebar.caption("All displayed operational times use Europe/Madrid local time.")

if st.sidebar.button("Refresh Madrid now", type="primary", use_container_width=True):
    manual_refresh_started = time.perf_counter()
    try:
        with st.spinner("Refreshing models, METAR, TAF and Polymarket…"):
            refresh_result = collect_live_trading_refresh(airport_code, target)
            checkpoint_started = time.perf_counter()
            collect_research_checkpoints(
                [airport_code],
                window_minutes=35,
                catchup_hours=48,
                sync_universe=False,
            )
            checkpoint_elapsed_seconds = time.perf_counter() - checkpoint_started
    except Exception as exc:
        st.sidebar.error(f"Refresh failed: {type(exc).__name__}: {exc}")
    else:
        errors = dict(refresh_result.get("errors") or {})
        message = (
            f"Completed in {time.perf_counter() - manual_refresh_started:.1f}s · "
            f"providers {float(refresh_result.get('provider_elapsed_seconds', 0)):.1f}s · "
            f"Neon {float(refresh_result.get('storage_elapsed_seconds', 0)):.1f}s · "
            f"checkpoint {checkpoint_elapsed_seconds:.1f}s · "
            f"models refreshed {int(refresh_result.get('models_refreshed', 0))} · "
            f"manual-live saved "
            f"{int(refresh_result.get('manual_live_snapshots', 0))} · "
            f"Meteoblue {refresh_result.get('meteoblue_status', 'not configured')}"
        )
        if errors:
            st.sidebar.warning(message + " · failed: " + ", ".join(sorted(errors)))
        else:
            st.sidebar.success(message)

data = load_madrid_data(target, airport_code, timezone_name)
metar_curve_records = (
    data["observations"][["observed_at", "temp_c"]].to_dict("records")
    if not data["observations"].empty
    else []
)
render_aemet_station_panel(target, metar_curve_records, timezone_name)
markets = latest_market_frame(data["markets"])
now = datetime.now(timezone.utc)
nowcast = build_current_live_nowcast(
    airport=airport,
    target=target,
    captured_at=now,
    forecasts=data["forecasts"],
    actuals=data["actuals"],
    observations=data["observations"],
    hourly=data["hourly"],
    markets=markets,
    tafs=data["tafs"],
    snapshots=data["snapshots"],
    variants=data["variants"],
)

checkpoint_labels = [
    str(item["label"]) for item in airport.get("decision_checkpoints_local") or []
]
fixed_checkpoint_table = checkpoint_rows(
    data["snapshots"],
    target=target,
    zone=timezone_name,
    labels=checkpoint_labels,
)

if nowcast is None:
    st.warning(
        "No Champion can be built: fewer than two fresh model sources are available. "
        "Stale sources are excluded rather than silently reused."
    )
    st.dataframe(
        fixed_checkpoint_table,
        hide_index=True,
        width="stretch",
    )
    st.stop()

probabilities = dict(nowcast.probabilities)
prior_probabilities = latest_prior_probabilities(data["signals"], target)
market_conflict = detect_market_model_conflict(probabilities, markets)
material_adjustments = {
    name: value
    for name, value in nowcast.adjustment_contributions.items()
    if name != "total" and abs(float(value)) >= 0.05
}
trade_decision = build_trade_decision(
    probabilities=probabilities,
    markets=markets,
    forecast_confidence=nowcast.forecast_confidence,
    day_status=nowcast.day_status,
    metar_pending=nowcast.metar_pending,
    market_model_conflict=market_conflict.is_conflict,
    forecast_stale=nowcast.forecast_data_stale,
    previous_probabilities=prior_probabilities,
    live_signals=[
        f"{name.replace('_', ' ')} {float(value):+.2f} °C"
        for name, value in sorted(
            material_adjustments.items(), key=lambda item: abs(float(item[1])), reverse=True
        )[:3]
    ],
    recommendations_enabled=settings.edge_recommendations_enabled,
)

if nowcast.forecast_data_stale:
    st.error("MODEL DATA STALE · do not use for a trading decision.")
elif nowcast.metar_pending:
    st.error("METAR DUE · wait for the routine report before deciding.")
elif nowcast.stale_models:
    st.warning("Excluded stale models: " + ", ".join(nowcast.stale_models))

top_bucket = max(probabilities, key=probabilities.get)
top_probability = float(probabilities[top_bucket])

st.subheader("1 · Current Madrid decision state")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Champion expected maximum", temperature(nowcast.final_forecast_mean))
m2.metric("Latest METAR", temperature(nowcast.current_observed_temp))
m3.metric("METAR max so far", temperature(nowcast.observed_max, digits=0))
m4.metric(
    "Temperature trend",
    f"{float(nowcast.heating_rate):+.1f} °C/h" if nowcast.heating_rate is not None else "—",
)
first_live_row = fixed_checkpoint_table[
    fixed_checkpoint_table.Checkpoint.eq("First Live @12:00")
]
m5.metric(
    "First Live @12:00",
    str(first_live_row.iloc[0].Champion) if not first_live_row.empty else "—",
)
m5.caption(
    str(first_live_row.iloc[0].Recorded) if not first_live_row.empty else "not stored yet"
)
st.caption(
    f"Calculated {local_time(now, timezone_name, seconds=True)} · latest METAR used "
    f"{local_time(nowcast.latest_observation_at, timezone_name)} · "
    f"day status: {nowcast.day_status.label}."
)

left, right = st.columns([1.1, 1.3])
with left:
    st.markdown("**Forecast chain**")
    st.dataframe(
        pd.DataFrame(
            [
                {"Stage": "Raw ensemble", "Forecast": temperature(nowcast.raw_model_mean)},
                {"Stage": "Bias-corrected", "Forecast": temperature(nowcast.corrected.mean)},
                {
                    "Stage": "Live weather-adjusted",
                    "Forecast": temperature(nowcast.metar_conditioned_mean),
                },
                {"Stage": "Champion", "Forecast": temperature(nowcast.final_forecast_mean)},
            ]
        ),
        hide_index=True,
        width="stretch",
    )
with right:
    st.markdown("**Champion reliability by fixed checkpoint**")
    reliability = fixed_checkpoint_reliability(data["snapshots"], data["actuals"])
    shown = reliability.copy()
    shown["Exact bucket"] = shown.exact_bucket.map(percentage)
    shown["±1 °C"] = shown.within_1c.map(percentage)
    shown["MAE"] = shown.mae.map(
        lambda value: f"{float(value):.2f} K" if pd.notna(value) else "—"
    )
    shown["Through"] = shown.data_through.map(
        lambda value: value.strftime("%d.%m.%Y") if value else "—"
    )
    st.dataframe(
        shown[["checkpoint", "Exact bucket", "±1 °C", "MAE", "n", "Through"]].rename(
            columns={"checkpoint": "Checkpoint", "n": "N"}
        ),
        hide_index=True,
        width="stretch",
    )
    with st.expander("Why does N increase or stay unchanged?", expanded=False):
        coverage = shown[
            [
                "checkpoint",
                "scheduled_days",
                "reconstructed_days",
                "late_post_peak_days",
                "missing_days",
                "provisional_days",
            ]
        ].rename(
            columns={
                "checkpoint": "Checkpoint",
                "scheduled_days": "Scheduled/final",
                "reconstructed_days": "Reconstructed",
                "late_post_peak_days": "Late/post-peak",
                "missing_days": "Missing",
                "provisional_days": "Provisional Actuals",
            }
        )
        st.dataframe(coverage, hide_index=True, width="stretch")
        st.caption(
            "N increases only for a final station Actual paired with a scheduled, "
            "pre-peak checkpoint. Reconstructed, late/post-peak, missing and provisional "
            "days stay visible but are not counted."
        )

st.subheader("2 · Fixed decision checkpoints")
st.dataframe(
    fixed_checkpoint_table,
    hide_index=True,
    width="stretch",
)

st.subheader("3 · Relevant buckets")
bucket_rows = [
    {"Bucket": f"{int(bucket)} °C", "Champion probability": percentage(probability, digits=1)}
    for bucket, probability in sorted(probabilities.items())
    if float(probability) >= 0.005 or abs(int(bucket) - int(top_bucket)) <= 2
]
st.dataframe(pd.DataFrame(bucket_rows), hide_index=True, width="stretch")
st.caption(
    f"Most likely exact bucket: {top_bucket} °C ({top_probability:.1%}). "
    "Buckets are always sorted by temperature, not probability."
)

with st.expander("Trading context · research only", expanded=False):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", trade_decision.status)
    c2.metric("Selected bucket", trade_decision.bucket_label or "—")
    c3.metric("YES ask", percentage(trade_decision.buy_price, digits=1))
    c4.metric("Uncalibrated gap", percentage(trade_decision.edge, digits=1))
    for blocker in trade_decision.blockers:
        st.write(f"• {blocker}")
    st.caption(
        "Forecast confidence describes the weather forecast information set, not the "
        "certainty of one specific bet. Edge and wagering remain RESEARCH ONLY."
    )

with st.expander("Model, TAF and Meteoblue diagnostics", expanded=False):
    if not nowcast.model_freshness.empty:
        freshness = nowcast.model_freshness.copy()
        display_columns = [
            column
            for column in ["model", "max_temp_c", "age_minutes", "is_fresh", "source"]
            if column in freshness
        ]
        st.dataframe(freshness[display_columns], hide_index=True, width="stretch")
    if not data["provider_calls"].empty:
        calls = data["provider_calls"].copy()
        calls["Attempted"] = calls.attempted_at.map(
            lambda value: local_time(value, timezone_name)
        )
        st.markdown("**Meteoblue checkpoint calls**")
        st.dataframe(
            calls[["checkpoint_label", "Attempted", "status", "rows_written"]].rename(
                columns={
                    "checkpoint_label": "Checkpoint",
                    "status": "Status",
                    "rows_written": "Rows",
                }
            ),
            hide_index=True,
            width="stretch",
        )
    if not data["tafs"].empty:
        latest_taf = data["tafs"].sort_values("issue_time").iloc[-1]
        st.markdown("**Latest TAF**")
        st.code(str(latest_taf.raw_taf))
        st.caption(
            f"Issued {local_time(latest_taf.issue_time, timezone_name)} · TAF remains a "
            "separate forecast stage."
        )

with st.expander("Terminology and evidence glossary", expanded=False):
    st.markdown(
        "- **Scheduled:** fixed checkpoint captured within its allowed operational window, "
        "using only information available by the target time.\n"
        "- **Reconstructed:** generated later from causally eligible stored inputs. It is not "
        "counted as genuine sequential OOS evidence.\n"
        "- **Late/post-peak:** the checkpoint occurred after the modeled or observed peak.\n"
        "- **Missing:** no usable checkpoint exists.\n"
        "- **N:** final station Actuals paired with scheduled, pre-peak checkpoints only."
    )
    for label, description in EVIDENCE_GLOSSARY.items():
        st.write(f"**{label}:** {description}")
    for label, description in FRESHNESS_GLOSSARY.items():
        st.write(f"**{label}:** {description}")

st.caption(
    "Fixed Madrid checkpoints: D−1 @20:00 · D0 @09:00 · First Live @12:00 · "
    "Late Live @16:00. Meteoblue is attempted once per checkpoint window, maximum four "
    "times per local day."
)
