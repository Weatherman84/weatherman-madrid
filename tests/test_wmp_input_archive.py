"""Synthetic clocks over the saved input fixture; these are not forecast evidence."""

from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from weatherman.research_wmp.archive import (
    archive_once, build_index, capture_window, encode, write_once,
)
from weatherman.research_wmp.snapshot import checkpoint, digest, instant

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research/wmp_madrid_shadow_v1"


def protocol():
    return json.loads((RESEARCH / "PROTOCOL.json").read_text())


def synthetic_bundle(day="2026-11-24"):
    original = json.loads((RESEARCH / "evidence/manual-inputs-2026-11-23.json").read_text())
    s = deepcopy(original)
    cut = checkpoint(day)
    final = cut - timedelta(minutes=7)
    delta = final - instant(original["as_of"])
    date_delta = date.fromisoformat(day) - date.fromisoformat(original["target_date"])
    s.update(target_date=day, capture_started_at=(final - timedelta(seconds=20)).isoformat(),
             as_of=final.isoformat())
    for kind, source in s["sources"].items():
        source["received_at"] = final.isoformat()
        if kind != "metar":
            daily = source["data"]["daily"]
            daily["time"] = [(date.fromisoformat(d) + date_delta).isoformat() for d in daily["time"]]
            for field in ("start_date", "end_date"):
                if field in source["request"]:
                    source["request"][field] = (date.fromisoformat(source["request"][field]) + date_delta).isoformat()
        else:
            for row in source["data"]:
                for field in ("observed_at", "received_at", "provider_received_at"):
                    row[field] = (instant(row[field]) + delta).isoformat()
        source["sha256"] = digest(source["data"])
    return s


def clocks(day="2026-11-24", finish_minutes=6):
    cut = checkpoint(day)
    values = iter([cut - timedelta(minutes=8), cut - timedelta(minutes=finish_minutes)])
    return lambda: next(values)


def never_collect():
    raise AssertionError("This path must not request source data")


@pytest.mark.parametrize("day", ["2026-11-24", "2027-07-10", "2027-03-28", "2027-10-31"])
def test_dst_safe_capture_window_and_midnight_identity(day):
    cut = checkpoint(day)
    assert capture_window(cut - timedelta(minutes=10))
    assert capture_window(cut - timedelta(seconds=1))
    assert not capture_window(cut - timedelta(minutes=11))
    assert not capture_window(cut)


def test_audit_cannot_capture_or_create_a_forecast(tmp_path):
    result = archive_once(tmp_path, protocol(), run_id="audit", collector=never_collect,
                          mode="audit", clock=lambda: checkpoint("2026-11-24") - timedelta(minutes=5))
    assert result["status"] == "audit_only"
    assert result["journal"]["selected_days"] == 0
    assert result["journal"]["prospective_OOS_days"] == 0
    assert list(tmp_path.iterdir()) == [tmp_path / "journal.json"]


def test_before_start_date_no_fake_capture_or_historical_gap_is_created(tmp_path):
    result = archive_once(tmp_path, protocol(), run_id="before-start", collector=never_collect,
                          clock=lambda: checkpoint("2026-11-23") + timedelta(hours=4))
    assert result["journal"]["selected_days"] == result["journal"]["gap_days"] == 0


def test_first_valid_capture_is_immutable_and_retry_performs_no_requests(tmp_path):
    bundle = synthetic_bundle()
    result = archive_once(tmp_path, protocol(), run_id="1-1", collector=lambda: bundle, clock=clocks())
    assert result["status"] == "selected_inputs_only"
    assert result["journal"]["selected_days"] == 1
    assert not result["journal"]["forecast_generated"]
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*.json")}
    retry = archive_once(tmp_path, protocol(), run_id="2-1", collector=never_collect,
                         clock=lambda: checkpoint("2026-11-24") - timedelta(minutes=3))
    assert retry["status"] == "already_selected_no_request"
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*.json")}


def test_archive_validation_does_not_run_the_probability_model(tmp_path, monkeypatch):
    import weatherman.research_wmp.snapshot as snapshot
    monkeypatch.setattr(snapshot, "fit_climatology", lambda *a: pytest.fail("No model fit in archive"))
    result = archive_once(tmp_path, protocol(), run_id="data-only", collector=synthetic_bundle, clock=clocks())
    assert result["journal"]["selected_days"] == 1


def test_partial_family_is_diagnostic_and_later_valid_retry_can_win(tmp_path):
    bad = synthetic_bundle()
    del bad["sources"]["ensemble"]
    result = archive_once(tmp_path, protocol(), run_id="partial", collector=lambda: bad, clock=clocks())
    assert result["status"].startswith("invalid_or_incomplete_input")
    assert result["journal"]["selected_days"] == 0
    old = next((tmp_path / "bundles").glob("*.json")).read_bytes()
    result = archive_once(tmp_path, protocol(), run_id="complete", collector=synthetic_bundle, clock=clocks())
    assert result["journal"]["selected_days"] == 1
    assert old in [p.read_bytes() for p in (tmp_path / "bundles").glob("*.json")]


