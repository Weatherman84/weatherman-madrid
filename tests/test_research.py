from datetime import date

import pandas as pd

from weatherman.research import filter_target_window, market_timing_metrics


def test_research_window_keeps_only_latest_target_days():
    frame = pd.DataFrame(
        [
            {"target_date": date(2026, 1, day), "value": day}
            for day in range(1, 11)
        ]
    )

    result = filter_target_window(frame, 3)

    assert result.value.tolist() == [8, 9, 10]


def test_market_timing_metrics_respect_fahrenheit_bucket_width():
    scored = pd.DataFrame(
        [
            {
                "airport": "KATL",
                "target_date": date(2026, 7, 20),
                "timing": "D-1 · 24h lead",
                "lead_bucket": "D-1 · 24h lead",
                "stage": "Raw model mean",
                "forecast_c": 30.0,
                "max_temp_c": 30.4,
                "error": -0.4,
                "abs_error": 0.4,
            }
        ]
    )
    catalog = {
        "KATL": {
            "market_unit": "F",
            "market_bucket_width": 2,
        }
    }

    result = market_timing_metrics(scored, catalog)

    assert result.iloc[0].market_exact_hit == 1.0
