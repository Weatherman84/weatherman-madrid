import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from weatherman.regime_memory import (
    enrich_nowcast_with_regime_memory,
    evaluate_promotion_gate,
    find_analog_days,
)


def feature_payload(*, residual: float = 0.5, wind: float = 240.0) -> str:
    return json.dumps(
        {
            "effective_temperature_residual_c": residual,
            "dryness_surprise_c": 2.0,
            "observed_heating_rate_60m_cph": 2.0,
            "heating_rate_surprise_cph": 0.4,
            "cloud_surprise_pct": -10.0,
            "wind_speed_kph": 15.0,
            "wind_direction_deg": wind,
            "remaining_model_rise_c": 2.0,
        }
    )


def historical_snapshots(target: date) -> pd.DataFrame:
    rows = []
    for offset, residual in [(3, 0.4), (2, 0.5), (1, 0.6)]:
        day = target - timedelta(days=offset)
        rows.append(
            {
                "airport": "TEST",
                "target_date": day,
                "captured_at": datetime.combine(
                    day,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                )
                + timedelta(hours=11),
                "day_phase": "heating",
                "hours_to_peak": 3.0,
                "final_forecast_c": 30.0,
                "final_spread_c": 0.8,
                "taf_adjustment_c": 0.0,
                "features_json": feature_payload(residual=residual),
            }
        )
    rows.append(
        {
            "airport": "TEST",
            "target_date": target + timedelta(days=1),
            "captured_at": datetime.combine(
                target + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ),
            "day_phase": "heating",
            "hours_to_peak": 3.0,
            "final_forecast_c": 10.0,
            "final_spread_c": 0.8,
            "taf_adjustment_c": 0.0,
            "features_json": feature_payload(residual=0.5),
        }
    )
    return pd.DataFrame(rows)


def historical_actuals(target: date) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "airport": "TEST",
                "target_date": target - timedelta(days=offset),
                "max_temp_c": 31.0,
            }
            for offset in (3, 2, 1)
        ]
        + [
            {
                "airport": "TEST",
                "target_date": target + timedelta(days=1),
                "max_temp_c": 50.0,
            }
        ]
    )


def test_analog_search_is_target_date_safe_and_explainable():
    target = date(2026, 8, 1)
    analogs = find_analog_days(
        historical_snapshots(target),
        historical_actuals(target),
        current_signature={
            "effective_temperature_residual_c": 0.5,
            "dryness_surprise_c": 2.0,
            "observed_heating_rate_60m_cph": 2.0,
            "heating_rate_surprise_cph": 0.4,
            "cloud_surprise_pct": -10.0,
            "wind_speed_kph": 15.0,
            "wind_direction_deg": 240.0,
            "remaining_model_rise_c": 2.0,
            "hours_to_peak": 3.0,
            "final_spread_c": 0.8,
            "taf_adjustment_c": 0.0,
        },
        target=target,
        current_phase="heating",
    )
    assert len(analogs) == 3
    assert all(date.fromisoformat(item.target_date) < target for item in analogs)
    assert all(item.residual_c == 1.0 for item in analogs)
    assert all(item.matched_on for item in analogs)


def test_promotion_gate_needs_and_rewards_sequential_oos_days():
    start = date(2026, 6, 1)
    variants = []
    actuals = []
    for index in range(30):
        target = start + timedelta(days=index)
        captured = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc) + timedelta(
            hours=12
        )
        actual = 30.0 + (index % 3)
        actuals.append({"airport": "TEST", "target_date": target, "max_temp_c": actual})
        variants.extend(
            [
                {
                    "airport": "TEST",
                    "target_date": target,
                    "captured_at": captured,
                    "timing": "D0 live",
                    "variant": "Champion",
                    "factor": None,
                    "forecast_c": actual - 1.0,
                    "probabilities_json": json.dumps({str(round(actual - 1)): 0.8, str(round(actual)): 0.2}),
                },
                {
                    "airport": "TEST",
                    "target_date": target,
                    "captured_at": captured,
                    "timing": "D0 live",
                    "variant": "Analog Memory Challenger",
                    "factor": "regime_memory_analog",
                    "forecast_c": actual,
                    "probabilities_json": json.dumps({str(round(actual)): 0.8, str(round(actual - 1)): 0.2}),
                },
            ]
        )
    gate = evaluate_promotion_gate(
        pd.DataFrame(variants),
        pd.DataFrame(actuals),
        timing_group="D0 live",
    )
    assert gate.oos_days == 30
    assert gate.eligible
    assert gate.status == "AUTO-PROMOTION ELIGIBLE"
    assert gate.mae_gain_c == 1.0
    assert gate.brier_gain is not None and gate.brier_gain > 0


def test_promotion_gate_rolls_back_after_recent_deterioration():
    start = date(2026, 6, 1)
    variants = []
    actuals = []
    for index in range(30):
        target = start + timedelta(days=index)
        captured = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
        actual = 30.0
        challenger = actual if index < 20 else actual - 2.0
        challenger_probabilities = (
            {"30": 0.8, "29": 0.2}
            if index < 20
            else {"28": 0.8, "30": 0.2}
        )
        actuals.append({"airport": "TEST", "target_date": target, "max_temp_c": actual})
        variants.extend(
            [
                {
                    "airport": "TEST",
                    "target_date": target,
                    "captured_at": captured,
                    "timing": "D0 live",
                    "variant": "Champion",
                    "factor": None,
                    "forecast_c": actual - 1.0,
                    "probabilities_json": json.dumps({"29": 0.8, "30": 0.2}),
                },
                {
                    "airport": "TEST",
                    "target_date": target,
                    "captured_at": captured,
                    "timing": "D0 live",
                    "variant": "Analog Memory Challenger",
                    "factor": "regime_memory_analog",
                    "forecast_c": challenger,
                    "probabilities_json": json.dumps(challenger_probabilities),
                },
            ]
        )

    gate = evaluate_promotion_gate(
        pd.DataFrame(variants),
        pd.DataFrame(actuals),
        timing_group="D0 live",
    )

    assert gate.mae_gain_c is not None and gate.mae_gain_c > 0
    assert not gate.eligible
    assert "rolled back automatically" in gate.explanation


