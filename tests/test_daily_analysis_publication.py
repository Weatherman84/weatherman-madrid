from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.prepare_daily_analysis_publication import (
    prepare_publication,
    publication_metadata,
)
from scripts.verify_daily_analysis_publication import _validate_bytes


def payload() -> dict:
    return {
        "airport": "LEMD",
        "classification": "READ-ONLY DAILY ANALYSIS EXPORT",
        "contains_credentials": False,
        "writes_production_database": False,
        "research_only": True,
        "generated_at": "2026-09-16T19:30:00+00:00",
        "application_version": "1.0.13",
        "actuals": [
            {"target_date": "2026-09-15", "is_final_station_actual": True},
            {"target_date": "2026-09-16", "is_final_station_actual": True},
        ],
    }


def test_prepare_publication_reuses_exact_bytes_for_dated_alias(tmp_path: Path) -> None:
    source = tmp_path / "daily-analysis-latest.json"
    raw = (json.dumps(payload(), ensure_ascii=False, indent=2) + "\n").encode()
    source.write_bytes(raw)
    metadata = prepare_publication(source, tmp_path)
    dated = tmp_path / "daily-analysis" / "2026-09-16.json"
    assert dated.read_bytes() == raw
    assert metadata["target_date"] == "2026-09-16"
    assert metadata["size_bytes"] == len(raw)
    assert metadata["sha256"] == hashlib.sha256(raw).hexdigest()
    manifest = json.loads((tmp_path / "daily-analysis-publication.json").read_text())
    assert manifest["sha256"] == metadata["sha256"]


def test_publication_validation_rejects_unsafe_export(tmp_path: Path) -> None:
    unsafe = payload()
    unsafe["contains_credentials"] = True
    source = tmp_path / "unsafe.json"
    source.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(ValueError, match="contains_credentials"):
        publication_metadata(source)


def test_download_validation_requires_exact_bytes_and_safety_fields() -> None:
    raw = json.dumps(payload()).encode()
    digest = hashlib.sha256(raw).hexdigest()
    _validate_bytes(
        raw, sha256=digest, size=len(raw), target_date="2026-09-16",
        generated_at="2026-09-16T19:30:00+00:00",
    )
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _validate_bytes(
            raw, sha256="0" * 64, size=len(raw), target_date="2026-09-16",
            generated_at="2026-09-16T19:30:00+00:00",
        )
