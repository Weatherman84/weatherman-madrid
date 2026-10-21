from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from weatherman.opportunity_results import (
    FINAL_MARKET_STATUS,
    FINAL_METAR_STATUS,
    bucket_contains,
    build_performance_views,
    build_portfolio_opportunities,
    collect_result_observations,
    evaluate_metar_day,
    normalize_official_market_results,
)


def _journal(*, second_checkpoint: bool = False) -> dict:
    signals = []
    snapshots = []
    trades = []
    for index, rule in enumerate(("rule-a", "rule-b"), start=1):
        signal_id = f"signal-{index}"
        snapshot_id = f"snapshot-{index}"
        trade_id = f"trade-{index}"
        signals.append(
            {
                "signal_id": signal_id,
                "airport": "EPWA",
                "target_date": "2026-10-20",
                "checkpoint": "W60 Peak -90m",
                "rule_id": rule,
                "hc_class": "HC-B",
                "opportunity_status": "HIGH INTEREST",
                "triggered_at": "2026-10-20T12:00:00+00:00",
                "bucket": 14,
                "forecast": {"top1_probability": 0.6},
            }
        )
        snapshots.append(
            {
                "market_snapshot_id": snapshot_id,
                "signal_id": signal_id,
                "market_id": "market-14",
                "event": "warsaw-event",
                "bucket_label": "14°C",
                "bucket_low_c": 14,
                "bucket_high_c": 14,
            }
        )
        trades.append(
            {
                "shadow_trade_id": trade_id,
                "signal_id": signal_id,
                "entry_snapshot_id": snapshot_id,
                "airport": "EPWA",
                "target_date": "2026-10-20",
                "checkpoint": "W60 Peak -90m",
                "bucket": 14,
                "entry_price": 0.4,
                "model_probability": 0.6,
                "estimated_edge": 0.2,
                "stake_usd": 1.0,
                "generated_at": "2026-10-20T12:00:00+00:00",
            }
        )
    if second_checkpoint:
        signal = {**signals[0], "signal_id": "signal-3", "rule_id": "rule-c", "checkpoint": "W60 Peak -30m"}
        snapshot = {**snapshots[0], "market_snapshot_id": "snapshot-3", "signal_id": "signal-3"}
        trade = {
            **trades[0], "shadow_trade_id": "trade-3", "signal_id": "signal-3",
            "entry_snapshot_id": "snapshot-3", "checkpoint": "W60 Peak -30m",
        }
        signals.append(signal)
        snapshots.append(snapshot)
        trades.append(trade)
    return {
        "opportunity_signals": signals,
        "market_snapshots": snapshots,
        "shadow_trades": trades,
        "result_observations": [],
    }


