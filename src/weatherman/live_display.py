from __future__ import annotations

from typing import Any


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sum_contributions(nowcast: object, names: tuple[str, ...]) -> float:
    values = dict(getattr(nowcast, "adjustment_contributions", {}) or {})
    return sum(_number(values.get(name)) for name in names)


def forecast_chain_rows(nowcast: object) -> list[dict[str, str]]:
    stages = [
        ("Raw ensemble", _number(getattr(nowcast, "weighted_raw_mean", None))),
        ("Bias-corrected", _number(getattr(getattr(nowcast, "corrected"), "mean"))),
        ("Live weather-adjusted", _number(getattr(nowcast, "metar_conditioned_mean", None))),
        (
            "Champion",
            _number(getattr(nowcast, "final_forecast_mean", None)),
        ),
    ]
    rows: list[dict[str, str]] = []
    previous: float | None = None
    for label, value in stages:
        rows.append(
            {
                "Stage": label,
                "Forecast": f"{value:.2f} °C",
                "Change": "Starting point" if previous is None else f"{value - previous:+.2f} °C",
            }
        )
        previous = value
    return rows


def challenger_rows(nowcast: object) -> list[dict[str, str]]:
    champion_mean = _number(getattr(nowcast, "final_forecast_mean", None))
    champion_spread = _number(getattr(nowcast, "final_forecast_spread", None))
    variants = dict(getattr(nowcast, "challenger_variants", {}) or {})
    rows: list[dict[str, str]] = []
    for name, variant in sorted(variants.items()):
        alternative_mean = _number(variant.get("forecast_mean_c"))
        alternative_spread = _number(variant.get("spread_c"))
        without_factor = name.startswith("Without ")
        delta = (
            champion_mean - alternative_mean
            if without_factor
            else alternative_mean - champion_mean
        )
        if without_factor:
            interpretation = f"Factor's Champion effect {delta:+.2f} °C"
            role = "Live-factor test"
        else:
            interpretation = f"Shadow alternative {delta:+.2f} °C"
            role = "Research only"
        probabilities = dict(variant.get("probabilities") or {})
        top_bucket = (
            str(max(probabilities, key=probabilities.get)) + " °C"
            if probabilities
            else "—"
        )
        rows.append(
            {
                "Variant": name,
                "Alternative forecast": f"{alternative_mean:.2f} °C",
                "Interpretation": interpretation,
                "Spread": f"{alternative_spread:.2f} °C ({alternative_spread - champion_spread:+.2f})",
                "Top bucket": top_bucket,
                "Role": role,
            }
        )
    return rows


def regime_strength_rows(nowcast: object) -> list[dict[str, str]]:
    """Expose continuous regime evidence, including zero and not-applicable states."""
    features = dict(getattr(nowcast, "live_features", {}) or {})
    specs = (
        ("Rapid Heat Ramp", "rapid_heat_ramp", "Bias relaxation and spread"),
        ("Persistent Hot", "persistent_hot", "Bias, spread and regional weights"),
        ("Phase vs Amplitude", "phase_vs_amplitude", "Anchor phase guard and spread"),
        ("Maritime Advection", "maritime_advection", "Cooling center and heating cap"),
        ("Maritime Low Range", "maritime_low_range", "Warm-factor damping and spread"),
        (
            "Post-Convective Uncertainty",
            "post_convective_uncertainty",
            "Spread and confidence only",
        ),
    )
    rows: list[dict[str, str]] = []
    for label, key, role in specs:
        applicable = bool(_number(features.get(f"{key}_applicable")))
        strength = max(0.0, min(1.0, _number(features.get(f"{key}_strength"))))
        active = bool(_number(features.get(f"{key}_active")))
        if not applicable:
            status = "Not applicable"
        elif strength <= 0:
            status = "Evaluated · no evidence"
        elif active:
            status = "Gradual influence"
        else:
            status = "Watch only"
        rows.append(
            {
                "Regime": label,
                "Applicability": "Yes" if applicable else "No",
                "Evidence": f"{strength:.0%}" if applicable else "—",
                "State": status,
                "Possible role": role,
            }
        )
    return rows


