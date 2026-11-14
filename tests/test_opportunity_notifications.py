from __future__ import annotations

from copy import deepcopy

from weatherman.opportunity_notifications import (
    JOURNAL_CLASSIFICATION,
    format_github_comment,
    select_new_notification_candidates,
)


def _signal(
    signal_id: str,
    airport: str,
    *,
    edge: float,
    status: str,
    market_id: str,
    rule_id: str = "rule-a",
) -> tuple[dict, dict]:
    signal = {
        "signal_id": signal_id,
        "airport": airport,
        "target_date": "2026-11-14",
        "checkpoint": "W60 Peak -120m",
        "rule_id": rule_id,
        "hc_class": "HC-A",
        "opportunity_status": status,
        "triggered_at": "2026-11-14T08:00:00+00:00",
        "bucket": 17,
        "best_ask": 0.52,
        "conservative_rule_edge": edge,
        "forecast": {"top1_probability": 0.61},
    }
    snapshot = {
        "market_snapshot_id": f"snapshot-{signal_id}",
        "signal_id": signal_id,
        "market_id": market_id,
        "event": f"event-{airport.lower()}",
        "bucket_label": "17°C",
        "bucket_low_c": 17,
        "bucket_high_c": 17,
    }
    return signal, snapshot


def _journal(*pairs: tuple[dict, dict]) -> dict:
    return {
        "classification": JOURNAL_CLASSIFICATION,
        "opportunity_signals": [pair[0] for pair in pairs],
        "market_snapshots": [pair[1] for pair in pairs],
        "shadow_trades": [],
        "result_observations": [],
    }


def test_notification_policy_alerts_ltfm_with_or_without_edge_and_epwa_edge_only() -> None:
    ltfm_a = _signal("ltfm-a", "LTFM", edge=-0.05, status="NO TRADE", market_id="ltfm-17")
    ltfm_b = _signal(
        "ltfm-b", "LTFM", edge=-0.05, status="NO TRADE", market_id="ltfm-17", rule_id="rule-b",
    )
    epwa_edge = _signal("epwa-edge", "EPWA", edge=0.12, status="HIGH INTEREST", market_id="epwa-17")
    epwa_no_edge = _signal("epwa-no-edge", "EPWA", edge=-0.02, status="NO TRADE", market_id="epwa-18")
    eham_edge = _signal("eham-edge", "EHAM", edge=0.20, status="HIGH INTEREST", market_id="eham-17")

    selected = select_new_notification_candidates(
        _journal(),
        _journal(ltfm_a, ltfm_b, epwa_edge, epwa_no_edge, eham_edge),
    )

    assert [(row["airport"], row["edge_class"]) for row in selected] == [
        ("EPWA", "EDGE"),
        ("LTFM", "NO_EDGE"),
    ]
    assert selected[1]["overlapping_rule_count"] == 2


def test_existing_opportunity_is_not_notified_again() -> None:
    ltfm = _signal("ltfm-a", "LTFM", edge=-0.05, status="NO TRADE", market_id="ltfm-17")
    previous = _journal(ltfm)
    assert select_new_notification_candidates(previous, deepcopy(previous)) == []


def test_missing_prior_journal_initializes_without_historical_alert_burst() -> None:
    ltfm = _signal("ltfm-a", "LTFM", edge=-0.05, status="NO TRADE", market_id="ltfm-17")
    assert select_new_notification_candidates({}, _journal(ltfm)) == []


def test_github_comment_contains_stable_deduplication_marker() -> None:
    ltfm = _signal("ltfm-a", "LTFM", edge=-0.05, status="NO TRADE", market_id="ltfm-17")
    candidate = select_new_notification_candidates(_journal(), _journal(ltfm))[0]
    comment = format_github_comment(candidate)
    assert f"<!-- opportunity-alert:{candidate['opportunity_id']} -->" in comment
    assert "LTFM Opportunity" in comment
    assert "ohne positiven Edge" in comment
    assert "keine automatische Ausführung" in comment
