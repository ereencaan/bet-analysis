"""MCP server entry point.

Step 1 (skeleton): exposes `analyze_match` as a stub that echoes its arguments
so we can verify the Claude Desktop / Cursor stdio handshake before wiring up
the data layer, aggregator, and debate orchestrator.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import config
from .models import LineupKnown

logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bet_analysis")

mcp = FastMCP("bet-analysis")


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

    Args:
        sport: e.g. "football", "basketball".
        home_team: Home side name.
        away_team: Away side name.
        kickoff: ISO 8601 kickoff time with timezone.
        lineups_known: "yes" | "no" | "ask".
        home_lineup: Optional starting XI override.
        away_lineup: Optional starting XI override.
        preferred_markets: Optional filter, e.g. ["1X2", "BTTS"].

    Returns:
        FinalRecommendation as dict, or LineupQueryResponse if confirmation needed.
    """
    log.info(
        "analyze_match (stub) sport=%s match=%s vs %s kickoff=%s lineups_known=%s",
        sport,
        home_team,
        away_team,
        kickoff,
        lineups_known,
    )
    return {
        "status": "stub",
        "message": "Skeleton MCP server is alive. Pipeline not yet implemented.",
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
async def get_team_form(team: str, sport: str = "football", last_n: int = 10) -> dict[str, Any]:
    """Quick lookup of a team's recent form. Stub until data layer lands."""
    return {"status": "stub", "team": team, "sport": sport, "last_n": last_n}


@mcp.tool()
async def clear_cache(scope: str | None = None) -> dict[str, Any]:
    """Invalidate cached data. Pass a key prefix to scope (e.g. 'api_football:')."""
    return {"status": "stub", "scope": scope}


def main() -> None:
    log.info("Starting bet-analysis MCP server (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
