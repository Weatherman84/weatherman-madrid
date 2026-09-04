from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from weatherman.analytics import fixed_checkpoint_reliability
from weatherman.collector import (
    _collection_mode,
    _declared_slots_for_day,
    _expected_slot_at,
)


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from run_madrid_replay_pilot import replay_evidence  # noqa: E402


def test_fixed_reliability_explains_why_n_does_not_increase() -> None:
    snapshots = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "target_date": date(2026, 8, 20),
                "captured_at": datetime(2026, 8, 20, 7, tzinfo=timezone.utc),
                "checkpoint_label": "D0 Morning @09:00",
                "checkpoint_status": "scheduled-causal",
                "checkpoint_reconstructed": False,
                "hours_to_peak": 8.0,
                "final_forecast_c": 37.2,
            },
            {
                "airport": "LEMD",
                "target_date": date(2026, 8, 21),
                "captured_at": datetime(2026, 8, 21, 7, tzinfo=timezone.utc),
                "checkpoint_label": "D0 Morning @09:00",
                "checkpoint_status": "reconstructed-causal",
                "checkpoint_reconstructed": True,
                "hours_to_peak": 8.0,
                "final_forecast_c": 39.0,
            },
            {
                "airport": "LEMD",
                "target_date": date(2026, 8, 20),
                "captured_at": datetime(2026, 8, 20, 14, tzinfo=timezone.utc),
                "checkpoint_label": "Late Live @16:00",
                "checkpoint_status": "scheduled-causal",
                "checkpoint_reconstructed": False,
                "hours_to_peak": -0.5,
                "final_forecast_c": 37.0,
            },
        ]
    )
    actuals = pd.DataFrame(
        [
            {
                "airport": "LEMD",
                "target_date": date(2026, 8, 20),
                "max_temp_c": 37.0,
                "source": "stored-metar-station",
            },
            {
                "airport": "LEMD",
                "target_date": date(2026, 8, 21),
                "max_temp_c": 38.0,
                "source": "stored-metar-station",
            },
            {
                "airport": "LEMD",
                "target_date": date(2026, 8, 22),
                "max_temp_c": 39.0,
                "source": "metar-provisional",
            },
        ]
    )

    result = fixed_checkpoint_reliability(snapshots, actuals).set_index("checkpoint")
    morning = result.loc["D0 Morning @09:00"]
    assert morning.n == 1
    assert morning.exact_bucket == 1.0
    assert morning.scheduled_days == 1
    assert morning.reconstructed_days == 1
    assert morning.provisional_days == 1
    late = result.loc["Late Live @16:00"]
    assert late.n == 0
    assert late.late_post_peak_days == 1


def test_replay_evidence_distinguishes_stored_from_reconstructed_inputs() -> None:
    cutoff = datetime(2026, 8, 20, 7, tzinfo=timezone.utc)
    stored = {
        "checkpoint_status": "reconstructed-causal",
        "source_provenance_json": json.dumps(
            [
                {"relevant_to_checkpoint": True, "fetched_at": "2026-08-20T06:50:00Z"},
                {"relevant_to_checkpoint": True, "fetched_at": "2026-08-20T06:55:00Z"},
            ]
        ),
    }
    assert replay_evidence(stored, cutoff) == "historical-causal"
    reconstructed = dict(stored)
    reconstructed["source_provenance_json"] = json.dumps(
        [
            {"relevant_to_checkpoint": True, "fetched_at": "2026-08-20T06:50:00Z"},
            {"relevant_to_checkpoint": True, "fetched_at": "2026-08-20T08:00:00Z"},
        ]
    )
    assert replay_evidence(reconstructed, cutoff) == "reconstructed-research"


def test_adaptive_collector_cadence_counts_only_declared_slots() -> None:
    slots = _declared_slots_for_day(date(2026, 8, 20))
    assert len(slots) == 40
    delayed = datetime(2026, 8, 20, 22, 57, tzinfo=timezone.utc)
    assert _expected_slot_at(delayed) == datetime(
        2026, 8, 20, 22, 7, tzinfo=timezone.utc
    )


def test_hybrid_collector_marks_only_local_fixed_slots_as_full() -> None:
    assert _collection_mode(
        "auto",
        scheduled_at=datetime(2026, 8, 20, 7, 7, tzinfo=timezone.utc),
        trigger="cloudflare",
    ) == "fixed"
    assert _collection_mode(
        "auto",
        scheduled_at=datetime(2026, 8, 20, 7, 37, tzinfo=timezone.utc),
        trigger="cloudflare",
    ) == "aviation"
    assert _collection_mode(
        "closeout",
        scheduled_at=datetime(2026, 8, 20, 19, 15, tzinfo=timezone.utc),
        trigger="cloudflare",
    ) == "closeout"