@dataclass(frozen=True)
class DummyNowcast:
    live_features: dict[str, object]
    wind_speed_kph: float
    wind_direction_deg: float
    hours_to_peak: float
    taf_adjustment_c: float
    final_forecast_mean: float
    final_forecast_spread: float
    day_status: object
    challenger_variants: dict[str, dict[str, object]]
    probabilities: dict[int, float]
    forecast_confidence: int
    adjustment_contributions: dict[str, float]
    stage_probabilities: dict[str, dict[int, float]]
    taf_guidance: object | None = None
    regime_memory: object | None = None


def test_learned_memory_stays_challenger_only_by_default():
    target = date(2026, 8, 1)
    nowcast = DummyNowcast(
        live_features={
            "effective_temperature_residual_c": 0.5,
            "dryness_surprise_c": 2.0,
            "observed_heating_rate_60m_cph": 2.0,
            "heating_rate_surprise_cph": 0.4,
            "cloud_surprise_pct": -10.0,
            "remaining_model_rise_c": 2.0,
        },
        wind_speed_kph=15.0,
        wind_direction_deg=240.0,
        hours_to_peak=3.0,
        taf_adjustment_c=0.0,
        final_forecast_mean=30.0,
        final_forecast_spread=0.8,
        day_status=SimpleNamespace(
            phase="heating",
            minimum_bucket=None,
            maximum_bucket=None,
        ),
        challenger_variants={},
        probabilities={29: 0.2, 30: 0.6, 31: 0.2},
        forecast_confidence=72,
        adjustment_contributions={"total": 0.0},
        stage_probabilities={},
    )
    enriched = enrich_nowcast_with_regime_memory(
        nowcast,
        historical_snapshots(target),
        historical_actuals(target),
        pd.DataFrame(),
        pd.DataFrame(),
        airport_profile={},
        timezone_name="UTC",
        target=target,
        as_of=datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
    )
    assert enriched is not None
    assert enriched.final_forecast_mean == 30.0
    assert enriched.regime_memory.shadow_only
    assert enriched.regime_memory.center_adjustment_c > 0
    assert "Analog Memory Challenger" in enriched.challenger_variants
    assert (
        enriched.challenger_variants["Analog Memory Challenger"]["forecast_mean_c"]
        > enriched.final_forecast_mean
    )


def test_guarded_automatic_promotion_applies_without_manual_review():
    target = date(2026, 8, 1)
    variants = []
    promotion_actuals = []
    for index in range(30):
        day = date(2026, 6, 1) + timedelta(days=index)
        captured = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(
            hours=12
        )
        promotion_actuals.append(
            {"airport": "TEST", "target_date": day, "max_temp_c": 31.0}
        )
        variants.extend(
            [
                {
                    "airport": "TEST",
                    "target_date": day,
                    "captured_at": captured,
                    "timing": "D0 live",
                    "variant": "Champion",
                    "factor": None,
                    "forecast_c": 30.0,
                    "probabilities_json": json.dumps({"30": 0.8, "31": 0.2}),
                },
                {
                    "airport": "TEST",
                    "target_date": day,
                    "captured_at": captured,
                    "timing": "D0 live",
                    "variant": "Analog Memory Challenger",
                    "factor": "regime_memory_analog",
                    "forecast_c": 31.0,
                    "probabilities_json": json.dumps({"31": 0.8, "30": 0.2}),
                },
            ]
        )
    nowcast = DummyNowcast(
        live_features={
            "effective_temperature_residual_c": 0.5,
            "dryness_surprise_c": 2.0,
            "observed_heating_rate_60m_cph": 2.0,
            "heating_rate_surprise_cph": 0.4,
            "cloud_surprise_pct": -10.0,
            "remaining_model_rise_c": 2.0,
        },
        wind_speed_kph=15.0,
        wind_direction_deg=240.0,
        hours_to_peak=3.0,
        taf_adjustment_c=0.0,
        final_forecast_mean=30.0,
        final_forecast_spread=0.8,
        day_status=SimpleNamespace(phase="heating", minimum_bucket=None, maximum_bucket=None),
        challenger_variants={},
        probabilities={29: 0.2, 30: 0.6, 31: 0.2},
        forecast_confidence=72,
        adjustment_contributions={"total": 0.0},
        stage_probabilities={},
    )
    actuals = pd.concat(
        [pd.DataFrame(promotion_actuals), historical_actuals(target)],
        ignore_index=True,
    )

    enriched = enrich_nowcast_with_regime_memory(
        nowcast,
        historical_snapshots(target),
        actuals,
        pd.DataFrame(),
        pd.DataFrame(variants),
        airport_profile={},
        timezone_name="UTC",
        target=target,
        as_of=datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
        config={"allow_promoted": True},
    )

    assert enriched.regime_memory.applied_to_champion
    assert not enriched.regime_memory.shadow_only
    assert enriched.final_forecast_mean > 30.0
    assert "Without Promoted Regime Memory" in enriched.challenger_variants
