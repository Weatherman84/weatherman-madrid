"""Research-only notification selection for new scanner opportunities.

The notification policy is deliberately separate from the frozen selector and
trading policy.  It only decides which already-created, immutable opportunities
are surfaced to the repository owner.
"""
from __future__ import annotations

from typing import Any, Mapping

from .opportunity_results import build_portfolio_opportunities


NOTIFICATION_POLICY_VERSION = "opportunity_push_policy_v1"
JOURNAL_CLASSIFICATION = "CROSS-AIRPORT RESEARCH-ONLY SHADOW JOURNAL"


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_alertable(opportunity: Mapping[str, Any]) -> bool:
    """Return whether an immutable opportunity matches the user alert policy."""
    airport = str(opportunity.get("airport") or "").upper()
    if airport == "LTFM":
        return True
    edge = _number(opportunity.get("estimated_edge"))
    return (
        airport == "EPWA"
        and opportunity.get("opportunity_status") == "HIGH INTEREST"
        and edge is not None
        and edge > 0
    )


def _representative_signal(
    opportunity: Mapping[str, Any], signals: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    members = [signals[signal_id] for signal_id in opportunity.get("signal_ids", []) if signal_id in signals]
    return min(
        members,
        key=lambda row: (str(row.get("triggered_at") or ""), str(row.get("signal_id") or "")),
        default={},
    )


def select_new_notification_candidates(
    previous: Mapping[str, Any], current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Select new deduplicated opportunities without backfilling old signals.

    A missing/invalid prior journal initializes the notification baseline and
    intentionally emits nothing.  This prevents deployment from generating a
    burst of historical alerts.
    """
    if previous.get("classification") != JOURNAL_CLASSIFICATION:
        return []
    if current.get("classification") != JOURNAL_CLASSIFICATION:
        raise ValueError("Current Opportunity Scanner journal failed classification validation")

    prior_ids = {
        str(row["opportunity_id"])
        for row in build_portfolio_opportunities(previous)
    }
    signals = {
        str(row["signal_id"]): row
        for row in current.get("opportunity_signals", [])
        if row.get("signal_id")
    }
    candidates: list[dict[str, Any]] = []
    for opportunity in build_portfolio_opportunities(current):
        opportunity_id = str(opportunity["opportunity_id"])
        if opportunity_id in prior_ids or not _is_alertable(opportunity):
            continue
        signal = _representative_signal(opportunity, signals)
        ask = _number(signal.get("best_ask"))
        edge = _number(opportunity.get("estimated_edge"))
        probability = _number(opportunity.get("model_probability"))
        candidates.append(
            {
                "notification_policy_version": NOTIFICATION_POLICY_VERSION,
                "opportunity_id": opportunity_id,
                "airport": opportunity.get("airport"),
                "target_date": opportunity.get("target_date"),
                "checkpoint": opportunity.get("checkpoint"),
                "triggered_at": opportunity.get("triggered_at"),
                "predicted_bucket": opportunity.get("predicted_bucket"),
                "bucket_label": opportunity.get("bucket_label"),
                "model_probability": probability,
                "best_ask": ask,
                "conservative_edge": edge,
                "edge_class": "EDGE" if edge is not None and edge > 0 else "NO_EDGE",
                "opportunity_status": opportunity.get("opportunity_status"),
                "overlapping_rule_count": opportunity.get("overlapping_rule_count"),
                "rule_ids": opportunity.get("rule_ids", []),
                "research_only": True,
                "automatic_execution": False,
            }
        )
    return sorted(
        candidates,
        key=lambda row: (
            str(row.get("triggered_at") or ""),
            str(row.get("airport") or ""),
            str(row.get("opportunity_id") or ""),
        ),
    )


def format_github_comment(candidate: Mapping[str, Any]) -> str:
    """Format one compact, idempotently identifiable GitHub push message."""
    airport = str(candidate.get("airport") or "—")
    edge = _number(candidate.get("conservative_edge"))
    probability = _number(candidate.get("model_probability"))
    ask = _number(candidate.get("best_ask"))
    edge_label = "positiver Edge" if edge is not None and edge > 0 else "ohne positiven Edge"
    probability_label = "—" if probability is None else f"{probability * 100:.1f} %"
    ask_label = "—" if ask is None else f"{ask:.3f} USD"
    edge_value = "—" if edge is None else f"{edge * 100:+.1f} Prozentpunkte"
    bucket = candidate.get("bucket_label") or candidate.get("predicted_bucket") or "—"
    rules = int(candidate.get("overlapping_rule_count") or 0)
    opportunity_id = str(candidate["opportunity_id"])
    return "\n".join(
        [
            f"<!-- opportunity-alert:{opportunity_id} -->",
            f"## 🔔 {airport} Opportunity · {edge_label}",
            "",
            f"- **Zieltag:** {candidate.get('target_date') or '—'}",
            f"- **Checkpoint:** {candidate.get('checkpoint') or '—'}",
            f"- **Prognostizierter Bucket:** {bucket}",
            f"- **Modellwahrscheinlichkeit:** {probability_label}",
            f"- **Gespeicherter Best Ask:** {ask_label}",
            f"- **Konservativer Edge:** {edge_value}",
            f"- **Überlappende Regeln:** {rules}",
            "",
            "Research-only Shadow-Signal · keine automatische Ausführung.",
        ]
    )
