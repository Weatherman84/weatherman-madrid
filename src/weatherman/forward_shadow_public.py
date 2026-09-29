"""Small cached reader for the Cloudflare Forward/Shadow journal."""
from __future__ import annotations

from typing import Any

import httpx
import streamlit as st


@st.cache_data(ttl=300, max_entries=4, show_spinner=False)
def fetch_forward_shadow_journal(public_base_url: str) -> dict[str, Any]:
    base = str(public_base_url or "").strip().rstrip("/")
    if not base:
        return {}
    try:
        response = httpx.get(
            f"{base}/forward-shadow-journal.json",
            timeout=httpx.Timeout(0.5, connect=0.25),
            follow_redirects=True,
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return {}
    valid = (
        payload.get("airport") == "LEMD"
        and payload.get("classification")
        == "READ-ONLY FROZEN FORWARD SHADOW JOURNAL"
        and payload.get("research_only") is True
        and payload.get("automatic_promotion") is False
        and payload.get("writes_production_database") is False
    )
    return payload if valid else {}
