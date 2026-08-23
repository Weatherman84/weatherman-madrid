from __future__ import annotations

from copy import deepcopy


def _merged(defaults: dict, configured: object) -> dict:
    result = deepcopy(defaults)
    if isinstance(configured, dict):
        result.update(deepcopy(configured))
    return result


def continuous_regime_profiles(airport: dict[str, object]) -> dict[str, dict | None]:
    """Return one continuous evidence architecture with airport-specific parameters.

    Heat continuation, curve phase and observed convection are meteorologically relevant
    at every trading airport. Maritime regimes remain applicable only where a defensible
    sea-wind sector is configured; a missing sector is a scientific applicability gate,
    not an implicit zero-strength observation.
    """
    coastal = bool(airport.get("maritime_advection") or airport.get("maritime_low_range"))
    heat = _merged(
        {
            "enabled": True,
            "positive_bias_multiplier": 0.50,
            "spread_multiplier": 1.20,
            "confidence_multiplier": 0.92,
            "regional_models": [],
            "gradual_start_fraction": 0.50,
        },
        airport.get("heat_regime"),
    )
    persistent = _merged(
        {
            "enabled": True,
            "minimum_latest_anomaly_c": 1.5 if coastal else 2.0,
            "maximum_actual_age_days": 1,
            "maximum_forecast_drop_c": 2.5,
            "minimum_recent_warm_error_c": 0.6,
            "minimum_taf_gap_c": 1.0,
            "gradual_start_score": 0.45,
            "gradual_full_score": 0.85,
            "positive_bias_multiplier": 0.35,
            "spread_multiplier": 1.25,
            "confidence_multiplier": 0.92,
        },
        heat.get("persistent_hot"),
    )
    heat["persistent_hot"] = persistent

    phase = _merged(
        {
            "enabled": True,
            "window_hours": 3.5,
            "minimum_reports": 3,
            "minimum_hours_to_peak": 1.0,
            "maximum_phase_shift_hours": 3.0,
            "phase_step_hours": 0.5,
            "minimum_phase_shift_hours": 0.75,
            "minimum_rmse_gain_c": 0.30,
            "maximum_phase_rmse_c": 1.0,
            "phase_anchor_blend": 0.70,
            "center_minimum_reports": 5,
            "center_minimum_rmse_gain_c": 0.55,
            "center_maximum_phase_rmse_c": 0.65,
            "unconfirmed_spread_addition_c": 0.15,
            "unconfirmed_confidence_multiplier": 0.92,
        },
        airport.get("phase_vs_amplitude"),
    )
    convective = _merged(
        {
            "enabled": True,
            "window_hours": 48,
            "minimum_reports": 2,
            "full_strength_hours": 12,
            "spread_multiplier": 1.40,
            "confidence_multiplier": 0.88,
        },
        airport.get("post_convective_uncertainty"),
    )
    return {
        "heat": heat,
        "phase": phase,
        "post_convective": convective,
        "maritime_advection": deepcopy(airport.get("maritime_advection")),
        "maritime_low_range": deepcopy(airport.get("maritime_low_range")),
    }
