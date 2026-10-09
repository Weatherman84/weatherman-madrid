from datetime import date, datetime, timedelta, timezone
import json
import subprocess

import httpx
import pandas as pd
import pytest

from weatherman.historical_archive import (
    ArchiveError,
    BudgetExceeded,
    RawCache,
    actuals_from_metars,
    candidate_runs,
    eligible,
    group_time,
)
from weatherman.historical_replay import (
    build_checkpoint,
    daily_forecasts,
    metrics,
    schedule,
    summary,
)


def test_local_checkpoints_follow_dst():
    summer = schedule(date(2026, 7, 1))
    winter = schedule(date(2026, 1, 1))
    assert summer[0][1].astimezone(timezone.utc).hour == 18
    assert winter[0][1].astimezone(timezone.utc).hour == 19
    assert summer[0][1].date() == date(2026, 6, 30)
    assert [cutoff.hour for _, cutoff in summer] == [20, 9, 12, 16]


def test_run_initialisation_is_not_publication():
    cutoff = datetime(2026, 9, 10, 7, tzinfo=timezone.utc)
    runs = candidate_runs("gfs_global", cutoff)
    assert runs[0].hour == 0
    assert all(run + timedelta(hours=6, minutes=10) <= cutoff for run in runs)


def test_strict_rejects_schedule_assumptions_and_conservative_is_explicit():
    cutoff = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    frame = pd.DataFrame(
        [
            {
                "model_run_at": cutoff - timedelta(hours=8),
                "available_at": cutoff,
                "availability_basis": "estimated_conservative_schedule",
            },
            {
                "model_run_at": cutoff - timedelta(hours=8),
                "available_at": cutoff + timedelta(seconds=1),
                "availability_basis": "verified_historical_availability",
            },
            {
                "model_run_at": cutoff - timedelta(hours=8),
                "available_at": None,
                "availability_basis": "verified_historical_availability",
            },
        ]
    )
    assert eligible(frame, cutoff, policy="strict").empty
    assert len(eligible(frame, cutoff, policy="conservative")) == 1


@pytest.mark.parametrize("column", ["model_run_at", "issue_time", "observed_at"])
def test_future_source_timestamp_fails_closed(column):
    cutoff = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    frame = pd.DataFrame(
        [
            {
                column: cutoff + timedelta(seconds=1),
                "available_at": cutoff,
                "availability_basis": "verified_historical_availability",
            }
        ]
    )
    with pytest.raises(ArchiveError, match="Leakage"):
        eligible(frame, cutoff, policy="strict")


def test_correction_revision_with_unknown_reception_is_excluded():
    cutoff = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    frame = pd.DataFrame(
        [
            {
                "observed_at": cutoff,
                "available_at": cutoff,
                "availability_basis": "verified_historical_availability",
                "revision_conflict": True,
                "is_correction": False,
            }
        ]
    )
    assert eligible(frame, cutoff, policy="strict").empty


def observation_day(day):
    times = pd.date_range(
        pd.Timestamp(day, tz="Europe/Madrid"),
        pd.Timestamp(day + timedelta(days=1), tz="Europe/Madrid"),
        freq="30min",
        inclusive="left",
    ).tz_convert("UTC")
    return pd.DataFrame(
        {"observed_at": times, "temp_c": [20.0] * len(times), "revision_conflict": False}
    )


def test_actual_requires_every_routine_half_hour_and_accounts_for_dst():
    day = date(2026, 10, 25)
    full = observation_day(day)
    actual = actuals_from_metars(full).iloc[0]
    assert actual.actual_complete and actual.expected_reports == 50
    full.loc[1, "temp_c"] = 30
    # Omitting one short peak cannot create a fake settlement-grade Actual.
    incomplete = actuals_from_metars(full.drop(index=1)).iloc[0]
    assert not incomplete.actual_complete
    assert pd.isna(incomplete.max_temp_c)
    assert incomplete.archive_metar_max_c == 20


def test_taf_hour24_rollover():
    issue = datetime(2026, 9, 30, 23, tzinfo=timezone.utc)
    assert group_time(issue, 30, 24) == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_model_maximum_requires_full_local_day():
    day = date(2026, 9, 10)
    start = pd.Timestamp(day, tz="Europe/Madrid").tz_convert("UTC")
    run = start - timedelta(hours=10)
    frame = pd.DataFrame(
        {
            "valid_at": pd.date_range(start, periods=24, freq="h"),
            "model": "gfs_global",
            "model_run_at": run,
            "available_at": run + timedelta(hours=6),
            "temp_c": [20.0] * 24,
        }
    )
    assert len(daily_forecasts(frame)) == 1
    assert daily_forecasts(frame.iloc[1:]).empty