def test_completion_after_cutoff_is_quarantined_and_gap_is_not_backfilled(tmp_path):
    cut = checkpoint("2026-11-24")
    s = synthetic_bundle()
    s["as_of"] = (cut + timedelta(seconds=1)).isoformat()
    values = iter([cut - timedelta(minutes=8), cut + timedelta(seconds=2)])
    result = archive_once(tmp_path, protocol(), run_id="late", collector=lambda: s, clock=lambda: next(values))
    assert result["status"] == "late_capture_not_selected"
    assert result["journal"]["selected_days"] == 0
    assert len(result["journal"]["attempts"]) == 1
    closed = archive_once(tmp_path, protocol(), run_id="close", collector=never_collect,
                          clock=lambda: cut + timedelta(minutes=12))
    assert closed["journal"]["gap_days"] == 1
    old_gap = (tmp_path / "gaps/2026-11-24.json").read_bytes()
    # Even an explicitly backdated retry cannot replace a closed gap.
    retry = archive_once(tmp_path, protocol(), run_id="backdated", collector=never_collect, clock=clocks())
    assert retry["status"] == "closed_gap_no_backfill"
    assert (tmp_path / "gaps/2026-11-24.json").read_bytes() == old_gap


def test_old_snapshot_cannot_be_relabelled_as_new_capture(tmp_path):
    old = json.loads((RESEARCH / "evidence/manual-inputs-2026-11-23.json").read_text())
    result = archive_once(tmp_path, protocol(), run_id="old-data", collector=lambda: old, clock=clocks())
    assert result["status"].startswith("invalid_or_incomplete_input")
    assert result["journal"]["selected_days"] == 0


def test_old_source_receipt_cannot_be_hidden_by_new_outer_timestamps(tmp_path):
    s = synthetic_bundle()
    s["sources"]["ensemble"]["received_at"] = (checkpoint("2026-11-24") - timedelta(hours=3)).isoformat()
    result = archive_once(tmp_path, protocol(), run_id="old-receipt", collector=lambda: s, clock=clocks())
    assert "receipt outside" in result["status"]
    assert result["journal"]["selected_days"] == 0


def test_partial_network_failure_is_recordable_without_silent_fallback(monkeypatch):
    from weatherman.research_wmp import capture
    import httpx

    def fake(item):
        kind = item[0]
        if kind == "ensemble":
            raise httpx.ConnectError("test failure")
        return kind, {"kind": kind}

    monkeypatch.setattr(capture, "retrieve", fake)
    partial = capture.capture_snapshot(allow_partial=True)
    assert partial["capture_errors"] == {"ensemble": "ConnectError"}
    assert set(partial["sources"]) == {"history", "recent", "metar"}
    with pytest.raises(RuntimeError, match="ensemble"):
        capture.capture_snapshot()


def test_corrupt_selected_object_stops_before_another_network_request(tmp_path):
    archive_once(tmp_path, protocol(), run_id="initial", collector=synthetic_bundle, clock=clocks())
    file = next((tmp_path / "bundles").glob("*.json"))
    data = json.loads(file.read_text())
    data["target_date"] = "2099-01-01"
    file.write_text(encode(data))
    with pytest.raises(ValueError, match="digest"):
        archive_once(tmp_path, protocol(), run_id="retry", collector=never_collect, clock=clocks())


def test_existing_bytes_and_paths_cannot_be_replaced_or_traversed(tmp_path):
    p = tmp_path / "file.json"
    assert write_once(p, {"a": 1})
    assert not write_once(p, {"a": 1})
    with pytest.raises(ValueError, match="replace"):
        write_once(p, {"a": 2})
    with pytest.raises(ValueError, match="identity"):
        archive_once(tmp_path, protocol(), run_id="../escape", collector=never_collect, clock=clocks())


def test_input_only_index_remains_separate_from_prediction_evidence(tmp_path):
    archive_once(tmp_path, protocol(), run_id="first", collector=synthetic_bundle, clock=clocks())
    journal = build_index(tmp_path, protocol())
    assert journal["data_only"] and not journal["writes_production_database"]
    assert journal["prospective_OOS_days"] == 0
    assert not journal["automatic_execution"] and not journal["automatic_promotion"]
    assert all("candidate" not in r for r in journal["selected"])


def test_workflow_has_no_database_or_provider_secrets_and_only_pushes_the_data_branch():
    source = (ROOT / ".github/workflows/wmp-madrid-input-archive.yml").read_text()
    assert "DATABASE_URL" not in source and "secrets." not in source
    assert "git push origin HEAD:wmp-madrid-input-archive" in source
    assert "HEAD:main" not in source
    assert "git diff --name-only" in source
    assert "'push' && 'audit'" in source
    assert 'cron: "50,55 13,14 * * *"' in source
    assert 'cron: "12,32 14,15 * * *"' in source
