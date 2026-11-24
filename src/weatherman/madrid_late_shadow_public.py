"""Bounded, fail-closed public reader; the UI never computes a Shadow forecast."""

import httpx

from .madrid_late_shadow_journal import validate_decision
from .madrid_late_shadow_model import PARAMETER_SHA, VERSION

PUBLIC_JOURNAL = "https://raw.githubusercontent.com/Weatherman84/weatherman-madrid/madrid-v11-shadow-data/journal.json"


def validate_journal(payload):
    if (payload.get("schema") != "madrid_late_prospective_shadow_v1"
            or payload.get("candidate_id") != VERSION or payload.get("airport") != "LEMD"
            or payload.get("parameter_sha256") != PARAMETER_SHA
            or payload.get("research_only") is not True
            or payload.get("automatic_promotion") is not False
            or payload.get("automatic_execution") is not False):
        raise ValueError("Unsupported live Shadow journal")
    seen = set()
    for record in payload["decisions"]:
        validate_decision(record)
        if record["target_date"] in seen:
            raise ValueError("Duplicate live Shadow day")
        seen.add(record["target_date"])
    return payload


def fetch_journal():
    try:
        with httpx.stream("GET", PUBLIC_JOURNAL, timeout=5, follow_redirects=True) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 25_000_000:
                    raise ValueError("Oversized Shadow journal")
                chunks.append(chunk)
        import json
        return validate_journal(json.loads(b"".join(chunks)))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {"status": "live_journal_unavailable", "decisions": []}