def test_same_model_stitch_retains_earlier_hours_and_records_both_runs():
    start = pd.Timestamp("2026-09-10", tz="Europe/Madrid").tz_convert("UTC")
    old = start - timedelta(hours=8)
    new = start + timedelta(hours=2)
    first = pd.DataFrame(
        {
            "valid_at": pd.date_range(start, periods=24, freq="h"),
            "model": "gfs_global",
            "model_run_at": old,
            "available_at": old + timedelta(hours=6),
            "temp_c": [30.0] + [20.0] * 23,
        }
    )
    second = pd.DataFrame(
        {
            "valid_at": pd.date_range(new, periods=22, freq="h"),
            "model": "gfs_global",
            "model_run_at": new,
            "available_at": new + timedelta(hours=6),
            "temp_c": [25.0] * 22,
        }
    )
    result = daily_forecasts(pd.concat([first, second]), include_stitched=True)
    current = result.sort_values("run_at").iloc[-1]
    assert current.max_temp_c == 30.0
    assert current.daily_aggregation_method == "causal_same_model_multi_run_partial"
    assert len(current.daily_aggregation_sources) == 2


def test_modal_accuracy_is_not_center_rounding():
    row = {
        "champion_center_c": 20.6,
        "champion_modal_bucket": 20,
        "stored_metar_actual_c": 20.0,
        "actual_bucket": 20,
        "champion_probabilities": {20: 0.7, 21: 0.3},
        "top1_probability": 0.7,
    }
    result = metrics([row])
    assert result["modal_accuracy"] == 1
    assert result["mae_c"] == pytest.approx(0.6)
    assert result["brier_multiclass"] == pytest.approx(0.18)


def test_partial_never_unlocks_backfill():
    row = {
        "target_date": "2026-09-10",
        "evidence_class": "historical_partial_fidelity",
        "status": "diagnostic_partial_replay",
        "missing_fields": ["verified_publication"],
        "checkpoint_label": "First Live @12:00",
        "actual_complete": True,
    }
    assert not summary([row])["dry_run_passed"]
    assert summary([])["large_backfill_status"] == "blocked_quality_gate"


def test_cache_no_second_download_and_checksum_corruption_fails(tmp_path):
    cache = RawCache(tmp_path)
    payload = json.dumps({"hourly": {"time": ["2026-09-10T00:00"]}}).encode()
    cache.client.close()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=payload)

    cache.client = httpx.Client(transport=httpx.MockTransport(handler))
    first, meta = cache.get("https://example.test/model", {}, kind="json")
    assert cache.get("https://example.test/model", {}, kind="json")[0] == first
    assert len(calls) == 1 and cache.hits == 1
    (tmp_path / meta["path"]).write_bytes(b"corrupt")
    with pytest.raises(ArchiveError, match="checksum"):
        cache.get("https://example.test/model", {}, kind="json")
    cache.close()


def test_budget_zero_forbids_download(tmp_path):
    cache = RawCache(tmp_path, max_requests=0)
    with pytest.raises(BudgetExceeded):
        cache.get("https://example.test/model", {}, kind="json")
    cache.close()


def test_database_runtime_never_imported():
    result = subprocess.run(
        [
            "python",
            "-c",
            (
                "import weatherman.historical_replay; import sys; "
                "assert 'weatherman.db' not in sys.modules; "
                "assert 'weatherman.service' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_current_and_future_actual_cannot_change_checkpoint_prediction():
    day = date(2026, 9, 10)
    checkpoint = schedule(day)[1][1]
    start = pd.Timestamp(day, tz="Europe/Madrid").tz_convert("UTC")
    run = start + timedelta(hours=2)
    frames = []
    for model in ("gfs_global", "ecmwf_ifs025"):
        frames.append(
            pd.DataFrame(
                {
                    "valid_at": pd.date_range(start, periods=24, freq="h"),
                    "model": model,
                    "model_run_at": run,
                    "available_at": run + timedelta(hours=6),
                    "availability_basis": "verified_historical_availability",
                    "fetched_at": run,
                    "source": "open-meteo",
                    "airport": "LEMD",
                    "temp_c": [20.0] * 24,
                    "dewpoint_c": 5.0,
                    "cloud_cover": 0.0,
                    "wind_kph": 5.0,
                    "wind_direction": 180.0,
                    "radiation_wm2": 600.0,
                    "temp_850hpa_c": None,
                }
            )
        )
    paths = pd.concat(frames, ignore_index=True)
    actuals = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "target_date": day,
                "max_temp_c": 20.0,
                "archive_metar_max_c": 20.0,
                "actual_complete": True,
                "available_at": start + timedelta(days=1),
                "source": "stored-metar-compatible-archive",
                "availability_basis": "verified_historical_availability",
            },
            {
                "airport": "LEMD",
                "target_date": day + timedelta(days=1),
                "max_temp_c": 90.0,
                "archive_metar_max_c": 90.0,
                "actual_complete": True,
                "available_at": start + timedelta(days=2),
                "source": "stored-metar-compatible-archive",
                "availability_basis": "verified_historical_availability",
            },
        ]
    )
    kwargs = dict(
        airport={"models": ["gfs_global", "ecmwf_ifs025"]},
        target=day,
        label="D0 Morning @09:00",
        checkpoint=checkpoint,
        paths=paths,
        observations=pd.DataFrame(),
        tafs=pd.DataFrame(),
        timing_policy="strict",
        source_errors=[],
    )
    before = build_checkpoint(actuals=actuals, **kwargs)
    actuals["max_temp_c"] = 99.0
    after = build_checkpoint(actuals=actuals, **kwargs)
    assert before["champion_probabilities"] == after["champion_probabilities"]
    assert before["champion_center_c"] == after["champion_center_c"]
