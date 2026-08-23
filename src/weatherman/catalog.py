from __future__ import annotations

from .settings import airports


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
