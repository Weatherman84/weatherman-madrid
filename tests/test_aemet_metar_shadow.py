from weatherman.aemet_metar_shadow import (
    build_shadow_diagnostics,
    ground_truth_comparison,
    metar_bucket_persistence_shadow,
    physical_stall_shadow,
    time_aligned_series_comparisons,
)


def rising_payload() -> dict:
    return {
        "observations": [
            {"observed_at": "2026-09-04T14:00:00Z", "temperature_c": 38.1},
            {"observed_at": "2026-09-04T14:20:00Z", "temperature_c": 38.4},
            {"observed_at": "2026-09-04T14:50:00Z", "temperature_c": 38.8},
        ],
        "physical_tmax": {
            "observed_at": "2026-09-04T14:50:00Z",
            "value_c": 38.8,
        },
    }


def metars() -> list[dict]:
    return [
        {"observed_at": "2026-09-04T14:00:00Z", "temp_c": 38.0},
        {"observed_at": "2026-09-04T14:30:00Z", "temp_c": 38.0},
        {"observed_at": "2026-09-04T15:00:00Z", "temp_c": 38.0},
    ]


def test_daily_max_gap_is_not_labeled_as_sensor_bias() -> None:
    result = ground_truth_comparison(rising_payload(), metars())

    assert result["stored_metar_max"]["value_c"] == 38.0
    assert result["aemet_physical_tmax"]["value_c"] == 38.8
    assert result["market_resolution_actual"] is None
    assert result["daily_max_series_gap_c"] == 0.8
    assert result["daily_max_series_gap_role"] == "series_difference_not_sensor_bias"


def test_only_nearby_timestamps_are_compared() -> None:
    comparisons = time_aligned_series_comparisons(
        rising_payload(),
        [
            {"observed_at": "2026-09-04T14:00:00Z", "temp_c": 38.0},
            {"observed_at": "2026-09-04T16:00:00Z", "temp_c": 38.0},
        ],
    )

    assert len(comparisons) == 1
    assert comparisons[0]["aemet_minus_metar_c"] == 0.1
    assert comparisons[0]["difference_role"] == "series_difference_not_sensor_bias"


def test_rising_aemet_series_prevents_false_physical_stall() -> None:
    result = physical_stall_shadow(rising_payload())

    assert result["stall_level"] == "low"
    assert result["probability"] is None
    assert result["calibration_status"] == "insufficient_oos_data"
    assert result["champion_impact_c"] == 0.0


def test_repeated_metar_does_not_prove_stall_while_physical_series_rises() -> None:
    result = metar_bucket_persistence_shadow(rising_payload(), metars())

    assert result["persistence_state"] == "next_metar_bucket_still_physically_possible"
    assert result["probability"] is None
    assert result["market_resolution_impact"] is None
    assert result["next_nominal_metar_at"] == "2026-09-04T15:30:00+00:00"


def test_combined_diagnostics_are_research_only() -> None:
    result = build_shadow_diagnostics(rising_payload(), metars())

    assert result["classification"] == "RESEARCH-ONLY UNCALIBRATED SHADOW DIAGNOSTICS"
    assert result["physical_stall"]["bucket_probability_impact"] is None
    assert result["metar_bucket_persistence"]["champion_impact_c"] == 0.0
