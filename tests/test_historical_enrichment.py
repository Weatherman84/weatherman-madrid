import json
from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest

from weatherman.historical_archive import ArchiveError, RawCache, eligible
from weatherman.historical_enrichment import ogimet_rows, policy_engine, shift_availability
from weatherman.nowcast import build_live_nowcast


META = {"fetched_at": "2026-10-10T06:00:00Z", "sha256": "fixture"}


def test_ogimet_retains_issue_revision_and_month_boundary():
    data = b"LEMD,2026,04,30,23,00,TAF AMD LEMD 302300Z 0100/0206 VRB05KT CAVOK TX24/0116Z TN09/0105Z BECMG 0108/0110 20010KT TEMPO 0113/0119 22015G25KT=\n"
    row = ogimet_rows(data, META).iloc[0]
    assert row.issue_time == pd.Timestamp("2026-04-30T23:00Z")
    assert row.valid_from == pd.Timestamp("2026-05-01T00:00Z")
    assert row.max_temp_at == pd.Timestamp("2026-05-01T16:00Z")
    assert row.is_amended and row.max_temp_c == 24
    periods = json.loads(row.periods_json)
    assert len(periods) == 3 and periods[1]["change"] == "BECMG"
    assert periods[2]["wind_gust_kt"] == 25
    assert eligible(
        pd.DataFrame([row]), pd.Timestamp("2026-04-30T23:04Z"), policy="conservative"
    ).empty
    assert (
        len(eligible(pd.DataFrame([row]), pd.Timestamp("2026-04-30T23:05Z"), policy="conservative"))
        == 1
    )
    assert eligible(pd.DataFrame([row]), pd.Timestamp("2026-05-01T00:00Z"), policy="strict").empty


def test_temporary_groups_do_not_replace_prevailing_state():
    raw = b"LEMD,2026,07,14,05,00,TAF LEMD 140500Z 1406/1512 01005KT CAVOK TEMPO 1407/1409 03015G25KT BKN020 BECMG 1410/1412 20010KT=\n"
    periods = json.loads(ogimet_rows(raw, META).iloc[0].periods_json)
    assert periods[0]["time_to"] == "2026-07-14T12:00:00+00:00"
    assert periods[1]["time_to"] == "2026-07-14T09:00:00+00:00"
    assert periods[2]["clouds"][0]["cover"] == "SKC"
    assert periods[2]["wind_gust_kt"] is None


def test_invalid_original_change_dates_fail_closed_without_fabrication():
    raw = b"LEMD,2026,07,05,17,00,TAF LEMD 051700Z 0518/0624 VRB05KT CAVOK TX38/0616Z TEMPO 0315/0320 25010KT=\n"
    row = ogimet_rows(raw, META).iloc[0]
    assert row.max_temp_c == 38 and row.periods_json is None
    assert not row.decoded_periods_available and row.decode_error


def test_receipt_sensitivity_changes_only_assumed_times():
    frame = pd.DataFrame(
        {
            "available_at": pd.to_datetime(["2026-07-15T06:10Z"] * 2),
            "availability_basis": [
                "estimated_conservative_schedule",
                "verified_historical_availability",
            ],
        }
    )
    result = shift_availability(frame, 60)
    assert result.available_at.iloc[0] == pd.Timestamp("2026-07-15T07:10Z")
    assert result.available_at.iloc[1] == frame.available_at.iloc[1]
    assert frame.available_at.iloc[0] == pd.Timestamp("2026-07-15T06:10Z")


