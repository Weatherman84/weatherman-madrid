"""Temporal boundaries and distribution semantics for the isolated v11 experiment."""
from datetime import timedelta
from pathlib import Path

import pytest

from weatherman.v11.contracts import (
    ModelRun, checkpoint, day_bounds, delta_probabilities, distribution, instant,
    market_partition, normal_buckets, observed_floor, remaining_hazard, reported_bucket,
    require_training_labels, score, temperature_scale,
)
from weatherman.v11.evidence import observation_features, read_rows


@pytest.mark.parametrize("day,hours", [("2026-03-29", 23), ("2026-10-25", 25),
                                      ("2026-11-17", 24)])
def test_local_day_dst(day, hours):
    start, end = day_bounds(day)
    assert (end - start).total_seconds() / 3600 == hours


def test_checkpoint_local_clock():
    assert checkpoint("2026-03-29", 9).hour == 7
    assert checkpoint("2026-10-25", 9).hour == 8
    assert checkpoint("2026-10-25", 20, True).isoformat() == "2026-10-24T18:00:00+00:00"


@pytest.mark.parametrize("value", ["2026-11-17T16:00:00", "2026-11-17"])
def test_naive_timestamps_rejected(value):
    with pytest.raises(ValueError):
        instant(value)


@pytest.mark.parametrize("field,value", [
    ("initialized_at", "2026-11-17T16:00:00+00:00"),
    ("available_at", "2026-11-17T16:00:00+00:00"),
    ("received_at", "2026-11-17T16:00:00+00:00"),
    ("initialized_at", "2026-11-14T00:00:00+00:00"),
    ("value", float("nan")),
])
def test_run_causality_and_staleness(field, value):
    fields = dict(model="IFS", initialized_at="2026-11-17T06:00:00+00:00",
                  available_at="2026-11-17T10:00:00+00:00",
                  received_at="2026-11-17T10:05:00+00:00", value=17)
    fields[field] = value
    with pytest.raises(ValueError):
        ModelRun(**fields).require_causal("2026-11-17T15:00:00+00:00")


def test_exact_cutoff_is_allowed():
    ModelRun("IFS", "2026-11-17T06:00:00Z", "2026-11-17T10:00:00Z",
             "2026-11-17T15:00:00Z", 17).require_causal("2026-11-17T15:00:00Z")


def test_unavailable_label_cannot_enter_fit():
    with pytest.raises(ValueError):
        require_training_labels([{"actual_available_at": "2026-11-18T00:10:00Z"}],
                                "2026-11-17T15:00:00Z")


@pytest.mark.parametrize("values", [{}, {16: .8}, {16: 1.1, 17: -.1},
                                    {16: float("nan")}, {16: float("inf")},
                                    {16.5: 1.0}, {"16": 1.0, 16: .1}])
def test_invalid_or_incomplete_probabilities_fail_closed(values):
    with pytest.raises(ValueError):
        distribution(values)


def test_floor_absorbs_mass_instead_of_inflating_upper_tail():
    p = observed_floor({14: .4, 15: .2, 16: .2, 17: .2}, 16)
    assert p[16] == pytest.approx(.8)
    assert p[17] == pytest.approx(.2)
    assert sum(delta_probabilities(p, 16).values()) == pytest.approx(1)


def test_normal_mass_and_target_not_used_to_define_support():
    p = normal_buckets(16.5, .7)
    assert sum(p.values()) == pytest.approx(1)
    assert p[16] == pytest.approx(p[17])
    assert score(p, 100)["log_loss"] > 20


@pytest.mark.parametrize("value,expected", [(16.49, 16), (16.5, 17), (-.5, -1), (-1.5, -2)])
def test_explicit_half_away_rounding(value, expected):
    assert reported_bucket(value, "half_away_from_zero") == expected


def test_unknown_market_rounding_cannot_be_guessed():
    with pytest.raises(ValueError):
        reported_bucket(16.5, "market_default")


def test_open_market_tails():
    p = market_partition({15: .2, 16: .5, 17: .3}, [
        {"id": "low", "lower": None, "upper": 15},
        {"id": "middle", "lower": 16, "upper": 16},
        {"id": "high", "lower": 17, "upper": None}])
    assert p == {"low": .2, "middle": .5, "high": .3}


def test_market_overlap_rejected():
    with pytest.raises(ValueError):
        market_partition({16: 1}, [{"id": "a", "lower": None, "upper": 16},
                                     {"id": "b", "lower": 16, "upper": None}])


def test_scores_include_actual_outside_exported_support():
    s = score({16: 1}, 17)
    assert s["brier"] == 2
    assert s["discrete_crps"] == 1
    assert s["zero_winner_mass"] == 1
    assert score({17: 1}, 17)["brier"] == 0


def test_temperature_scaling_keeps_zeros_and_mass():
    p = temperature_scale({16: .8, 17: .2, 18: 0}, 2)
    assert p.get(18, 0) == 0
    assert p[16] < .8
    assert sum(p.values()) == pytest.approx(1)


def test_hazard_allows_17_november_2100_bucket():
    p = remaining_hazard([("2026-11-17T20:00:00Z", .2),
                          ("2026-11-17T21:00:00Z", .1)],
                         "2026-11-17T15:00:00Z", "2026-11-17")
    assert p == pytest.approx(.28)


def test_hazard_outside_day_and_past_windows_rejected():
    with pytest.raises(ValueError):
        remaining_hazard([("2026-11-18T01:00:00Z", .5)],
                         "2026-11-17T15:00:00Z", "2026-11-17")


