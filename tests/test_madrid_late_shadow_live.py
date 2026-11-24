"""Causal capture, frozen inference parity and append-only retries."""

from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path

import pytest

from weatherman.madrid_late_shadow_inputs import build_input, capture_due
from weatherman.madrid_late_shadow_journal import (
    append_record, build_journal, decision, outcome, seal, validate_decision,
)
from weatherman.madrid_late_shadow_model import (
    CHECKPOINT, MODELS, PARAMETER_SHA, ZONE, bounds, cutoff, load_parameters, predict,
)
from weatherman.madrid_late_shadow_public import validate_journal
from weatherman.madrid_late_shadow_display import table_view_for_target, checkpoint_shadow_value


ROOT = Path(__file__).resolve().parents[1]


def sources(day="2026-11-24"):
    cut = cutoff(day)
    start, end = bounds(day)
    captured = cut - timedelta(minutes=7)
    observations = [{"observed_at": (cut - timedelta(minutes=m)).isoformat(), "temp_c": t,
                     "dewpoint_c": 7.0, "wind_kph": 10.0, "wind_direction": 180.0, "cloud_cover": 20.0}
                    for m, t in [(150, 17), (120, 18), (90, 19), (60, 20), (30, 20)]]
    forecasts, hourly = [], []
    for i, model in enumerate(MODELS):
        run = cut - timedelta(hours=4)
        forecasts.append({"id": i, "model": model, "run_at": run.isoformat(),
                          "fetched_at": (cut - timedelta(minutes=20)).isoformat(),
                          "model_run_at": run.isoformat(), "available_at": (run + timedelta(hours=1)).isoformat(),
                          "max_temp_c": 21.0 + i / 10, "provenance_status": "test"})
        t = start
        while t < end:
            hourly.append({"model": model, "run_at": run.isoformat(), "valid_at": t.isoformat(),
                           "temp_c": 21.0 + i / 10 if t == cut + timedelta(hours=1) else 20.0})
            t += timedelta(hours=1)
    bulletin = {"id": 1, "issue_time": (cut - timedelta(hours=5)).isoformat(),
                "available_at": (cut - timedelta(hours=4)).isoformat(), "valid_from": start.isoformat(),
                "valid_to": end.isoformat(), "max_temp_c": 22, "max_temp_at": (cut + timedelta(hours=1)).isoformat(),
                "content_hash": "known"}
    drivers = {"captured_at": (cut - timedelta(minutes=20)).isoformat(),
               "features": {"remaining_model_rise_c": 1.0, "model_radiation_wm2": 350.0,
                            "future_radiation_max_wm2": 400.0}}
    return day, captured, {"observations": observations, "forecasts": forecasts,
                           "hourly": hourly, "bulletins": [bulletin], "drivers": drivers}


def frozen(day="2026-11-24"):
    day, captured, raw = sources(day)
    row = build_input(day, captured, **raw)
    return decision(row, captured_at=captured, generated_at=captured + timedelta(seconds=1), run_id="test")


def test_exported_parameters_reproduce_original_research_vectors():
    fixtures = json.loads((ROOT / "tests/fixtures/madrid_late_shadow_parity.json").read_text())
    assert {(r["input"]["curve_available"], r["input"]["taf_present"]) for r in fixtures} == {
        (True, True), (True, False), (False, True), (False, False)}
    before = load_parameters()
    for fixture in fixtures:
        predicted = predict(fixture["input"])
        for name in ("candidate", "reference"):
            expected = {int(k): v for k, v in fixture[name].items()}
            assert predicted[name] == pytest.approx(expected, abs=1e-12)
    assert before == load_parameters()
    assert PARAMETER_SHA == digest_file(ROOT / "src/weatherman/data/madrid_late_shadow_parameters_v1.json")