def test_isolated_identity_engine_matches_native_and_does_not_monkeypatch():
    as_of = datetime(2026, 7, 15, 7, tzinfo=timezone.utc)
    forecasts = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "model": model,
                "run_at": pd.Timestamp("2026-07-15T06:10Z"),
                "available_at": pd.Timestamp("2026-07-15T06:10Z"),
                "fetched_at": pd.Timestamp("2026-10-10T06:00Z"),
                "model_run_at": pd.Timestamp("2026-07-15T00:00Z"),
                "target_date": as_of.date(),
                "max_temp_c": maximum,
                "source": "open-meteo",
                "horizon": "D0",
            }
            for model, maximum in [("gfs_global", 35.0), ("ecmwf_ifs025", 36.0)]
        ]
    )
    kwargs = dict(
        forecasts=forecasts,
        actuals=pd.DataFrame(),
        observations=pd.DataFrame(),
        hourly=pd.DataFrame(),
        markets=pd.DataFrame(),
        timezone_name="Europe/Madrid",
        target=as_of.date(),
        as_of=as_of,
        _build_challengers=False,
    )
    native = build_live_nowcast(**kwargs)
    identity = policy_engine("{}")(**kwargs)
    assert native is not None and identity is not None
    assert native.probabilities == identity.probabilities
    assert native.final_forecast_mean == identity.final_forecast_mean
    assert (
        __import__("weatherman.nowcast", fromlist=["build_live_nowcast"]).build_live_nowcast
        is build_live_nowcast
    )


def test_archive_rejects_any_trading_post(tmp_path):
    cache = RawCache(tmp_path)
    with pytest.raises(ArchiveError, match="read-only"):
        cache.get("https://clob.polymarket.com/order", {}, kind="market_json", read_only_post=True)
    cache.close()


def test_price_series_post_is_cached_by_body(tmp_path):
    cache = RawCache(tmp_path)
    cache.client.close()
    cache.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"history": {"x": [{"t": 1, "p": 0.2}]}})
        )
    )
    url = "https://clob.polymarket.com/batch-prices-history"
    params = {"markets": ["x"], "fidelity": 5}
    _, meta = cache.get(url, params, kind="market_json", read_only_post=True)
    assert meta["method"] == "POST" and cache.requests == 1
    cache.get(url, params, kind="market_json", read_only_post=True)
    assert cache.requests == 1
    cache.close()


def test_unknown_run_time_requires_explicit_conservative_calibration_bound():
    frame = pd.DataFrame(
        {
            "model_run_at": [pd.NaT],
            "available_at": [pd.Timestamp("2026-01-02T05:10Z")],
            "availability_basis": ["estimated_fixed_lead48_plus_publication_delay"],
            "model_run_upper_bound_at": [pd.Timestamp("2026-01-01T23:00Z")],
            "calibration_evidence": ["fixed_48h_lead_sensitivity_not_exact_d1_run"],
        }
    )
    assert len(eligible(frame, pd.Timestamp("2026-01-02T06:00Z"), policy="conservative")) == 1
    assert eligible(frame, pd.Timestamp("2026-01-02T06:00Z"), policy="strict").empty
    frame.loc[0, "calibration_evidence"] = "native_run"
    with pytest.raises(ArchiveError):
        eligible(frame, pd.Timestamp("2026-01-02T06:00Z"), policy="conservative")
    frame.loc[0, "calibration_evidence"] = "fixed_48h_lead_sensitivity_not_exact_d1_run"
    frame.loc[0, "model_run_upper_bound_at"] = pd.Timestamp("2026-01-02T06:30Z")
    with pytest.raises(ArchiveError):
        eligible(frame, pd.Timestamp("2026-01-02T06:00Z"), policy="conservative")


def test_market_history_uses_only_points_before_checkpoint():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "historical_market_prices",
        Path(__file__).resolve().parents[1] / "scripts/historical_market_prices.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cutoff = pd.Timestamp("2026-07-15T10:00Z")
    ts = int(cutoff.timestamp())
    point = module.historical_point([{"t": ts - 300, "p": 0.3}, {"t": ts + 60, "p": 0.9}], cutoff)
    assert point["series_price"] == 0.3 and point["age_minutes"] == 5
    assert not point["executable_quote"]
    assert module.historical_point([{"t": ts + 60, "p": 0.9}], cutoff) is None
