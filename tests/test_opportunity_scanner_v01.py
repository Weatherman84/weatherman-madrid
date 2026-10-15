from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pytest

from weatherman.opportunity_portfolio import evidence_funnel, portfolio_metrics, run_scanner_cycle
from weatherman.opportunity_scanner import (
    checkpoint_schedule, expected_peak_time, load_airport_config, load_display_rules,
    load_rule_pack, market_freshness, matching_rules, resolve_shadow_trade,
    validate_causal_forecast,
)
from weatherman.opportunity_store import OpportunityStore
from weatherman.opportunity_scanner_public import (
    is_current_target_state, is_relevant_display_state,
)


def history(target: date, count: int = 20, minute: int = 15 * 60) -> list[dict]:
    return [{"target_date": (target - timedelta(days=offset)).isoformat(),
             "actual_first_peak_minute": minute, "eligible": True}
            for offset in range(count, 0, -1)]


def lemd_forecast(target: date = date(2026, 10, 12)) -> dict:
    return {
        "airport": "LEMD", "target_date": target.isoformat(),
        "checkpoint": "W60 Peak -30m", "generated_at": "2026-10-12T12:30:00+00:00",
        "weather_observed_at": "2026-10-12T12:25:00+00:00",
        "weather_source_age_minutes": 5, "current_temperature_c": 24.0,
        "forecast_center_c": 25.1, "modal_bucket": 25, "top1_bucket": 25,
        "top1_probability": 0.60, "top2_bucket": 24, "top2_probability": 0.25,
        "top1_top2_gap": 0.35, "model_spread_c": 0.3,
        "regimes": ["Late Dry Mixing"],
        "models": ["ecmwf_ifs025", "gfs_global", "icon_global"],
    }


def market() -> dict:
    return {
        "airport": "LEMD", "target_date": "2026-10-12", "bucket": 25,
        "event": "highest-temperature-in-madrid-on-october-12-2026", "market_id": "m25",
        "best_bid": 0.45, "best_ask": 0.50, "midpoint": 0.475, "spread": 0.05,
        "last_price": 0.48, "series_price": 0.48, "liquidity": 100,
        "snapshot_at": "2026-10-12T12:25:00+00:00", "market_status": "open",
    }


def test_frozen_rule_assets_are_exact_and_classified() -> None:
    pack = load_rule_pack()
    assert pack["canonical_rules_sha256"] == "b08f9e5be300a46dc82eebd8b5f09d35ae480799ea2ccbd6d5e816b65cbd4420"
    assert len(pack["rules"]) == 24
    assert sum(rule["hc_class"] == "HC-A" for rule in pack["rules"]) == 4
    assert sum(rule["hc_class"] == "HC-B" for rule in pack["rules"]) == 20
    assert len(load_display_rules()) == 12


def test_every_frozen_rule_can_be_reproduced_from_selector_fields() -> None:
    season_dates = {"DJF": "2026-01-15", "MAM": "2026-04-15",
                    "JJA": "2026-07-15", "SON": "2026-10-15", "ALL": "2026-10-15"}
    for rule in load_rule_pack()["rules"]:
        snapshot = {
            "airport": rule["airport"], "target_date": season_dates[rule["season"]],
            "checkpoint": rule["checkpoint"], "generated_at": "2026-10-15T12:00:00+00:00",
            "modal_bucket": 20, "top1_probability": 0.9, "top1_top2_gap": 0.9,
            "model_spread_c": 0.1,
            "regimes": [condition["value"] for condition in rule["selector_conditions"]
                        if condition["field"] == "regimes"],
        }
        assert rule["rule_id"] in {row["rule_id"] for row in matching_rules(snapshot)}


def test_peak_is_prior_only_and_dst_safe() -> None:
    target = date(2026, 3, 29)
    result = expected_peak_time(history(target), target, "Europe/Madrid")
    assert result["available"] is True
    assert result["history_days"] == 20
    peak = datetime.fromisoformat(result["expected_peak_local"])
    assert peak.utcoffset() == timedelta(hours=2)
    assert peak.hour == 15
    schedule = checkpoint_schedule(result["expected_peak_local"])
    assert [row["minutes_before_expected_peak"] for row in schedule] == [120, 90, 60, 30]
    assert schedule[-1]["checkpoint_utc"].startswith("2026-03-29T12:30:00+00:00")


def test_peak_requires_20_history_days_and_ignores_current_day() -> None:
    target = date(2026, 10, 12)
    rows = history(target, 19) + [{"target_date": target.isoformat(), "actual_first_peak_minute": 1}]
    assert expected_peak_time(rows, target, "Europe/Madrid")["available"] is False


def test_outcome_leakage_is_rejected() -> None:
    snapshot = lemd_forecast()
    snapshot["actual_bucket"] = 25
    with pytest.raises(ValueError, match="Outcome leakage"):
        validate_causal_forecast(snapshot)


@pytest.mark.parametrize("age,expected", [(15, "fresh"), (16, "acceptable"),
                                            (30, "acceptable"), (31, "stale"),
                                            (60, "stale"), (61, "very_stale")])
def test_market_freshness_boundaries(age: int, expected: str) -> None:
    generated = datetime(2026, 10, 12, 12, 30, tzinfo=timezone.utc)
    assert market_freshness(generated - timedelta(minutes=age), generated)[0] == expected