def test_observation_cutoff_and_local_day():
    obs = [{"observed_at": "2026-11-17T14:30:00Z", "available_at": "2026-11-17T14:35:00Z", "temp_c": 16},
           {"observed_at": "2026-11-17T15:00:00Z", "available_at": "2026-11-17T15:05:00Z", "temp_c": 18},
           {"observed_at": "2026-11-16T22:30:00Z", "temp_c": 30}]
    f = observation_features(obs, "2026-11-17T15:00:00Z", "2026-11-17")
    assert f["observed_max"] == 16
    assert f["observation_age"] == 30
    assert observation_features([], "2026-11-17T15:00:00Z", "2026-11-17") == {}


def test_late_17_november_real_fixture_and_future_invariance():
    path = Path(__file__).resolve().parents[1] / "research/v11/artifacts/rows.jsonl.gz"
    rows = read_rows(path)
    row = next(r for r in rows if r["target_date"] == "2026-11-17" and "Late" in r["checkpoint"])
    assert row["observed_max"] == 16
    assert row["actual"] == 17
    assert row["features"]["latest_observed_at"] <= row["checkpoint_at"]
    # The future label is excluded from the versioned inference feature vector.
    from weatherman.v11.evidence import FEATURES
    assert "actual" not in FEATURES and "actual_peak_at" not in FEATURES
    assert len(rows) > 2800
    for r in rows:
        if r["features"].get("latest_observed_at"):
            assert instant(r["features"]["latest_observed_at"]) <= instant(r["checkpoint_at"])
            start, _ = day_bounds(r["target_date"])
            assert instant(r["features"]["latest_observed_at"]) >= start


def test_frozen_model_inference_ignores_future_labels_and_market():
    pytest.importorskip("sklearn")
    from weatherman.v11.bakeoff import CONFIG, FrozenModel, MODELS, common
    path = Path(__file__).resolve().parents[1] / "research/v11/artifacts/rows.jsonl.gz"
    rows = read_rows(path)
    train = [r for r in rows if CONFIG["discovery"][0] <= r["target_date"] <= CONFIG["discovery"][1]
             and "Late" in r["checkpoint"] and common(r)]
    model = FrozenModel(train)
    row = next(r for r in rows if r["target_date"] == "2026-11-17" and "Late" in r["checkpoint"])
    assert all(m in row["models"] for m in MODELS)
    expected = model.predict(row)["delta_full"]
    changed = {**row, "actual": 999, "market": {"price": .99},
               "actual_available_at": (instant(row["actual_available_at"]) + timedelta(days=1)).isoformat()}
    assert model.predict(changed)["delta_full"] == expected
    assert expected[17] > 0
    assert sum(expected.values()) == pytest.approx(1)


def frozen_fixture(tmp_path):
    from weatherman.v11.shadow import freeze_forecast
    return freeze_forecast(tmp_path / "forecast.json", probabilities={-1: .2, 16: .6, 17: .2},
                           airport="LEMD", target_date="2026-11-17", cutoff="2026-11-17T15:00:00Z",
                           frozen_at="2026-11-17T15:00:01Z", model_version="v11-test",
                           source_sha256="a" * 64, evidence_class="historical_reduced_fidelity")


def market_fixture():
    return {"read_at": "2026-11-17T15:00:02Z", "airport": "LEMD", "target_date": "2026-11-17",
            "market_id": "test-market", "resolution_source": "stored_metar",
            "resolution_rules_verified": True, "quotes": [
                {"bucket": k, "outcome_id": str(k), "captured_at": "2026-11-17T15:00:00Z",
                 "bid": .1, "ask": .2, "ask_depth": 10, "fee_per_share": .01,
                 "slippage_per_share": .01} for k in (-1, 16, 17)]}


def test_freeze_survives_json_roundtrip_and_rejects_overwrite(tmp_path):
    import json
    from weatherman.v11.shadow import market_diagnostic
    frozen_fixture(tmp_path)
    loaded = json.loads((tmp_path / "forecast.json").read_text())
    result = market_diagnostic(loaded, market_fixture())
    assert result["trade_allowed"] is False and result["official_roi"] is None
    assert result["quotes"][1]["edge_after_cost"] == pytest.approx(.38)
    assert sum(q["normalized_midpoint_probability"] for q in result["quotes"]) == pytest.approx(1)
    with pytest.raises(FileExistsError):
        frozen_fixture(tmp_path)


@pytest.mark.parametrize("field,value", [("read_at", "2026-11-17T14:59:00Z"),
                                         ("resolution_source", "aemet_physical"),
                                         ("target_date", "2026-11-18"),
                                         ("airport", "EHAM")])
def test_market_rejects_premature_read_and_target_mismatch(tmp_path, field, value):
    from weatherman.v11.shadow import market_diagnostic
    f, m = frozen_fixture(tmp_path), market_fixture()
    m[field] = value
    with pytest.raises(ValueError):
        market_diagnostic(f, m)


def test_changed_frozen_forecast_rejected(tmp_path):
    from weatherman.v11.shadow import market_diagnostic
    f = frozen_fixture(tmp_path)
    f["probabilities"] = {"16": 1}
    with pytest.raises(ValueError):
        market_diagnostic(f, market_fixture())


def test_missing_market_price_is_excluded_not_fabricated():
    pytest.importorskip("sklearn")
    from scripts.run_v11_market_proxy import benchmark
    prices = [{"target_date": "2026-11-17", "checkpoint_label": "Late", "historical_point": None,
               "resolution_status": "resolved"}]
    rows = [{"target_date": "2026-11-17", "checkpoint": "Late"}]
    predictions = [{"day": "2026-11-17", "checkpoint": "Late", "candidate": "test"}]
    result = benchmark(prices, rows, predictions, {"Late": "test"})
    assert result["excluded"]["missing_or_stale_price"] == 1
    assert not result["summaries"] and result["official_roi"] is None
