from __future__ import annotations

from collections.abc import Mapping


CHECKPOINT_LABELS: Mapping[str, str] = {
    "d1": "D−1 Evening @20:00 LT",
    # These three keys remain stable for imported v10.7.x history. The Madrid
    # cockpit itself uses only the four fixed labels below.
    "d0_06": "D0 @06:00 LT",
    "d0_10": "D0 @10:00 LT",
    "live": "First stored live snapshot after D0@10",
    "d0_09": "D0 Morning @09:00 LT",
    "live_12": "First Live @12:00 LT",
    "live_16": "Late Live @16:00 LT",
}

FORECAST_STAGE_LABELS: Mapping[str, str] = {
    "raw": "Raw ensemble",
    "bias": "Bias-corrected",
    "metar": "Live weather-adjusted",
    "champion": "Champion",
    "taf": "TAF guidance",
}

EVIDENCE_GLOSSARY: Mapping[str, str] = {
    "scheduled": (
        "A real production snapshot stored at the intended checkpoint from information "
        "available at that time. Scheduled does not by itself guarantee fresh sources."
    ),
    "reconstructed": (
        "Rebuilt later from guidance proven available before the checkpoint. It is useful "
        "for research, but is not genuine live/OOS evidence."
    ),
    "late/post-peak": (
        "Stored only after the intended trading time or after the modelled peak. It remains "
        "diagnostic and is excluded from timing-reliability claims."
    ),
    "missing": "No defensible forecast snapshot is available for this checkpoint.",
}

FRESHNESS_GLOSSARY: Mapping[str, str] = {
    "fresh": "All Champion-relevant sources are at most 30 minutes old.",
    "aging": "The oldest Champion-relevant source is 31–90 minutes old.",
    "stale": "At least one Champion-relevant source is more than 90 minutes old.",
    "unavailable": "Source age cannot be established or no relevant source is available.",
}


def checkpoint_stage_label(prefix: str, stage: str) -> str:
    """Return one canonical timepoint + forecast-stage display label."""
    return f"{CHECKPOINT_LABELS[prefix]} · {FORECAST_STAGE_LABELS[stage]}"
