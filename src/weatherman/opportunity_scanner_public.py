"""Bounded reader for a published Opportunity Scanner shadow journal."""
from __future__ import annotations

from typing import Any

import httpx
import streamlit as st


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
