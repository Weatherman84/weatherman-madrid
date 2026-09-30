#!/usr/bin/env python3
"""Build the compact frozen Forward/Shadow journal from read-only Neon."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text

from weatherman.db import ENGINE, Session
from weatherman.forward_shadow import (
    FORWARD_START_DATE,
    build_forward_shadow_journal,
)


def _existing(url: str | None) -> dict:
    if not url:
        return {}
    try:
        response = httpx.get(url, timeout=15, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise RuntimeError(
            "Existing forward journal is temporarily unavailable; refusing to "
            "reconstruct prior immutable decisions"
        ) from exc
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    payload = response.json()
    if payload.get("classification") != "READ-ONLY FROZEN FORWARD SHADOW JOURNAL":
        raise RuntimeError("Existing forward journal failed classification validation")
    return payload


def _public_base(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None


def _aemet_tmax(base_url: str, target: str) -> float | None:
    today_url = f"{base_url}/aemet-today.json"
    archive_url = f"{base_url}/archive/aemet/{target.replace('-', '/')}.json.gz"
    for url in (today_url, archive_url):
        try:
            response = httpx.get(url, timeout=5, follow_redirects=True)
        except httpx.HTTPError:
            continue
        if response.status_code in {404, 503}:
            continue
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            continue
        if payload.get("local_date") != target:
            continue
        value = (payload.get("physical_tmax") or {}).get("value_c")
        return float(value) if value is not None else None
    return None


def _enrich_aemet(payload: dict, base_url: str | None, end_date: date) -> int:
    if not base_url:
        return 0
    recent = {
        row["target_date"]
        for row in payload.get("forward_records", [])
        if date.fromisoformat(row["target_date"]) >= end_date - timedelta(days=2)
    }
    values = {target: _aemet_tmax(base_url, target) for target in sorted(recent)}
    enriched = 0
    for decision in payload.get("decisions", []):
        outcome = decision.get("outcome")
        value = values.get(decision.get("target_date"))
        if outcome is not None and outcome.get("aemet_tmax") is None and value is not None:
            outcome["aemet_tmax"] = value
            enriched += 1
    return enriched


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("forward-shadow-journal.json"))
    parser.add_argument("--existing-url")
    parser.add_argument("--start-date", type=date.fromisoformat, default=FORWARD_START_DATE)
    parser.add_argument("--end-date", type=date.fromisoformat)
    args = parser.parse_args()

    existing = _existing(args.existing_url)
    effective_end = args.end_date or datetime.now(timezone.utc).date() + timedelta(days=1)
    with Session() as session:
        if ENGINE.dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        payload = build_forward_shadow_journal(
            session,
            existing=existing,
            start_date=args.start_date,
            end_date=args.end_date,
            generated_at=datetime.now(timezone.utc),
        )
        session.rollback()
    payload["log"]["aemet_outcomes_enriched_this_run"] = _enrich_aemet(
        payload, _public_base(args.existing_url), effective_end
    )
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded)
    print(json.dumps({
        "status": "built",
        "output": str(args.output),
        "size_bytes": len(encoded),
        "decision_rows": payload["log"]["decision_rows"],
        "resolved_decision_rows": payload["log"]["resolved_decision_rows"],
        "production_access": "read_only_transaction",
        "writes_production_database": False,
    }, separators=(",", ":")))


if __name__ == "__main__":
    main()
