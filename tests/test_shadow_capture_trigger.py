"""Replay the observed missing-trigger day against the real workflow gates."""

import builtins
from datetime import datetime
import json
from pathlib import Path
import re
import textwrap
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from weatherman.madrid_late_shadow_display import table_view_for_target
from weatherman.madrid_late_shadow_journal import build_journal, recording_coverage

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ("madrid-v11-shadow.yml", "wmp-madrid-input-archive.yml")


def workflow(name):
    return (ROOT / ".github/workflows" / name).read_text()


def workflow_gate(source):
    block = source.split("python - <<'PY' >> \"$GITHUB_OUTPUT\"", 1)[1].split("\n          PY", 1)[0]
    return textwrap.dedent(block)


def gate_at(source, at, tmp_path, monkeypatch, capsys, mode="auto"):
    real_import = builtins.__import__

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz)

    def controlled_import(name, *args, **kwargs):
        return SimpleNamespace(datetime=Clock) if name == "datetime" else real_import(name, *args, **kwargs)

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("MODE", mode)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    exec(compile(workflow_gate(source), "workflow_gate", "exec"),
         {"__builtins__": {**vars(builtins), "__import__": controlled_import}})
    output = capsys.readouterr().out
    assert "Madrid clock:" in summary.read_text()
    return "due=true" in output


@pytest.mark.parametrize("name", WORKFLOWS)
def test_observed_cloudflare_backed_feed_supplies_the_missing_window_trigger(name):
    source = workflow(name)
    triggers = source.split("  workflow_run:", 1)[1].split("    types:", 1)[0]
    accepted = re.findall(r'"([^"]+)"', triggers)
    evidence = json.loads((ROOT / "tests/fixtures/shadow_trigger_2026_11_24.json").read_text())
    zone = ZoneInfo("Europe/Madrid")
    eligible = [r for r in evidence["upstream_jobs"] if r["workflow"] in accepted
                and "15:50" <= datetime.fromisoformat(r["completed_at"].replace("Z", "+00:00")).astimezone(zone).strftime("%H:%M") < "16:00"]
    assert [r["workflow"] for r in eligible] == ["13 - Build opportunity scanner causal feed"]
    # Before this correction, the sole accepted collector completed at 15:37:59.
    assert "3 - Madrid collector" in accepted
    assert "head_repository.full_name == github.repository" in source
    assert "branches: [main]" in source
    # It is a clock source, not a dependency on the scanner's result quality.
    assert "workflow_run.conclusion == 'success'" not in source


@pytest.mark.parametrize("name", WORKFLOWS)
@pytest.mark.parametrize("at", ["2026-11-24T14:54:19+00:00", "2027-07-10T13:54:19+00:00"])
def test_real_gate_still_runs_after_feed_completion_and_one_minute_setup(name, at, tmp_path, monkeypatch, capsys):
    assert gate_at(workflow(name), datetime.fromisoformat(at), tmp_path, monkeypatch, capsys)


@pytest.mark.parametrize("name", WORKFLOWS)
def test_late_trigger_does_not_reopen_capture(name, tmp_path, monkeypatch, capsys):
    assert not gate_at(workflow(name), datetime.fromisoformat("2026-11-24T15:00:00+00:00"), tmp_path, monkeypatch, capsys)


def test_coverage_reports_yesterdays_absence_without_creating_a_forecast(tmp_path):
    journal = build_journal(tmp_path, as_of="2026-11-25T07:00:00+00:00")
    assert journal["recording_coverage"]["missing_days"] == ["2026-11-24"]
    assert journal["decisions"] == [] and journal["curve_evidence"]["frozen_days"] == 0
    assert not list(tmp_path.rglob("*.json"))
    view = table_view_for_target("2026-11-24", live=journal)
    assert view["status"] == "missed_checkpoint" and not view["probabilities"]
    assert "2026-11-24" in view["caption"] and "not backfilled" in view["caption"]


def test_coverage_does_not_close_today_before_the_checkpoint_or_mark_recorded_days_missing():
    before = recording_coverage([], "2026-11-24T14:59:59+00:00")
    assert before["missing_days"] == []
    after = recording_coverage([], "2026-11-24T15:10:00+00:00")
    assert after["missing_days"] == ["2026-11-24"]
    with_record = recording_coverage([{"target_date": "2026-11-24"}], "2026-11-25T07:00:00+00:00")
    assert with_record["missing_days"] == []


def test_old_journal_without_coverage_remains_supported(tmp_path):
    journal = build_journal(tmp_path, as_of="2026-11-25T07:00:00+00:00")
    journal.pop("recording_coverage")
    assert table_view_for_target("2026-11-24", live=journal)["status"] == "missing_snapshot"
