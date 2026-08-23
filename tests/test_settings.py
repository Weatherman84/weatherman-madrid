from weatherman.catalog import market_city_index, research_airports, trading_airports
from weatherman.settings import airports, settings


def test_packaged_airports_are_available():
    catalog = airports()
    assert set(catalog) == {"LEMD"}
    assert set(trading_airports()) == {"LEMD"}
    assert set(research_airports()) == {"LEMD"}
    assert "ukmo_global_deterministic_10km" in catalog["LEMD"]["models"]
    assert catalog["LEMD"]["heat_regime"]["positive_bias_multiplier"] == 0.4
    assert "meteofrance_arome_france_hd" in catalog["LEMD"]["heat_regime"][
        "regional_models"
    ]
    assert catalog["LEMD"]["heat_regime"]["persistent_hot"][
        "maximum_actual_age_days"
    ] == 1
    checkpoints = catalog["LEMD"]["decision_checkpoints_local"]
    assert [item["time"] for item in checkpoints] == ["20:00", "09:00", "12:00", "16:00"]
    assert market_city_index()["madrid"][0] == "LEMD"
    assert settings.regime_memory_auto_promotion_enabled is True