def _complete_metars(target: date, zone_name: str) -> list[dict]:
    zone = ZoneInfo(zone_name)
    local = datetime.combine(target, datetime.min.time(), tzinfo=zone)
    end = datetime.combine(target + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    rows = []
    current = local
    while current < end:
        rows.append(
            {
                "observed_at": current.astimezone(timezone.utc),
                "temp_c": 14 if current.hour == 16 else 10,
            }
        )
        current += timedelta(hours=1)
    return rows


def test_duplicate_rules_are_one_opportunity_but_checkpoints_stay_separate() -> None:
    one = build_portfolio_opportunities(_journal())
    assert len(one) == 1
    assert one[0]["overlapping_rule_count"] == 2
    assert one[0]["rule_ids"] == ["rule-a", "rule-b"]
    assert one[0]["representative_shadow_trade_id"] == "trade-1"
    assert len(build_portfolio_opportunities(_journal(second_checkpoint=True))) == 2


def test_metar_completion_uses_local_day_and_handles_dst() -> None:
    target = date(2026, 10, 25)  # Europe/Warsaw fall-back day has 25 hours.
    result = evaluate_metar_day(
        _complete_metars(target, "Europe/Warsaw"),
        target.isoformat(),
        "Europe/Warsaw",
        now=datetime(2026, 10, 26, 8, tzinfo=timezone.utc),
    )
    assert result["status"] == FINAL_METAR_STATUS
    assert result["coverage"]["accepted_observations"] >= 24


def test_metar_correction_wins_over_original_report_at_same_time() -> None:
    rows = _complete_metars(date(2026, 10, 20), "Europe/Warsaw")
    observed = rows[5]["observed_at"]
    rows.extend(
        [
            {"observed_at": observed, "temp_c": 16, "raw": "METAR EPWA COR 200300Z"},
            {"observed_at": observed, "temp_c": 11, "raw": "METAR EPWA 200300Z"},
        ]
    )
    result = evaluate_metar_day(
        rows, "2026-10-20", "Europe/Warsaw",
        now=datetime(2026, 10, 21, 8, tzinfo=timezone.utc),
    )
    assert result["maximum_c"] == 16


def test_official_result_requires_explicit_resolution_not_a_high_price() -> None:
    market = {
        "id": "market-14",
        "groupItemTitle": "14°C",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
        "closed": True,
    }
    unresolved = normalize_official_market_results(
        {"markets": [market]}, source_url="https://example.test/event"
    )[0]
    assert unresolved["officially_resolved"] is False
    resolved = normalize_official_market_results(
        {"markets": [{**market, "umaResolutionStatus": "resolved"}]},
        source_url="https://example.test/event",
    )[0]
    assert resolved["officially_resolved"] is True
    assert resolved["yes_won"] is True


def test_result_collection_is_idempotent_and_portfolio_pnl_is_deduplicated() -> None:
    journal = _journal()
    now = datetime(2026, 10, 21, 8, tzinfo=timezone.utc)

    def metar_fetcher(_airport: str, hours: int = 48) -> list[dict]:
        assert hours >= 48
        return _complete_metars(date(2026, 10, 20), "Europe/Warsaw")

    def market_fetcher(_airport: dict, _target: date) -> list[dict]:
        return [
            {
                "market_id": "market-14", "bucket_label": "14°C",
                "bucket_low_c": 14, "bucket_high_c": 14,
                "officially_resolved": True, "yes_won": True,
                "source": "https://example.test/official",
            }
        ]

    updates = collect_result_observations(
        journal, now=now, metar_fetcher=metar_fetcher, market_fetcher=market_fetcher
    )
    assert len(updates) == 1
    assert updates[0]["meteorological"]["status"] == FINAL_METAR_STATUS
    assert updates[0]["official_market"]["status"] == FINAL_MARKET_STATUS
    journal["result_observations"] = updates
    assert collect_result_observations(
        journal, now=now + timedelta(hours=1), metar_fetcher=metar_fetcher,
        market_fetcher=market_fetcher,
    ) == []
    view = build_performance_views(journal)
    assert view["summary"]["unique_high_interest_opportunities"] == 1
    assert view["summary"]["meteorologically_completed"] == 1
    assert view["summary"]["officially_settled"] == 1
    assert view["summary"]["settled_stake_usd"] == 1.0
    assert view["summary"]["gross_pnl_usd"] == pytest.approx(1.5)
    assert view["summary"]["gross_roi"] == pytest.approx(1.5)
    assert view["summary"]["meteorological_what_if_gross_pnl_usd"] == pytest.approx(1.5)
    assert len(view["rule_diagnostics"]) == 2


def test_open_bucket_boundaries_are_inclusive() -> None:
    assert bucket_contains(10, None, 10)
    assert not bucket_contains(11, None, 10)
    assert bucket_contains(20, 20, None)
    assert not bucket_contains(19, 20, None)


def test_temporary_provider_failure_does_not_erase_a_final_metar_result() -> None:
    journal = _journal()
    opportunity = build_portfolio_opportunities(journal)[0]
    journal["result_observations"] = [
        {
            "result_observation_id": "prior-result",
            "opportunity_id": opportunity["opportunity_id"],
            "airport": "EPWA", "target_date": "2026-10-20",
            "recorded_at": "2026-10-21T07:00:00+00:00",
            "meteorological": {
                "status": FINAL_METAR_STATUS, "maximum_c": 14,
                "source": "test", "quality_notes": [],
            },
            "official_market": {
                "status": "AWAITING_OFFICIAL_RESOLUTION", "win": None,
                "quality_notes": ["still_open"],
            },
        }
    ]

    def failing_metar(_airport: str, hours: int = 48) -> list[dict]:
        raise TimeoutError

    def still_open(_airport: dict, _target: date) -> list[dict]:
        return [
            {
                "market_id": "market-14", "bucket_label": "14°C",
                "bucket_low_c": 14, "bucket_high_c": 14,
                "officially_resolved": False, "yes_won": None,
            }
        ]

    updates = collect_result_observations(
        journal, now=datetime(2026, 10, 21, 9, tzinfo=timezone.utc),
        metar_fetcher=failing_metar, market_fetcher=still_open,
    )
    assert updates
    assert updates[0]["meteorological"]["status"] == FINAL_METAR_STATUS


def test_results_page_renders_deduplicated_performance(tmp_path: Path, monkeypatch) -> None:
    from streamlit.testing.v1 import AppTest

    journal = _journal()
    opportunity = build_portfolio_opportunities(journal)[0]
    journal.update(
        {
            "classification": "CROSS-AIRPORT RESEARCH-ONLY SHADOW JOURNAL",
            "research_only": True,
            "automatic_promotion": False,
            "automatic_execution": False,
            "writes_production_database": False,
            "contains_credentials": False,
            "generated_at": "2026-10-21T10:00:00+00:00",
            "airport_states": [],
            "forward_rule_metrics": [],
            "portfolio_metrics": {},
            "evidence_funnel": {},
            "result_observations": [
                {
                    "result_observation_id": "result-1",
                    "opportunity_id": opportunity["opportunity_id"],
                    "airport": "EPWA",
                    "target_date": "2026-10-20",
                    "recorded_at": "2026-10-21T10:00:00+00:00",
                    "meteorological": {
                        "status": FINAL_METAR_STATUS, "maximum_c": 14,
                        "source": "test", "quality_notes": [],
                    },
                    "official_market": {
                        "status": FINAL_MARKET_STATUS, "win": True,
                        "winner_bucket_label": "14°C", "source": "test", "quality_notes": [],
                    },
                }
            ],
        }
    )
    journal["performance"] = build_performance_views(journal)
    path = tmp_path / "journal.json"
    path.write_text(json.dumps(journal), encoding="utf-8")
    monkeypatch.delenv("OPPORTUNITY_SCANNER_JOURNAL_URL", raising=False)
    monkeypatch.setenv("OPPORTUNITY_SCANNER_JOURNAL_PATH", str(path))
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "opportunity_scanner_app.py")
    ).run(timeout=10)
    app.sidebar.radio[0].set_value("Ergebnisse & Performance").run(timeout=10)
    assert not app.exception
    assert any("Unterschiedliche High-Interest-Opportunities" in metric.label for metric in app.metric)
