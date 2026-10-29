from __future__ import annotations

import httpx
import pytest

from scripts import run_opportunity_scanner as runner


def test_existing_journal_retries_a_transient_read_timeout(monkeypatch) -> None:
    attempts = 0
    sleeps: list[int] = []

    def fake_get(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ReadTimeout("temporary timeout")
        return httpx.Response(
            200,
            json={"classification": "CROSS-AIRPORT RESEARCH-ONLY SHADOW JOURNAL"},
            request=httpx.Request("GET", "https://example.test/journal.json"),
        )

    monkeypatch.setattr(runner.httpx, "get", fake_get)
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)

    payload = runner._fetch_existing_journal("https://example.test/journal.json")

    assert attempts == 3
    assert sleeps == [1, 2]
    assert payload["classification"] == "CROSS-AIRPORT RESEARCH-ONLY SHADOW JOURNAL"


def test_existing_journal_fails_closed_after_bounded_retries(monkeypatch) -> None:
    monkeypatch.setattr(
        runner.httpx,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            httpx.ReadTimeout("persistent timeout")
        ),
    )
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        runner._fetch_existing_journal("https://example.test/journal.json")
