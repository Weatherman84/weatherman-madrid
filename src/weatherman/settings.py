from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(os.getenv("WEATHERMAN_HOME", Path.cwd())).resolve()
load_dotenv(ROOT / ".env")

DEFAULT_METEOBLUE_URL = (
    "https://my.meteoblue.com/packages/basic-1h_basic-day?lat={lat}&lon={lon}"
    "&apikey={apikey}&asl={elevation}&format=json"
)


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///data/weatherman.db")
    meteoblue_api_key: str = os.getenv("METEOBLUE_API_KEY", "")
    meteoblue_url_template: str = os.getenv("METEOBLUE_URL_TEMPLATE", DEFAULT_METEOBLUE_URL)
    aemet_public_base_url: str = os.getenv("AEMET_PUBLIC_BASE_URL", "")
    timeout: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    live_open_meteo_refresh_minutes: int = int(
        os.getenv("LIVE_OPEN_METEO_REFRESH_MINUTES", "30")
    )
    live_meteoblue_refresh_minutes: int = int(
        os.getenv("LIVE_METEOBLUE_REFRESH_MINUTES", "120")
    )
    meteoblue_daily_call_limit: int = int(
        os.getenv("METEOBLUE_DAILY_CALL_LIMIT", "4")
    )
    meteoblue_checkpoint_lead_minutes: int = int(
        os.getenv("METEOBLUE_CHECKPOINT_LEAD_MINUTES", "15")
    )
    meteoblue_checkpoint_grace_minutes: int = int(
        os.getenv("METEOBLUE_CHECKPOINT_GRACE_MINUTES", "30")
    )
    checkpoint_capture_grace_minutes: int = int(
        os.getenv("CHECKPOINT_CAPTURE_GRACE_MINUTES", "35")
    )
    meteoblue_rate_limit_cooldown_hours: int = int(
        os.getenv("METEOBLUE_RATE_LIMIT_COOLDOWN_HOURS", "24")
    )
    maximum_live_model_age_minutes: int = int(
        os.getenv("MAXIMUM_LIVE_MODEL_AGE_MINUTES", "90")
    )
    collector_provider_workers: int = int(
        os.getenv("COLLECTOR_PROVIDER_WORKERS", "12")
    )
    collector_provider_timeout_seconds: float = float(
        os.getenv("COLLECTOR_PROVIDER_TIMEOUT_SECONDS", "8")
    )
    edge_recommendations_enabled: bool = os.getenv(
        "EDGE_RECOMMENDATIONS_ENABLED",
        "false",
    ).strip().casefold() in {"1", "true", "yes", "on"}
    regime_memory_allow_promoted: bool = os.getenv(
        "REGIME_MEMORY_ALLOW_PROMOTED",
        "false",
    ).strip().casefold() in {"1", "true", "yes", "on"}
    regime_memory_auto_promotion_enabled: bool = os.getenv(
        "REGIME_MEMORY_AUTO_PROMOTION",
        "true",
    ).strip().casefold() in {"1", "true", "yes", "on"}
    regime_memory_minimum_oos_days: int = int(
        os.getenv("REGIME_MEMORY_MINIMUM_OOS_DAYS", "30")
    )
    regime_memory_oos_start_date: str = os.getenv(
        "REGIME_MEMORY_OOS_START_DATE", "2026-08-31"
    )
    replay_database_url: str = os.getenv("REPLAY_DATABASE_URL", "")


def airports() -> dict[str, dict]:
    # Allow repository users to edit config/airports.json. The packaged copy is
    # the reliable fallback when Weatherman is installed by GitHub Actions.
    local_config = ROOT / "config" / "airports.json"
    resource = (
        local_config
        if local_config.exists()
        else files("weatherman").joinpath("data/airports.json")
    )
    with resource.open(encoding="utf-8") as handle:
        return json.load(handle)


def trading_airports() -> dict[str, dict]:
    """Airports that receive the full live trading collection."""
    return {
        code: details
        for code, details in airports().items()
        if details.get("tier", "trading") == "trading"
    }


def research_airports() -> dict[str, dict]:
    """Mapped Polymarket stations eligible for lightweight research collection."""
    return {
        code: details
        for code, details in airports().items()
        if details.get("research_enabled", True)
    }


def market_city_index() -> dict[str, tuple[str, dict]]:
    """Resolve Polymarket city slugs and aliases to configured stations."""
    index: dict[str, tuple[str, dict]] = {}
    for code, details in airports().items():
        aliases = [details.get("market_city"), *details.get("market_aliases", [])]
        for alias in aliases:
            if alias:
                index[str(alias).strip().casefold().replace(" ", "-")] = (code, details)
    return index


settings = Settings()
