"""Bounded reader for a published Opportunity Scanner shadow journal."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import httpx
import streamlit as st


def is_current_target_state(
    state: Mapping[str, Any], zone_name: str, *, now: datetime | None = None,
) -> bool:
    """Return whether a state belongs to the airport's current local target date."""
    forecast = state.get("forecast") or {}
    target_date = state.get("target_date") or forecast.get("target_date")
    if not target_date:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_date = current.astimezone(ZoneInfo(zone_name)).date().isoformat()
    return str(target_date)[:10] == local_date


def is_relevant_display_state(
    state: Mapping[str, Any], zone_name: str, *, now: datetime | None = None,
) -> bool:
    """Hide forecast evidence when the current-day state is operationally unavailable."""
    return (
        is_current_target_state(state, zone_name, now=now)
        and state.get("opportunity_status") != "DATA UNAVAILABLE"
    )


@st.cache_data(ttl=180, max_entries=2, show_spinner=False)
def fetch_opportunity_scanner_journal(url: str) -> dict[str, Any]:
    if not str(url or "").strip():
        return {}
    try:
        response = httpx.get(str(url).strip(), timeout=httpx.Timeout(3, connect=1), follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return {}
    safe = (
        payload.get("classification") == "CROSS-AIRPORT RESEARCH-ONLY SHADOW JOURNAL"
        and payload.get("research_only") is True
        and payload.get("automatic_promotion") is False
        and payload.get("automatic_execution") is False
        and payload.get("writes_production_database") is False
        and payload.get("contains_credentials") is False
    )
    return payload if safe else {}