def test_cycle_creates_immutable_signal_quote_and_one_dollar_shadow_trade(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "scanner.db")
    states = run_scanner_cycle(
        forecasts=[lemd_forecast()], peak_history={"LEMD": history(date(2026, 10, 12))},
        markets=[market()], store=store,
    )
    assert states[0]["opportunity_status"] == "HIGH INTEREST"
    journal = store.journal(generated_at=datetime(2026, 10, 12, tzinfo=timezone.utc))
    assert len(journal["opportunity_signals"]) == 1
    assert len(journal["market_snapshots"]) == 1
    assert len(journal["shadow_trades"]) == 1
    assert journal["shadow_trades"][0]["stake_usd"] == 1.0
    assert journal["shadow_trades"][0]["real_money"] is False
    run_scanner_cycle(forecasts=[lemd_forecast()],
                      peak_history={"LEMD": history(date(2026, 10, 12))},
                      markets=[market()], store=store)
    assert len(store.journal()["opportunity_signals"]) == 1


def test_immutable_conflict_and_resolution_pnl(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "scanner.db")
    states = run_scanner_cycle(forecasts=[lemd_forecast()],
                               peak_history={"LEMD": history(date(2026, 10, 12))},
                               markets=[market()], store=store)
    trade = states[0]["shadow_trade"]
    won = resolve_shadow_trade(trade, 25, "official-polymarket", resolved_at="2026-10-13T00:00:00Z",
                               metar_actual_bucket=25)
    lost = resolve_shadow_trade(trade, 24, "official-polymarket", resolved_at="2026-10-13T00:00:00Z",
                                metar_actual_bucket=25)
    assert won["hypothetical_pnl_usd"] == pytest.approx(1.0)
    assert lost["hypothetical_pnl_usd"] == -1.0
    assert won["metar_is_market_resolution"] is False
    assert store.append_resolution(won) is True
    with pytest.raises(Exception):
        store.append_resolution(lost)


def test_warnings_block_high_interest_and_missing_market_is_watch(tmp_path: Path) -> None:
    stale = lemd_forecast()
    stale["weather_source_age_minutes"] = 91
    first = run_scanner_cycle(forecasts=[stale], peak_history={"LEMD": history(date(2026, 10, 12))},
                              markets=[market()], store=OpportunityStore(tmp_path / "a.db"))[0]
    assert first["opportunity_status"] == "NO TRADE"
    second = run_scanner_cycle(forecasts=[lemd_forecast()], peak_history={"LEMD": history(date(2026, 10, 12))},
                               markets=[], store=OpportunityStore(tmp_path / "b.db"))[0]
    assert second["opportunity_status"] == "WATCH"


def test_operational_failure_is_not_reported_as_no_signal(tmp_path: Path) -> None:
    forecast = lemd_forecast()
    forecast["checkpoint"] = "W60 Peak -120m"
    forecast["regimes"] = []
    forecast["weather_source_age_minutes"] = 999
    state = run_scanner_cycle(forecasts=[forecast],
                              peak_history={"LEMD": history(date(2026, 10, 12))},
                              markets=[], store=OpportunityStore(tmp_path / "scanner.db"))[0]
    assert state["rule_triggered"] is False
    assert state["opportunity_status"] == "DATA UNAVAILABLE"


def test_missed_checkpoint_never_persists_forward_oos_signal(tmp_path: Path) -> None:
    forecast = lemd_forecast()
    forecast["generated_at"] = "2026-10-12T18:00:00+00:00"
    store = OpportunityStore(tmp_path / "scanner.db")
    state = run_scanner_cycle(
        forecasts=[forecast], peak_history={"LEMD": history(date(2026, 10, 12))},
        markets=[market()], store=store,
    )[0]
    assert state["opportunity_status"] == "DATA UNAVAILABLE"
    assert "checkpoint_missed" in state["opportunity_reason_codes"]
    assert store.journal()["opportunity_signals"] == []


def test_portfolio_and_funnel_keep_forward_evidence_separate(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "scanner.db")
    run_scanner_cycle(forecasts=[lemd_forecast()], peak_history={"LEMD": history(date(2026, 10, 12))},
                      markets=[market()], store=store)
    journal = store.journal()
    metrics = portfolio_metrics(journal, as_of=datetime(2026, 10, 12, tzinfo=timezone.utc))
    assert metrics["days_with_opportunity"] == 1
    assert evidence_funnel(journal)["shadow_trades"] == 1
    assert "historical_rule_evidence" in journal["opportunity_signals"][0]
    assert journal["opportunity_signals"][0]["evidence_class"] == "sequential_oos_shadow"


def test_scanner_ui_hides_unavailable_and_prior_day_forecast_values() -> None:
    now = datetime(2026, 10, 15, 8, 45, tzinfo=timezone.utc)
    unavailable = {
        "target_date": "2026-10-15", "opportunity_status": "DATA UNAVAILABLE",
        "forecast": {"target_date": "2026-10-15", "modal_bucket": 20},
    }
    assert is_current_target_state(unavailable, "Europe/Vienna", now=now) is True
    assert is_relevant_display_state(unavailable, "Europe/Vienna", now=now) is False

    available = {**unavailable, "opportunity_status": "NO SIGNAL"}
    assert is_relevant_display_state(available, "Europe/Vienna", now=now) is True

    prior_day = {**available, "target_date": "2026-10-14",
                 "forecast": {**available["forecast"], "target_date": "2026-10-14"}}
    assert is_current_target_state(prior_day, "Europe/Vienna", now=now) is False
    assert is_relevant_display_state(prior_day, "Europe/Vienna", now=now) is False


def test_scanner_isolation_from_madrid_database() -> None:
    source = Path("src/weatherman/opportunity_store.py").read_text(encoding="utf-8")
    runner = Path("scripts/run_opportunity_scanner.py").read_text(encoding="utf-8")
    assert "from .db" not in source
    assert "DATABASE_URL" not in source + runner
    assert "weatherman.db" in runner
    assert set(load_airport_config()["airports"]) == {"LEMD", "EHAM", "EDDM", "EPWA", "LTAC", "LTFM"}
