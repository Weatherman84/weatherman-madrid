"""Real EPS input replay, upstream parity and explicit non-activation gates."""

from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path

import pytest

from weatherman.research_wmp.snapshot import (
    checkpoint, digest, instant, member_counts, metar_floor, run_snapshot, validate_protocol, verify_upstream,
)

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research/wmp_madrid_shadow_v1"


@pytest.fixture
def inputs():
    return json.loads((RESEARCH / "evidence/manual-inputs-2026-11-23.json").read_text())


@pytest.fixture
def protocol():
    return json.loads((RESEARCH / "PROTOCOL.json").read_text())


def test_original_source_is_pinned_with_mit_notice():
    manifest = verify_upstream()
    assert manifest["commit"] == "cc01f6f228250138a9300e20d3cdc791675bbb96"
    license_file = ROOT / "src/weatherman/research_wmp/upstream/LICENSE"
    assert "Copyright (c) 2026 Krish Yadav" in license_file.read_text()


def test_real_member_schema_and_exact_original_pipeline_parity(inputs, protocol):
    before = deepcopy(inputs)
    result = run_snapshot(inputs, protocol)
    oracle = json.loads((RESEARCH / "evidence/upstream-parity-2026-11-23.json").read_text())
    for key, name in [("upstream_grid_math_without_floor", "unfloored_grid_math"),
                       ("candidate", "same_math_with_METAR_input")]:
        expected = {int(k): v for k, v in oracle[name]["probabilities"].items()}
        assert result[key] == expected
        assert sum(result[key].values()) == pytest.approx(1)
    assert result["members"] == protocol["family_members"]
    assert sum(result["members"].values()) == 143
    assert result["ensemble_weight"] == .95
    assert min(result["candidate"]) >= result["observed_metar_floor_c"]
    assert result["status"] == "offline_diagnostic_only" and not result["prospective_OOS"]
    assert inputs == before


@pytest.mark.parametrize("key,value", [("spread_inflation", 1.5), ("station_offset", .2),
                                      ("seed", 8), ("live_activation", True),
                                      ("historical_full_ensemble_validation_passed", True)])
def test_no_unreviewed_parameters_or_live_activation_switch(protocol, key, value):
    protocol[key] = value
    with pytest.raises(ValueError, match="Protocol"):
        validate_protocol(protocol)


def test_future_receipt_and_tampered_inputs_fail_closed(inputs, protocol):
    bad = deepcopy(inputs)
    bad["sources"]["ensemble"]["received_at"] = (instant(inputs["as_of"]) + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="after"):
        run_snapshot(bad, protocol)
    bad = deepcopy(inputs)
    bad["sources"]["ensemble"]["data"]["daily"]["temperature_2m_max_ncep_gefs025"][0] += .1
    with pytest.raises(ValueError, match="digest"):
        run_snapshot(bad, protocol)


@pytest.mark.parametrize("problem", ["missing_family", "deterministic_proxy", "wrong_unit", "null_member"])
def test_incomplete_eps_is_not_replaced_by_deterministic_models(inputs, problem):
    data = deepcopy(inputs["sources"]["ensemble"]["data"])
    daily = data["daily"]
    key = "temperature_2m_max_member01_gem_global_ensemble"
    if problem == "missing_family":
        for name in list(daily):
            if "gem_global" in name:
                del daily[name]
    elif problem == "deterministic_proxy":
        daily["temperature_2m_max_gfs_global"] = daily.pop(key)
    elif problem == "wrong_unit":
        data["daily_units"][key] = "°F"
    else:
        daily[key][0] = None
    with pytest.raises(ValueError):
        member_counts(data, inputs["target_date"])


def test_history_cannot_contain_target_actual(inputs, protocol):
    history = inputs["sources"]["history"]
    history["data"]["daily"]["time"][-1] = inputs["target_date"]
    history["sha256"] = digest(history["data"])
    with pytest.raises(ValueError, match="Historical window"):
        run_snapshot(inputs, protocol)


def test_forecast_grid_is_not_accepted_as_measured_metar(inputs, protocol):
    source = inputs["sources"]["metar"]
    source["request"]["station"] = "GRID"
    with pytest.raises(ValueError, match="station"):
        run_snapshot(inputs, protocol)


def test_corrected_metar_replaces_the_obsolete_value_instead_of_taking_a_false_maximum(inputs):
    at = instant(inputs["as_of"])
    common = {"station": "LEMD", "observed_at": (at - timedelta(minutes=30)).isoformat()}
    rows = [{**common, "received_at": (at - timedelta(minutes=25)).isoformat(), "temp_c": 30},
            {**common, "received_at": (at - timedelta(minutes=20)).isoformat(), "temp_c": 20}]
    assert metar_floor(rows, at) == 20
    rows.append({**rows[-1], "temp_c": 21})
    with pytest.raises(ValueError, match="Conflicting"):
        metar_floor(rows, at)


def test_committed_research_evidence_matches_its_manifest():
    import hashlib
    manifest = json.loads((RESEARCH / "MANIFEST.json").read_text())
    for path, expected in manifest["files_sha256"].items():
        assert hashlib.sha256((RESEARCH / path).read_bytes()).hexdigest() == expected


def test_no_numerical_dependence_on_later_actual_or_trading_price(inputs, protocol):
    a = run_snapshot(inputs, protocol)
    inputs["actual"] = -999
    inputs["market_price"] = .99
    b = run_snapshot(inputs, protocol)
    assert a["candidate"] == b["candidate"]


def test_real_historical_readiness_is_zero_and_no_scores_are_invented(inputs):
    path = ROOT / "scripts/run_wmp_madrid_shadow.py"
    spec = importlib.util.spec_from_file_location("wmp_readiness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = json.loads((ROOT / "src/weatherman/data/madrid_late_curve_shadow_v1.json").read_text())["cases"]
    audit = module.coverage(cases, [inputs])
    assert audit["cases_checked"] == 434
    assert audit["member_archive_screen_passed_cases"] == 0
    assert audit["excluded"]["missing_target_member_snapshot"] == 434
    assert audit["full_historical_scores"] is None
    assert audit["live_activation"] is False


def test_later_capture_cannot_be_relabelled_as_checkpoint_history(inputs):
    assert instant(inputs["as_of"]) > checkpoint(inputs["target_date"])
    result = json.loads((RESEARCH / "evidence/diagnostic-and-readiness-2026-11-23.json").read_text())
    assert not result["forecast_diagnostic"]["all_sources_received_by_checkpoint"]
    assert not result["forecast_diagnostic"]["prospective_OOS"]


def test_research_package_is_not_imported_by_production_entrypoints():
    for name in ("app.py", "src/weatherman/collector.py", "src/weatherman/service.py"):
        assert "research_wmp" not in (ROOT / name).read_text()
