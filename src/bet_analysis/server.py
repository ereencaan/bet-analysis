"""MCP server entry point.

Exposes the bet-analysis pipeline over stdio. Step 4 wires `get_team_form`
to a real `ApiSportsProvider` call (read-through cached). `analyze_match`
remains a stub until the aggregator + debate orchestrator land.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from .cache import Cache
from .config import config
from .data.api_sports import ApiSportsProvider
from .data.base import DataProvider, ProviderError
from .data.sofascore import SofaScoreProvider
from .models import LineupKnown, Sport

# Default provider per sport. SofaScore = free, current-season, Turkish Super
# Lig + every major league. API-Sports stays available as a fallback when a
# user explicitly asks for it (provider="api-sports").
_DEFAULT_PROVIDER_NAME = "sofascore"

logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bet_analysis")

mcp = FastMCP("bet-analysis")

# Lazy-initialized singletons. Created on first tool call so import time
# stays cheap and missing API keys don't block server startup.
_cache: Cache | None = None
_providers: dict[tuple[str, Sport], DataProvider] = {}


async def _get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = await Cache.connect()
    return _cache


async def _get_provider(sport: str, provider_name: str | None = None) -> DataProvider:
    """Resolve and cache a DataProvider for (provider_name, sport).

    `provider_name` defaults to SofaScore (free, current-season). Pass
    `"api-sports"` to fall back to the paid-tier-friendly provider.
    """
    name = (provider_name or _DEFAULT_PROVIDER_NAME).lower()
    key = (name, sport)
    if key in _providers:
        return _providers[key]
    cache = await _get_cache()
    if name == "sofascore":
        provider: DataProvider = SofaScoreProvider(sport=sport, cache=cache)  # type: ignore[arg-type]
    elif name in {"api-sports", "apisports", "api_football"}:
        provider = ApiSportsProvider(sport=sport, cache=cache)  # type: ignore[arg-type]
    else:
        raise ProviderError(f"Unknown provider: {provider_name!r}")
    _providers[key] = provider
    return provider


# ----------------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------------


@mcp.tool()
async def analyze_match(
    sport: str,
    home_team: str,
    away_team: str,
    kickoff: str,
    lineups_known: LineupKnown = "ask",
    home_lineup: list[str] | None = None,
    away_lineup: list[str] | None = None,
    preferred_markets: list[str] | None = None,
) -> dict[str, Any]:
    """Run full pre-match betting analysis (multi-AI debate).

    Pipeline still stubbed; aggregator + 3-AI debate land in subsequent
    commits. Args echo back so MCP integration is verifiable.
    """
    log.info(
        "analyze_match (stub) sport=%s match=%s vs %s kickoff=%s",
        sport, home_team, away_team, kickoff,
    )
    return {
        "status": "stub",
        "message": "Pipeline not yet implemented. Use get_team_form for live data.",
        "echo": {
            "sport": sport,
            "home_team": home_team,
            "away_team": away_team,
            "kickoff": kickoff,
            "lineups_known": lineups_known,
            "home_lineup": home_lineup,
            "away_lineup": away_lineup,
            "preferred_markets": preferred_markets,
        },
    }


@mcp.tool()
async def get_team_form(
    team: str,
    sport: str = "football",
    last_n: int = 10,
    provider: str | None = None,
) -> dict[str, Any]:
    """Resolve a team name and fetch its last-N form.

    Defaults to the SofaScore provider (free, live current-season).
    Pass `provider="api-sports"` to use the API-Sports adapter.

    Returns a TeamForm dict (scoring averages + football %s when applicable),
    plus the resolved team_id so follow-up calls can skip the lookup.
    """
    try:
        prov = await _get_provider(sport, provider)
    except ProviderError as e:
        return {"error": str(e)}

    try:
        found = await prov.find_team(team)
        if not found:
            return {"error": f"No team named {team!r} found ({prov.__class__.__name__})"}
        team_id, canonical = found
        form = await prov.get_team_form(team_id=team_id, last_n=last_n)
    except ProviderError as e:
        return {
            "error": str(e),
            "status_code": e.status_code,
            "endpoint": e.endpoint,
        }

    return {
        "provider": prov.__class__.__name__,
        "resolved": {"id": team_id, "name": canonical},
        "form": form.model_dump(mode="json"),
    }


@mcp.tool()
async def clear_cache(scope: str | None = None) -> dict[str, Any]:
    """Invalidate cached data. Pass a key prefix (e.g. 'apisports:football:')."""
    cache = await _get_cache()
    deleted = await cache.clear(prefix=scope)
    return {"deleted_rows": deleted, "scope": scope or "*"}


def main() -> None:
    log.info("Starting bet-analysis MCP server (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