def digest_file(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("day", ["2026-11-24", "2027-07-10", "2027-03-28", "2027-10-31"])
def test_real_madrid_cutoff_and_full_local_day_across_dst(day):
    day, captured, raw = sources(day)
    row = build_input(day, captured, **raw)
    assert row["curve_available"]
    assert cutoff(day).astimezone(ZONE).hour == 16
    assert capture_due(captured)
    assert not capture_due(cutoff(day))
    assert not capture_due(cutoff(day) - timedelta(minutes=11))
    with pytest.raises(ValueError, match="window"):
        build_input(day, cutoff(day) + timedelta(seconds=1), **raw)


def test_future_metar_and_taf_never_enter_prediction():
    day, captured, raw = sources()
    base = build_input(day, captured, **raw)
    raw["observations"].append({**raw["observations"][-1], "observed_at": (cutoff(day) + timedelta(minutes=30)).isoformat(), "temp_c": 40})
    raw["bulletins"].append({**raw["bulletins"][0], "id": 2,
                             "issue_time": (cutoff(day) + timedelta(minutes=1)).isoformat(),
                             "available_at": (cutoff(day) + timedelta(minutes=2)).isoformat(), "max_temp_c": 40})
    new = build_input(day, captured, **raw)
    assert base["features"] == new["features"]
    assert predict(base) == predict(new)


def test_latest_taf_without_tx_does_not_borrow_older_tx():
    day, captured, raw = sources()
    raw["bulletins"].append({**raw["bulletins"][0], "id": 2,
                             "issue_time": (captured - timedelta(minutes=20)).isoformat(),
                             "available_at": (captured - timedelta(minutes=10)).isoformat(),
                             "max_temp_c": None, "max_temp_at": None})
    assert not build_input(day, captured, **raw)["taf_present"]


@pytest.mark.parametrize("problem", ["incomplete", "different_maximum"])
def test_missing_curve_is_exact_reference_not_a_silent_model_switch(problem):
    day, captured, raw = sources()
    if problem == "incomplete":
        raw["hourly"].pop()
    else:
        raw["forecasts"][0]["max_temp_c"] += 0.1
    row = build_input(day, captured, **raw)
    assert not row["curve_available"]
    assert row["curve_audit"]["missing_reasons"]
    p = predict(row)
    assert p["candidate"] == p["reference"]


def test_newer_unusable_daily_run_does_not_select_an_older_convenient_one():
    day, captured, raw = sources()
    raw["forecasts"].append({**raw["forecasts"][0], "id": 100,
                             "fetched_at": (cutoff(day) + timedelta(seconds=1)).isoformat()})
    with pytest.raises(ValueError, match="latest forecast"):
        build_input(day, captured, **raw)


def test_missing_or_stale_metar_fails_closed():
    day, captured, raw = sources()
    raw["observations"].pop()
    with pytest.raises(ValueError, match="fresh"):
        build_input(day, captured, **raw)


def test_immutable_decision_retries_and_tamper_detection(tmp_path):
    r = frozen()
    assert append_record(tmp_path, "decisions", r)
    p = tmp_path / "decisions" / (r["target_date"] + ".json")
    before = p.read_bytes()
    assert not append_record(tmp_path, "decisions", r)
    assert p.read_bytes() == before
    changed = seal({**{k: v for k, v in r.items() if k != "sha256"}, "run_id": "retry"})
    with pytest.raises(ValueError, match="replace"):
        append_record(tmp_path, "decisions", changed)
    r["candidate"][20] = 0.01
    with pytest.raises(ValueError, match="digest"):
        validate_decision(r)


def test_late_or_other_day_generation_cannot_be_certified():
    r = frozen()
    for key, value in [("generated_at", cutoff(r["target_date"]).isoformat()),
                        ("captured_at", (cutoff(r["target_date"]) - timedelta(days=1, minutes=7)).isoformat())]:
        bad = seal({**{k: v for k, v in r.items() if k != "sha256"}, key: value})
        with pytest.raises(ValueError, match="cutoff"):
            validate_decision(bad)


def test_outcomes_append_separately_and_only_after_closeout(tmp_path):
    r = frozen()
    append_record(tmp_path, "decisions", r)
    before = (tmp_path / "decisions" / "2026-11-24.json").read_bytes()
    with pytest.raises(ValueError, match="closeout"):
        outcome(r, 21, None, evaluated_at=cutoff("2026-11-24") + timedelta(hours=2))
    at = cutoff("2026-11-25")
    o = outcome(r, 21, {"probabilities": {21: .75, 20: .25}}, evaluated_at=at)
    append_record(tmp_path, "outcomes", o)
    assert before == (tmp_path / "decisions" / "2026-11-24.json").read_bytes()
    journal = build_journal(tmp_path)
    assert journal["curve_evidence"]["resolved_days"] == 1
    assert journal["curve_evidence"]["paired_champion_days"] == 1
    assert journal["fallback_only"]["resolved_days"] == 0
    assert journal["curve_evidence"]["metrics"]["candidate"]["n"] == 1


def test_live_ui_selects_exact_day_and_keeps_first_live_unchanged(tmp_path):
    append_record(tmp_path, "decisions", frozen())
    live = build_journal(tmp_path)
    assert validate_journal(live)
    view = table_view_for_target("2026-11-24", live=live)
    assert view["status"] == "prospective_shadow" and view["live_available"]
    assert "inputs frozen 15:53:00" in view["caption"]
    assert checkpoint_shadow_value(view, "First Live @12:00") == "—"
    assert checkpoint_shadow_value(view, CHECKPOINT) != "—"
    assert not table_view_for_target("2026-11-25", live=live)["probabilities"]
    bad = deepcopy(live)
    bad["decisions"][0]["generated_at"] = cutoff("2026-11-24").isoformat()
    assert not table_view_for_target("2026-11-24", live=bad)["probabilities"]


def test_stored_source_package_replays_the_same_frozen_input():
    r = frozen()
    rebuilt = build_input(r["target_date"], r["captured_at"], **r["inputs"]["source_snapshot"])
    assert rebuilt == r["inputs"]
    assert predict(rebuilt)["candidate"] == r["candidate"]


def test_public_network_failure_preserves_missing_state(monkeypatch):
    import httpx
    import weatherman.madrid_late_shadow_public as public

    def broken(*args, **kwargs):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(public.httpx, "stream", broken)
    live = public.fetch_journal()
    view = table_view_for_target("2026-11-24", live=live)
    assert not view["probabilities"]
    assert "temporarily unavailable" in view["caption"]


def test_new_workflow_uses_its_own_branch_and_never_backfills():
    source = (ROOT / ".github/workflows/madrid-v11-shadow.yml").read_text()
    assert 'workflows: ["3 - Madrid collector"]' in source
    assert 'cron: "50,55 13,14 * * *"' in source
    assert "madrid-v11-shadow-data" in source
    assert "git push origin HEAD:madrid-v11-shadow-data" in source
    assert "git push origin HEAD:main" not in source
    assert "git diff --name-only" in source
    assert "secrets.DATABASE_URL" in source
    assert "'push' && 'audit'" in source


def test_source_reader_has_no_writes_and_no_outcome_input():
    # The script deliberately avoids the DB module's initialization/migration helpers.
    source = (ROOT / "scripts/record_madrid_late_shadow.py").read_text()
    assert "connect_args={\"options\"" not in source
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY" in source
    assert "SET LOCAL statement_timeout" in source
    assert "SHOW transaction_read_only" in source
    assert "transaction.rollback()" in source
    assert "init_db" not in source and "weatherman.db" not in source
    spec = importlib.util.spec_from_file_location("shadow_recorder", ROOT / "scripts/record_madrid_late_shadow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    queries = []

    class Result:
        def mappings(self):
            return []

    class Connection:
        def execute(self, statement, params):
            queries.append(str(statement))
            return Result()

    module.read_sources(Connection(), "2026-11-24")
    assert queries and all(q.startswith("SELECT ") for q in queries)
    assert all("actual" not in q and "market" not in q for q in queries)


def test_pooled_connection_is_read_only_before_source_queries_and_always_rolled_back():
    spec = importlib.util.spec_from_file_location("readonly_recorder", ROOT / "scripts/record_madrid_late_shadow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append("close")

        def begin(self):
            events.append("begin")
            return self

        def execute(self, statement):
            events.append(str(statement))

        def scalar(self, statement):
            events.append(str(statement))
            return "on"

        def rollback(self):
            events.append("rollback")

    class Engine:
        def connect(self):
            return Connection()

    with pytest.raises(RuntimeError, match="source read failure"):
        with module.read_only_snapshot(Engine()) as connection:
            connection.execute("SELECT weather_sources")
            raise RuntimeError("source read failure")
    assert events == ["begin", "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
                      "SET LOCAL statement_timeout = '20s'", "SHOW transaction_read_only",
                      "SELECT weather_sources", "rollback", "close"]
