"""Abstract data provider interface.

Every provider (API-Sports, Sportradar, The Odds API, ...) implements this
contract. The aggregator only talks to `DataProvider`, never to a concrete
provider, so swapping providers is a one-line change.

All methods are async. Concrete providers are expected to use the shared
SQLite cache to avoid hammering external APIs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import (
    MarketOdds,
    MatchResult,
    PlayerStatus,
    RefereeStats,
    Sport,
    TeamForm,
)


class DataProvider(ABC):
    """Read-only interface for fetching pre-match data.

    Implementations must:
      * Use cache.get_or_fetch with the appropriate TTL for each call.
      * Return strict pydantic objects (no dicts).
      * Raise `ProviderError` on transport / quota / auth failures.
      * Return `None` when a piece of data is genuinely unavailable
        (e.g. lineups not yet published) rather than raising.
    """

    sport: Sport

    @abstractmethod
    async def find_team(self, name: str) -> tuple[str, str] | None:
        """Resolve a free-text team name to (team_id, canonical_name).

        Returns None if no team found. If multiple matches, picks the best.
        """

    @abstractmethod
    async def find_match(
        self,
        home_team_id: str,
        away_team_id: str,
        kickoff_utc: datetime,
    ) -> str | None:
        """Resolve a match (home, away, kickoff) to a fixture/game id."""

    @abstractmethod
    async def get_team_form(self, team_id: str, last_n: int = 10) -> TeamForm: ...

    @abstractmethod
    async def get_h2h(
        self,
        team_a_id: str,
        team_b_id: str,
        last_n: int = 10,
    ) -> list[MatchResult]: ...

    @abstractmethod
    async def get_squad_status(
        self,
        team_id: str,
        match_id: str | None = None,
    ) -> list[PlayerStatus]: ...

    @abstractmethod
    async def get_lineups(
        self,
        match_id: str,
    ) -> tuple[list[str], list[str]] | None: ...

    @abstractmethod
    async def get_referee_stats(self, match_id: str) -> RefereeStats | None: ...

    @abstractmethod
    async def get_market_odds(self, match_id: str) -> MarketOdds: ...


class ProviderError(RuntimeError):
    """Raised on transport, auth, quota, or schema-shape failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        endpoint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint
