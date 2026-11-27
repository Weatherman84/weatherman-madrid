"""A research display must never silently turn archive values into live forecasts."""

from copy import deepcopy
from datetime import date
import hashlib
import json

import pytest

from weatherman.madrid_late_shadow_display import (
    DATA,
    bucket_shadow_value,
    checkpoint_shadow_value,
    comparison_rows,
    load_bundle,
    modal,
    table_view_for_target,
    view_for_target,
)


def test_frozen_archive_and_fallback_population():
    bundle = load_bundle()
    assert len(bundle["cases"]) == 434
    assert sum(r["curve_available"] for r in bundle["cases"]) == 393
    assert bundle["benchmark"]["candidate"]["n"] == 333
    assert not bundle["automatic_promotion"] and bundle["prospective_OOS_days"] == 0
    for r in bundle["cases"]:
        if not r["curve_available"]:
            assert r["candidate"] == r["reference"]


def test_missing_live_day_does_not_borrow_an_older_replay():
    bundle = load_bundle()
    before = deepcopy(bundle)
    view = view_for_target(bundle, date(2026, 11, 23))
    assert view["status"] == "missing_snapshot" and view["record"] is None
    assert view["live_available"] is False and bundle == before


def test_archived_day_is_explicit_replay_or_reference_fallback():
    bundle = load_bundle()
    replay = view_for_target(bundle, "2026-10-08")
    fallback = view_for_target(bundle, "2026-11-17")
    assert replay["status"] == "research_replay" and not replay["live_available"]
    assert fallback["status"] == "reference_fallback" and not fallback["live_available"]


def test_compact_bucket_display_preserves_all_tail_mass():
    r = view_for_target(load_bundle(), "2026-10-08")["record"]
    before = deepcopy(r)
    rows = comparison_rows(r)
    for name in ["Shadow candidate", "TX research reference", "Archived Champion"]:
        assert sum(row[name] for row in rows) == pytest.approx(1)
    assert r == before
    assert modal({"16": 0.5, "17": 0.5})[0] == 16


def test_changed_archive_checksum_fails_closed(tmp_path):
    p = tmp_path / "archive.json"
    p.write_bytes(DATA.read_bytes() + b" ")
    p.with_suffix(".sha256").write_text(DATA.with_suffix(".sha256").read_text())
    with pytest.raises(ValueError, match="checksum"):
        load_bundle(p)


@pytest.mark.parametrize("missing", ["checksum", "cases"])
def test_empty_integrity_or_case_data_fails_closed(tmp_path, missing):
    p = tmp_path / "archive.json"
    data = load_bundle()
    if missing == "cases":
        data["cases"] = []
    p.write_text(json.dumps(data))
    p.with_suffix(".sha256").write_text(
        "" if missing == "checksum" else hashlib.sha256(p.read_bytes()).hexdigest()
    )
    with pytest.raises(ValueError):
        load_bundle(p)


def test_naive_checkpoint_cannot_be_presented_as_a_valid_freeze(tmp_path):
    bundle = load_bundle()
    bundle["cases"][0]["cutoff"] = "2025-09-01T16:00:00"
    p = tmp_path / "archive.json"
    p.write_text(json.dumps(bundle))
    p.with_suffix(".sha256").write_text(hashlib.sha256(p.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match="timezone"):
        load_bundle(p)


def test_streamlit_existing_tables_have_shadow_column_and_no_new_panel():
    from streamlit.testing.v1 import AppTest
    from pathlib import Path

    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "scripts/preview_madrid_late_shadow.py")
    ).run(timeout=15)
    assert not app.exception, [e.message for e in app.exception]
    assert len(app.dataframe) == 2 and not app.metric and not app.selectbox
    for table in app.dataframe:
        assert "v11 Reference · Late" in table.value.columns
        assert "v11 Curve · Late" in table.value.columns
        assert "v11 Fallback · Late" in table.value.columns
    checkpoints = app.dataframe[0].value
    assert checkpoints.iloc[2]["v11 Curve · Late"] == "—"
    assert "21 °C" in checkpoints.iloc[3]["v11 Curve · Late"]
    app.date_input[0].set_value(date(2026, 11, 23)).run()
    assert not app.exception, [e.message for e in app.exception]
    assert set(app.dataframe[0].value["v11 Curve · Late"]) == {"—"}
    assert "no recorded Shadow" in app.caption[-1].value


def test_ui_archive_failure_does_not_break_the_cockpit(monkeypatch):
    import weatherman.madrid_late_shadow_display as ui

    def broken():
        raise ValueError("checksum mismatch")

    monkeypatch.setattr(ui, "load_bundle", broken)
    view = ui.table_view_for_target(date(2026, 11, 23))
    assert view["status"] == "archive_unavailable"
    assert bucket_shadow_value(view, 21) == "—"
    assert checkpoint_shadow_value(view, "Late Live @16:00") == "—"


def test_shadow_cells_keep_replay_and_missing_states_distinct():
    view = table_view_for_target("2026-10-08")
    assert checkpoint_shadow_value(view, "First Live @12:00") == "—"
    assert checkpoint_shadow_value(view, "Late Live @16:00").startswith("21 °C")
    assert bucket_shadow_value(view, 21) == "48.4%"
    assert "retrospective" in view["caption"]
    missing = table_view_for_target("2026-11-23")
    assert bucket_shadow_value(missing, 21) == "—"
    assert missing["probabilities"] == {}