def forecast_driver_rows(nowcast: object) -> list[dict[str, str]]:
    features = dict(getattr(nowcast, "live_features", {}) or {})
    corrected = getattr(nowcast, "corrected")
    weighted_mean = _number(getattr(nowcast, "weighted_raw_mean", None))
    corrected_mean = _number(getattr(corrected, "mean", None))
    temperature_path = _sum_contributions(
        nowcast,
        ("temperature_anchor", "heating_rate", "recent_station_error", "run_trend"),
    )
    sky_moisture = _sum_contributions(
        nowcast,
        (
            "dryness",
            "dewpoint_trend",
            "cloud",
            "radiation",
            "late_dry_mixing",
            "failed_convection",
            "clear_sky_override",
        ),
    )
    wind_effect = _sum_contributions(nowcast, ("wind", "maritime_advection"))

    observed_temp = getattr(nowcast, "current_observed_temp", None)
    observed_residual = features.get("effective_temperature_residual_c")
    heating_rate = getattr(nowcast, "heating_rate", None)
    metar_bits = []
    if observed_temp is not None:
        metar_bits.append(f"latest {float(observed_temp):.1f} °C")
    if observed_residual is not None:
        metar_bits.append(f"{float(observed_residual):+.1f} °C vs model path")
    if heating_rate is not None:
        metar_bits.append(f"trend {float(heating_rate):+.1f} °C/h")

    sky_bits = []
    observed_cloud = features.get("observed_cloud_cover_pct")
    dryness = features.get("observed_dryness_c")
    dewpoint = features.get("observed_dewpoint_c")
    if observed_cloud is not None:
        sky_bits.append(f"cloud {float(observed_cloud):.0f}%")
    if dewpoint is not None:
        sky_bits.append(f"dew point {float(dewpoint):.1f} °C")
    if dryness is not None:
        sky_bits.append(f"temperature–dew point spread {float(dryness):.1f} °C")
    if getattr(nowcast, "radiation_wm2", None) is not None:
        sky_bits.append(f"radiation {float(nowcast.radiation_wm2):.0f} W/m²")
    overlap_reduction = features.get("sky_overlap_reduction_c")
    if overlap_reduction is not None and float(overlap_reduction) > 0:
        sky_bits.append(f"overlap guard −{float(overlap_reduction):.2f} °C")

    wind_bits = []
    if getattr(nowcast, "wind_speed_kph", None) is not None:
        wind_bits.append(f"{float(nowcast.wind_speed_kph):.0f} km/h")
    if getattr(nowcast, "wind_direction_deg", None) is not None:
        wind_bits.append(f"from {float(nowcast.wind_direction_deg):.0f}°")
    if getattr(nowcast, "wind_source", None):
        wind_bits.append(str(nowcast.wind_source))

    taf = getattr(nowcast, "taf_guidance", None)
    taf_observation = "No current TAF"
    taf_effect = "No Champion effect"
    if taf is not None:
        tx = f"TX {taf.max_temp_c:.0f} °C · " if taf.max_temp_c is not None else ""
        taf_observation = f"{tx}{taf.agreement} · {taf.cloud_risk}"
        taf_effect = (
            f"center {taf.center_adjustment_c:+.2f} °C · "
            f"spread +{taf.spread_addition_c:.2f} °C"
        )

    variants = dict(getattr(nowcast, "challenger_variants", {}) or {})
    live_variants = [name.removeprefix("Without ") for name in variants if name.startswith("Without ")]
    if live_variants:
        regime_observation = ", ".join(live_variants[:4])
        if len(live_variants) > 4:
            regime_observation += f" +{len(live_variants) - 4} more"
        regime_effect = "Changes center, spread, weights or confidence; see alternatives"
    else:
        regime_observation = "No live regime factor active"
        regime_effect = "No additional Champion effect"

    future = getattr(nowcast, "future_outlook")
    if getattr(
        future,
        "reheating_watch",
        getattr(future, "post_rain_reheating_watch", False),
    ):
        future_effect = (
            f"Champion +0.00 °C · shadow {future.challenger_adjustment_c:+.2f} °C"
        )
    else:
        future_effect = "Already represented in model path · extra +0.00 °C"

    memory = getattr(nowcast, "regime_memory", None)
    if memory is None:
        analog_observation = "No analog assessment"
        analog_effect = "No Champion effect"
    else:
        similarity = (
            f" · best {memory.best_similarity:.0%}"
            if memory.best_similarity is not None
            else ""
        )
        analog_observation = f"{memory.analog_count} comparable days{similarity}"
        if memory.applied_to_champion:
            analog_effect = f"Champion {memory.center_adjustment_c:+.2f} °C"
        elif memory.challenger_ready:
            analog_effect = f"Live +0.00 °C · shadow {memory.center_adjustment_c:+.2f} °C"
        else:
            analog_effect = "Research only · not enough analogs"

    day_status = getattr(nowcast, "day_status")
    if day_status.is_locked:
        day_effect = "Distribution locked to the settled range"
    elif day_status.minimum_bucket is not None:
        day_effect = f"Buckets below {day_status.minimum_bucket} °C removed"
    else:
        day_effect = "No bucket truncation"

    return [
        {
            "Area": "Models & bias",
            "Current observation": (
                f"{len(getattr(nowcast, 'current'))} cadence-valid/used models · "
                f"weighted spread {getattr(nowcast, 'weighted_raw_spread'):.1f} °C"
            ),
            "Effect": f"Base center {corrected_mean - weighted_mean:+.2f} °C",
        },
        {
            "Area": "METAR path",
            "Current observation": " · ".join(metar_bits) if metar_bits else "No live METAR",
            "Effect": f"Center {temperature_path:+.2f} °C",
        },
        {
            "Area": "Moisture, cloud & radiation",
            "Current observation": " · ".join(sky_bits) if sky_bits else "No live detail",
            "Effect": f"Center {sky_moisture:+.2f} °C",
        },
        {
            "Area": "Wind",
            "Current observation": " · ".join(wind_bits) if wind_bits else "No wind detail",
            "Effect": f"Center {wind_effect:+.2f} °C",
        },
        {"Area": "TAF", "Current observation": taf_observation, "Effect": taf_effect},
        {
            "Area": "Live regimes",
            "Current observation": regime_observation,
            "Effect": regime_effect,
        },
        {
            "Area": "Future outlook",
            "Current observation": future.status,
            "Effect": future_effect,
        },
        {
            "Area": "Historical analog Challenger",
            "Current observation": analog_observation,
            "Effect": analog_effect,
        },
        {
            "Area": "Day constraints",
            "Current observation": day_status.label,
            "Effect": day_effect,
        },
    ]


def strongest_driver_summary(rows: list[dict[str, Any]]) -> str:
    """Short explanation line for the cockpit; detailed rows remain expandable."""
    material = [
        row
        for row in rows
        if row.get("Area") in {
            "METAR path",
            "Moisture, cloud & radiation",
            "Wind",
            "TAF",
            "Future outlook",
        }
        and row.get("Current observation")
    ]
    return " · ".join(
        f"{row['Area']}: {row['Current observation']}" for row in material[:3]
    )
