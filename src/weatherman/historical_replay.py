"""Offline-only replay adapter and coverage gate. No Production/SQL imports."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .historical_archive import VERSION, ZONE, ArchiveError, eligible, utc
from .nowcast import build_live_nowcast
from .regime_memory import enrich_nowcast_with_regime_memory
from .regime_profiles import continuous_regime_profiles
from .research_tracks import active_regimes, build_trading_shadow_decision, probability_ranking

CHECKPOINTS = (
    ("D-1 Evening @20:00", -1, 20),
    ("D0 Morning @09:00", 0, 9),
    ("First Live @12:00", 0, 12),
    ("Late Live @16:00", 0, 16),
)
CORRECTIONS = {
    "temperature_anchor": "temp_anchor_adjustment_c",
    "dryness": "dryness_adjustment_c",
    "dewpoint_trend": "dewpoint_trend_adjustment_c",
    "cloud": "cloud_adjustment_c",
    "heating_rate": "heating_rate_adjustment_c",
    "recent_station_error": "recent_error_adjustment_c",
    "radiation": "radiation_adjustment_c",
    "wind": "wind_adjustment_c",
    "run_trend": "run_trend_adjustment_c",
    "late_dry_mixing": "late_dry_mixing_adjustment_c",
    "failed_convection": "failed_convection_adjustment_c",
    "clear_sky_override": "clear_sky_override_adjustment_c",
    "total": "live_adjustment_c",
}


def schedule(target: date) -> list[tuple[str, datetime]]:
    result = []
    for label, offset, hour in CHECKPOINTS:
        day = target + timedelta(days=offset)
        result.append(
            (
                label,
                datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(ZONE)).replace(
                    hour=hour
                ),
            )
        )
    return result


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if value is None:
        return None
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return clean(value.item())
    return value


def engine_fingerprint(repo: Path) -> dict:
    paths = [
        *sorted((repo / "src/weatherman").glob("*.py")),
        repo / "src/weatherman/data/airports.json",
        repo / "scripts/historical_replay_backfill.py",
    ]
    return {
        str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    }


def daily_forecasts(paths: pd.DataFrame, *, include_stitched=False) -> pd.DataFrame:
    if paths.empty:
        return pd.DataFrame()
    frame = paths.copy()
    frame["target_date"] = pd.to_datetime(frame.valid_at, utc=True).dt.tz_convert(ZONE).dt.date
    rows = []
    for (_, _, day), group in frame.groupby(["model", "model_run_at", "target_date"]):
        start = pd.Timestamp(day, tz=ZONE).tz_convert("UTC")
        end = pd.Timestamp(day + timedelta(days=1), tz=ZONE).tz_convert("UTC")
        expected = pd.date_range(start, end, freq="h", inclusive="left")
        observed = pd.DatetimeIndex(pd.to_datetime(group.valid_at, utc=True))
        # No maxima from an incomplete model day or short forecast horizon.
        if len(expected.difference(observed)) or group.temp_c.isna().any():
            continue
        row = group.iloc[0].to_dict()
        row.update(
            {
                "target_date": day,
                "max_temp_c": float(group.temp_c.max()),
                "run_at": row["available_at"],
            }
        )
        available_day = utc(row["available_at"]).tz_convert(ZONE).date()
        row["horizon"] = f"D-{(day - available_day).days}" if day > available_day else "D0"
        rows.append(row)
    if include_stitched:
        # Match the daily endpoint's multi-run structure only as a documented
        # partial reconstruction: already eligible runs of the SAME model fill
        # earlier hours absent from the latest archived run. No observations,
        # reanalysis or other model values are used as substitutes.
        for (_, day), group in frame.groupby(["model", "target_date"]):
            latest = group.sort_values("model_run_at").iloc[-1]
            stitched = group.sort_values("model_run_at").drop_duplicates("valid_at", keep="last")
            start = pd.Timestamp(day, tz=ZONE).tz_convert("UTC")
            end = pd.Timestamp(day + timedelta(days=1), tz=ZONE).tz_convert("UTC")
            expected = pd.date_range(start, end, freq="h", inclusive="left")
            observed = pd.DatetimeIndex(pd.to_datetime(stitched.valid_at, utc=True))
            if len(expected.difference(observed)) or stitched.temp_c.isna().any():
                continue
            if any(
                r["model"] == latest.model
                and r["target_date"] == day
                and r["model_run_at"] == latest.model_run_at
                for r in rows
            ):
                continue
            row = latest.to_dict()
            available_day = utc(row["available_at"]).tz_convert(ZONE).date()
            row.update(
                {
                    "target_date": day,
                    "max_temp_c": float(stitched.temp_c.max()),
                    "run_at": row["available_at"],
                    "horizon": f"D-{(day - available_day).days}" if day > available_day else "D0",
                    "daily_aggregation_method": "causal_same_model_multi_run_partial",
                    "daily_aggregation_sources": sorted(set(str(v) for v in stitched.model_run_at)),
                }
            )
            rows.append(row)
    return pd.DataFrame(rows)


def build_checkpoint(
    *,
    airport: dict,
    target: date,
    label: str,
    checkpoint: datetime,
    paths: pd.DataFrame,
    observations: pd.DataFrame,
    tafs: pd.DataFrame,
    actuals: pd.DataFrame,
    timing_policy: str,
    source_errors: list[str],
    history_forecasts: pd.DataFrame | None = None,
) -> dict:
    """Labels are attached AFTER forecasting; only earlier known labels train bias."""
    forecasts = daily_forecasts(
        eligible(paths, checkpoint, policy=timing_policy), include_stitched=True
    )
    if history_forecasts is not None and not history_forecasts.empty:
        history = eligible(history_forecasts, checkpoint, policy=timing_policy)
        history = history[pd.to_datetime(history.target_date).dt.date < target]
        forecasts = pd.concat([history, forecasts], ignore_index=True).drop_duplicates(
            ["model", "model_run_at", "target_date"], keep="last"
        )
    metar = eligible(observations, checkpoint, policy=timing_policy)
    aviation = eligible(tafs, checkpoint, policy=timing_policy)
    prior_actuals = eligible(actuals, checkpoint, policy=timing_policy)
    if not prior_actuals.empty:
        prior_actuals = prior_actuals[
            prior_actuals.actual_complete
            & (pd.to_datetime(prior_actuals.target_date).dt.date < target)
        ].copy()
    hourly = eligible(paths, checkpoint, policy=timing_policy)
    if not hourly.empty:
        hourly["run_at"] = hourly.available_at
    missing = [
        "meteoblue_historical_forecasts",
        "historical_snapshot_memory",
        "historical_terminal_state",
        "temperature_850hPa",
    ]
    warmup_days = (
        sorted(set(pd.to_datetime(prior_actuals.target_date).dt.date))
        if not prior_actuals.empty
        else []
    )
    window_start = target - timedelta(days=90)
    recent_actual_days = {day for day in warmup_days if window_start <= day < target}
    training = (
        forecasts[
            (pd.to_datetime(forecasts.target_date).dt.date >= window_start)
            & (pd.to_datetime(forecasts.target_date).dt.date < target)
            & (forecasts.horizon == "D-1")
        ]
        if not forecasts.empty
        else pd.DataFrame()
    )
    warmup_counts = {
        model: len(
            set(pd.to_datetime(training[training.model == model].target_date).dt.date)
            & recent_actual_days
        )
        if not training.empty
        else 0
        for model in airport["models"]
    }
    if any(count < 90 for count in warmup_counts.values()):
        missing.append("90_day_warmup_incomplete")
    if timing_policy != "strict":
        missing.append("verified_historical_publication_times")
    if not forecasts.empty and "daily_aggregation_method" in forecasts:
        missing.append("exact_single_run_full_local_day_maximum")
    if aviation.empty:
        missing.append("historical_taf_bulletins")
    elif not aviation.decoded_periods_available.all():
        missing.append("decoded_taf_change_groups")
    used = []
    if not forecasts.empty:
        today = forecasts[pd.to_datetime(forecasts.target_date).dt.date == target]
        used = sorted(today.model.unique())
    missing += [f"model:{model}" for model in airport["models"] if model not in used]
    # Empty/missing historical inputs cannot receive a full-fidelity certificate.
    evidence = "historical_partial_fidelity"
    base = {
        "airport": "LEMD",
        "target_date": target.isoformat(),
        "checkpoint_label": label,
        "checkpoint_at": utc(checkpoint).isoformat(),
        "evidence_class": evidence,
        "timing_policy": timing_policy,
        "replay_engine_version": VERSION,
        "production_app_version": "1.0.19",
        "protected_forecast_baseline": "v10.7.10",
        "schema_version": "historical_replay_v1",
        "missing_fields": sorted(set(missing)),
        "source_errors": source_errors,
        "available_models": used,
        "expected_models": [*airport["models"], "meteoblue"],
        "input_coverage": {
            "forecast_rows": len(forecasts),
            "hourly_rows": len(hourly),
            "metar_rows": len(metar),
            "taf_rows": len(aviation),
            "prior_actual_days": len(prior_actuals),
        },
        "warmup": {
            "required_days": 90,
            "paired_days_by_model": warmup_counts,
            "complete": all(count == 90 for count in warmup_counts.values()),
        },
        "research_only": True,
        "automatic_promotion": False,
        "leakage_checks": "passed_for_selected_inputs",
        "causality_certified": False,
        "challenger_status": "blocked_missing_full_fidelity_inputs",
        "aemet_physical_tmax_c": None,
        "resolved_market_bucket": None,
    }
    profiles = continuous_regime_profiles(airport)
    # Preserve historical fetched_at in provenance; do not substitute the later fetch.
    # The engine's freshness assessment reads available_at first.
    nowcast = build_live_nowcast(
        forecasts=forecasts,
        actuals=prior_actuals,
        observations=metar,
        hourly=hourly,
        markets=pd.DataFrame(),
        tafs=aviation,
        timezone_name=ZONE,
        target=target,
        as_of=checkpoint,
        wind_profile=airport.get("heat_wind_profile"),
        routine_metar_minutes=airport.get("metar_minutes"),
        pre_metar_guard_minutes=airport.get("pre_metar_guard_minutes", 7),
        critical_window_local=airport.get("critical_window_local"),
        post_convective_profile=profiles["post_convective"],
        heat_regime_profile=profiles["heat"],
        phase_amplitude_profile=profiles["phase"],
        maritime_advection_profile=profiles["maritime_advection"],
        maritime_low_range_profile=profiles["maritime_low_range"],
        live_adjustment_guardrails=airport.get("live_adjustment_guardrails"),
        recent_warm_bias_profile=airport.get("recent_warm_bias_challenger"),
        future_reheating_profile=airport.get("future_reheating"),
        maximum_model_age_minutes=90,
    )
    nowcast = enrich_nowcast_with_regime_memory(
        nowcast,
        pd.DataFrame(),
        prior_actuals,
        metar,
        pd.DataFrame(),
        airport_profile=airport,
        timezone_name=ZONE,
        target=target,
        as_of=checkpoint,
        config={**dict(airport.get("regime_memory") or {}), "allow_promoted": False},
    )
    if nowcast is None or nowcast.forecast_data_stale:
        base["status"] = "unavailable_fewer_than_two_fresh_models"
    else:
        ranked = probability_ranking(nowcast.probabilities)
        center = nowcast.final_forecast_mean
        corrections = {
            field: nowcast.adjustment_contributions.get(name) for name, field in CORRECTIONS.items()
        }
        corrections["base_bias_adjustment_c"] = nowcast.corrected.mean - nowcast.weighted_raw_mean
        corrections["taf_adjustment_c"] = nowcast.taf_adjustment_c
        for name in ("rapid_heat_ramp", "regional_cluster", "persistent_hot", "maritime_advection"):
            corrections[f"{name}_adjustment_c"] = nowcast.live_features.get(f"{name}_adjustment_c")
        corrections["phase_anchor_delta_c"] = nowcast.live_features.get("phase_anchor_delta_c")
        base.update(
            {
                "status": "diagnostic_partial_replay",
                "champion_center_c": center,
                "champion_modal_bucket": ranked[0][0],
                "modal_bucket": ranked[0][0],
                "champion_probabilities": nowcast.probabilities,
                "probability_window": {"buckets": nowcast.probabilities, "complete": True},
                "forecast_stages": {
                    "raw": nowcast.raw_model_mean,
                    "weighted_raw": nowcast.weighted_raw_mean,
                    "bias": nowcast.corrected.mean,
                    "live": nowcast.metar_conditioned_mean,
                    "pre_taf": nowcast.metar_conditioned_mean,
                    "final": center,
                },
                "stage_probabilities": nowcast.stage_probabilities,
                "top1_top2_gap_pp": (ranked[0][1] - ranked[1][1]) * 100
                if len(ranked) > 1
                else None,
                "bucket_boundary_distance_c": abs(center - (math.floor(center) + 0.5)),
                "model_spread_c": nowcast.raw_model_spread,
                "raw_spread_c": nowcast.raw_model_spread,
                "final_spread_c": nowcast.final_forecast_spread,
                "model_inputs": forecasts[
                    pd.to_datetime(forecasts.target_date).dt.date == target
                ].to_dict("records"),
                "used_models": list(nowcast.current.model),
                "model_weights": nowcast.model_weights,
                "correction_contributions": corrections,
                "regime_raw_inputs": nowcast.live_features,
                "features_json": nowcast.live_features,
                "forecast_confidence": nowcast.forecast_confidence,
                "taf_bucket": None,
                "taf_history": aviation.to_dict("records"),
                "engine_challenger_variants": nowcast.challenger_variants,
            }
        )
        for index, (bucket, probability) in enumerate(ranked[:3], 1):
            base[f"top{index}_bucket"] = bucket
            base[f"top{index}_probability"] = probability
        guidance = nowcast.taf_guidance
        if guidance is not None and guidance.max_temp_c is not None:
            base["taf_bucket"] = math.floor(guidance.max_temp_c + 0.5)
        for field in (
            "rapid_heat_ramp_active",
            "regional_cluster_active",
            "persistent_hot_active",
            "phase_vs_amplitude_active",
            "maritime_advection_active",
        ):
            base[field] = bool(nowcast.live_features.get(field))
        base.update(corrections)
        base["active_regimes"] = active_regimes(base)
        base["trading_shadow"] = build_trading_shadow_decision(
            target_date=target.isoformat(),
            checkpoint=label,
            generated_at=utc(checkpoint).isoformat(),
            probabilities=nowcast.probabilities,
            taf_bucket=base["taf_bucket"],
            market_checkpoint=None,
            evidence_class=evidence,
            forecast_confidence=nowcast.forecast_confidence,
            regimes=base["active_regimes"],
        )
        base["trading_shadow"]["execution_status"] = "NO_TRADE_missing_executable_quotes"
    # Evaluation labels do not exist in engine inputs for this target date.
    labels = (
        actuals[pd.to_datetime(actuals.target_date).dt.date == target]
        if not actuals.empty
        else actuals
    )
    if labels.empty:
        base.update(
            {"stored_metar_actual_c": None, "actual_bucket": None, "actual_complete": False}
        )
    else:
        label_row = labels.iloc[-1]
        actual = label_row.max_temp_c
        base.update(
            {
                "stored_metar_actual_c": actual if pd.notna(actual) else None,
                "actual_bucket": math.floor(float(actual) + 0.5) if pd.notna(actual) else None,
                "archive_metar_max_c": label_row.archive_metar_max_c,
                "actual_source": label_row.source,
                "actual_complete": bool(label_row.actual_complete),
                "actual_available_at": label_row.available_at,
                "actual_report_coverage": label_row.to_dict(),
            }
        )
    return clean(base)


def metrics(rows: list[dict]) -> dict:
    evaluated = [
        r
        for r in rows
        if r.get("actual_bucket") is not None and r.get("champion_center_c") is not None
    ]
    if not evaluated:
        return {"n": 0}
    errors = [r["champion_center_c"] - r["stored_metar_actual_c"] for r in evaluated]
    scores = []
    for row in evaluated:
        probabilities = {int(k): v for k, v in row["champion_probabilities"].items()}
        if abs(sum(probabilities.values()) - 1) > 1e-6:
            raise ArchiveError("Invalid probability mass")
        actual = row["actual_bucket"]
        scores.append(
            (
                sum(
                    (probabilities.get(k, 0) - int(k == actual)) ** 2
                    for k in set(probabilities) | {actual}
                ),
                -math.log(max(1e-12, probabilities.get(actual, 0))),
            )
        )
    n = len(evaluated)
    result = {
        "n": n,
        "modal_accuracy": sum(r["champion_modal_bucket"] == r["actual_bucket"] for r in evaluated)
        / n,
        "mae_c": sum(abs(e) for e in errors) / n,
        "signed_bias_c": sum(errors) / n,
        "underforecast_pct": sum(e < 0 for e in errors) / n * 100,
        "overforecast_pct": sum(e > 0 for e in errors) / n * 100,
        "brier_multiclass": sum(s[0] for s in scores) / n,
        "log_loss": sum(s[1] for s in scores) / n,
    }
    for top in (2, 3):
        result[f"top{top}_coverage"] = (
            sum(
                r["actual_bucket"]
                in [k for k, _ in probability_ranking(r["champion_probabilities"])[:top]]
                for r in evaluated
            )
            / n
        )
    result["calibration"] = []
    for band in range(10):
        group = [
            r
            for r in evaluated
            if band / 10 <= r["top1_probability"] <= (band + 1) / 10
            and (band == 9 or r["top1_probability"] < (band + 1) / 10)
        ]
        if group:
            result["calibration"].append(
                {
                    "band": band,
                    "n": len(group),
                    "mean_probability": sum(r["top1_probability"] for r in group) / len(group),
                    "hit_rate": sum(r["champion_modal_bucket"] == r["actual_bucket"] for r in group)
                    / len(group),
                }
            )
    return result


def summary(rows: list[dict]) -> dict:
    output = {
        "research_version": VERSION,
        "research_only": True,
        "automatic_promotion": False,
        "production_neon_used": False,
        "production_reads": 0,
        "production_writes": 0,
        "target_days": len({r["target_date"] for r in rows}),
        "checkpoints": len(rows),
        "evidence_counts": dict(Counter(r["evidence_class"] for r in rows)),
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "missing_field_counts": dict(Counter(f for r in rows for f in r["missing_fields"])),
        "baseline_tournament": [],
        "regime_tournament": [],
        "challenger_tournament": {"status": "blocked_full_fidelity_not_available"},
        "counterfactual_upper_bound": {"status": "not_run_quality_gate_failed"},
        "holdout": {"status": "not_opened"},
        "walk_forward": {"status": "not_run"},
        "trading_replay": {"status": "not_run_missing_executable_quotes"},
    }
    for evidence in sorted({r["evidence_class"] for r in rows}):
        for checkpoint, _, _ in CHECKPOINTS:
            group = [
                r
                for r in rows
                if r["evidence_class"] == evidence and r["checkpoint_label"] == checkpoint
            ]
            output["baseline_tournament"].append(
                {
                    "evidence_class": evidence,
                    "checkpoint": checkpoint,
                    "diagnostic_only": evidence != "historical_full_fidelity",
                    **metrics(group),
                }
            )
            for regime in sorted({g for r in group for g in r.get("active_regimes", [])}):
                output["regime_tournament"].append(
                    {
                        "evidence_class": evidence,
                        "checkpoint": checkpoint,
                        "regime": regime,
                        "diagnostic_only": True,
                        **metrics([r for r in group if regime in r.get("active_regimes", [])]),
                    }
                )
    output["full_fidelity_gate_passed"] = bool(rows) and all(
        r["evidence_class"] == "historical_full_fidelity"
        and r.get("actual_complete")
        and r.get("causality_certified")
        and r.get("champion_center_c") is not None
        for r in rows
    )
    labels = {label for label, _, _ in CHECKPOINTS}
    dates = {r["target_date"] for r in rows}
    output["diagnostic_gate_passed"] = (
        len(dates) >= 3
        and len(rows) == len(dates) * len(labels)
        and len({(r["target_date"], r["checkpoint_label"]) for r in rows}) == len(rows)
        and all(
            {r["checkpoint_label"] for r in rows if r["target_date"] == day} == labels
            for day in dates
        )
        and all(
            r.get("actual_complete")
            and r.get("champion_center_c") is not None
            and r.get("champion_probabilities")
            and r.get("leakage_checks") == "passed_for_selected_inputs"
            and r.get("available_models")
            and r.get("research_only") is True
            and r.get("automatic_promotion") is False
            and r.get("evidence_class")
            in {"historical_partial_fidelity", "historical_full_fidelity"}
            for r in rows
        )
    )
    output["dry_run_passed"] = output["diagnostic_gate_passed"]
    output["large_backfill_status"] = (
        "ready_diagnostic_only" if output["dry_run_passed"] else "blocked_quality_gate"
    )
    return clean(output)


def save_records(root: Path, rows: list[dict]):
    """Nested payloads in JSON columns keep heterogeneous probability windows readable."""
    derived = root / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        [
            {
                k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
                for k, v in row.items()
            }
            for row in rows
        ]
    )
    temporary = derived / "historical-replay-master.tmp.parquet"
    frame.to_parquet(temporary, index=False, compression="zstd")
    temporary.replace(derived / "historical-replay-master.parquet")
    return derived / "historical-replay-master.parquet"
