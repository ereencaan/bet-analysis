"""Pydantic schemas for the bet-analysis pipeline.

Single source of truth for every shape passed between the data layer,
aggregator, AI debate orchestrator, and MCP tool surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

Sport = Literal[
    "football",
    "basketball",
    "tennis",
    "baseball",
    "icehockey",
    "americanfootball",
]

PlayerStatusValue = Literal["available", "injured", "suspended", "doubtful"]

StakeSuggestion = Literal["no_bet", "small", "medium", "large"]

ConsensusStrength = Literal["strong", "moderate", "weak", "no_consensus"]

LineupKnown = Literal["yes", "no", "ask"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Match / team primitives
# ---------------------------------------------------------------------------


class MatchResult(_Frozen):
    date: datetime
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    competition: str | None = None
    venue: Literal["home", "away", "neutral"] | None = None

    @property
    def outcome(self) -> Literal["W", "D", "L"]:
        """Outcome from the perspective of `home_team` of this record."""
        if self.home_goals > self.away_goals:
            return "W"
        if self.home_goals < self.away_goals:
            return "L"
        return "D"


class TeamForm(_Frozen):
    team_id: str
    team_name: str
    last_5_matches: list[MatchResult] = Field(default_factory=list)
    last_5_home_or_away: list[MatchResult] = Field(default_factory=list)
    goals_scored_avg: float = 0.0
    goals_conceded_avg: float = 0.0
    clean_sheets_pct: float = 0.0
    btts_pct: float = 0.0
    over_2_5_pct: float = 0.0


class PlayerStatus(_Frozen):
    name: str
    position: str
    status: PlayerStatusValue
    impact_rating: int = Field(ge=1, le=10)
    notes: str | None = None


# ---------------------------------------------------------------------------
# Officials, weather, odds
# ---------------------------------------------------------------------------


class RefereeStats(_Frozen):
    name: str
    matches_officiated: int
    yellow_cards_avg: float
    red_cards_avg: float
    penalties_awarded_avg: float
    home_win_pct: float | None = None


class WeatherInfo(_Frozen):
    temperature_c: float
    condition: str  # "clear", "rain", "snow", "wind", ...
    wind_kph: float
    humidity_pct: float
    precipitation_mm: float | None = None


class MarketOdds(_Frozen):
    """Snapshot of pre-match market odds across the major books.

    Decimal odds. Missing markets remain None.
    """

    one_x_two_home: float | None = None
    one_x_two_draw: float | None = None
    one_x_two_away: float | None = None

    over_2_5: float | None = None
    under_2_5: float | None = None

    btts_yes: float | None = None
    btts_no: float | None = None

    asian_handicap: dict[str, float] | None = None  # e.g. {"-1": 2.10, "+1": 1.75}

    cards_over_4_5: float | None = None
    cards_under_4_5: float | None = None

    corners_over_9_5: float | None = None
    corners_under_9_5: float | None = None

    bookmaker: str | None = None


class OddsSnapshot(_Frozen):
    captured_at: datetime
    odds: MarketOdds


# ---------------------------------------------------------------------------
# The aggregated context an AI sees
# ---------------------------------------------------------------------------


class MatchContext(_Frozen):
    match_id: str
    sport: Sport
    league: str
    kickoff_utc: datetime
    home_team: TeamForm
    away_team: TeamForm
    h2h_last_10: list[MatchResult] = Field(default_factory=list)
    home_squad_status: list[PlayerStatus] = Field(default_factory=list)
    away_squad_status: list[PlayerStatus] = Field(default_factory=list)
    lineups_confirmed: bool = False
    home_lineup: list[str] | None = None
    away_lineup: list[str] | None = None
    referee: RefereeStats | None = None
    weather: WeatherInfo | None = None
    market_odds: MarketOdds = Field(default_factory=MarketOdds)
    odds_movement: list[OddsSnapshot] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# AI verdicts and final recommendation
# ---------------------------------------------------------------------------


class BetSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str  # "1X2", "Over/Under 2.5", "BTTS", "AH -1", "Cards Over 4.5", ...
    selection: str  # "Home", "Yes", "Over", ...
    odds: float
    stake_suggestion: StakeSuggestion
    expected_value: float | None = None


class AIVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_name: str
    primary_pick: BetSelection
    alternative_picks: list[BetSelection] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    key_factors: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class FinalRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: str
    consensus_pick: BetSelection | None = None
    consensus_strength: ConsensusStrength
    individual_verdicts: list[AIVerdict]
    debate_summary: str
    risk_warnings: list[str] = Field(default_factory=list)
    no_bet_recommended: bool = False
    no_bet_reason: str | None = None


# ---------------------------------------------------------------------------
# Lineup interactive flow
# ---------------------------------------------------------------------------


class LineupQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["lineups_required"] = "lineups_required"
    message: str
    predicted_home_xi: list[str] = Field(default_factory=list)
    predicted_away_xi: list[str] = Field(default_factory=list)
    options: list[Literal["use_predicted", "i_will_provide", "wait_until_confirmed"]] = Field(
        default_factory=lambda: ["use_predicted", "i_will_provide", "wait_until_confirmed"]
    )
