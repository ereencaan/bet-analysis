"""API-Sports multi-sport provider (api-sports.io / api-football.com).

A single key unlocks 12 sports. Each sport sits at its own subdomain with
slightly different endpoint vocabulary (football=`fixtures`, basketball=
`games`, etc.) — we abstract that over a small `_SPORT_CONFIG` table.

v1 implements the full football contract; other sports get team search +
recent games + H2H. Sport-specific bits (BTTS%, O/U 2.5%, lineups, odds,
referee, injuries) are football-only for now and noted as such.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..cache import TTL, Cache, make_key
from ..config import config
from ..models import (
    MarketOdds,
    MatchResult,
    PlayerStatus,
    RefereeStats,
    Sport,
    TeamForm,
)
from .base import DataProvider, ProviderError

log = logging.getLogger("bet_analysis.api_sports")


@dataclass(frozen=True)
class _SportEndpoints:
    base_url: str
    match_endpoint: str  # "fixtures" for football, "games" for most others
    h2h_endpoint: str | None  # None = use match_endpoint with h2h param
    lineups_endpoint: str | None
    odds_endpoint: str | None
    injuries_endpoint: str | None


_SPORT_CONFIG: dict[Sport, _SportEndpoints] = {
    "football": _SportEndpoints(
        base_url="https://v3.football.api-sports.io",
        match_endpoint="fixtures",
        h2h_endpoint="fixtures/headtohead",
        lineups_endpoint="fixtures/lineups",
        odds_endpoint="odds",
        injuries_endpoint="injuries",
    ),
    "basketball": _SportEndpoints(
        base_url="https://v1.basketball.api-sports.io",
        match_endpoint="games",
        h2h_endpoint="games/h2h",
        lineups_endpoint=None,  # not provided by API-Sports basketball
        odds_endpoint="odds",
        injuries_endpoint=None,
    ),
    "baseball": _SportEndpoints(
        base_url="https://v1.baseball.api-sports.io",
        match_endpoint="games",
        h2h_endpoint="games/h2h",
        lineups_endpoint=None,
        odds_endpoint="odds",
        injuries_endpoint=None,
    ),
    "icehockey": _SportEndpoints(
        base_url="https://v1.hockey.api-sports.io",
        match_endpoint="games",
        h2h_endpoint="games/h2h",
        lineups_endpoint=None,
        odds_endpoint="odds",
        injuries_endpoint=None,
    ),
    "americanfootball": _SportEndpoints(
        base_url="https://v1.american-football.api-sports.io",
        match_endpoint="games",
        h2h_endpoint="games/h2h",
        lineups_endpoint=None,
        odds_endpoint="odds",
        injuries_endpoint=None,
    ),
    "tennis": _SportEndpoints(
        # Tennis isn't yet supported on api-sports — placeholder.
        base_url="https://v1.tennis.api-sports.io",
        match_endpoint="games",
        h2h_endpoint=None,
        lineups_endpoint=None,
        odds_endpoint=None,
        injuries_endpoint=None,
    ),
}


class ApiSportsProvider(DataProvider):
    """Multi-sport API-Sports adapter.

    Construct one instance per sport — the sport selects subdomain and
    endpoint vocabulary.
    """

    def __init__(
        self,
        sport: Sport,
        api_key: str | None = None,
        cache: Cache | None = None,
    ) -> None:
        if sport not in _SPORT_CONFIG:
            raise ValueError(f"Unsupported sport: {sport}")
        self.sport = sport
        self._cfg = _SPORT_CONFIG[sport]
        self._api_key = api_key or config.api_football_key
        if not self._api_key:
            raise ProviderError("API_FOOTBALL_KEY not configured")
        self._cache = cache
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ HTTP

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._cfg.base_url,
                headers={"x-apisports-key": self._api_key},
                timeout=httpx.Timeout(20.0, connect=5.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        ttl_seconds: int = TTL["team_form"],
    ) -> dict[str, Any]:
        """Read-through cached GET. Always returns the raw JSON body."""
        cache_key = make_key(f"apisports:{self.sport}", endpoint, params)

        async def _fetch() -> dict[str, Any]:
            client = await self._ensure_client()
            log.debug("GET %s%s params=%s", self._cfg.base_url, endpoint, params)
            r = await client.get(f"/{endpoint.lstrip('/')}", params=params or {})
            if r.status_code != 200:
                raise ProviderError(
                    f"HTTP {r.status_code} on /{endpoint}: {r.text[:200]}",
                    status_code=r.status_code,
                    endpoint=endpoint,
                )
            data = r.json()
            errors = data.get("errors")
            # API-Sports returns errors as either a list (empty when ok) or a
            # dict (key→message when something is wrong). Treat non-empty as
            # a hard failure.
            if isinstance(errors, dict) and errors:
                raise ProviderError(f"API errors on /{endpoint}: {errors}", endpoint=endpoint)
            if isinstance(errors, list) and errors:
                raise ProviderError(f"API errors on /{endpoint}: {errors}", endpoint=endpoint)
            return data

        if self._cache is None:
            return await _fetch()
        return await self._cache.get_or_fetch(cache_key, ttl_seconds, _fetch)

    # ------------------------------------------------------------ Find / IDs

    async def find_team(self, name: str) -> tuple[str, str] | None:
        data = await self._get(
            "teams",
            params={"search": name},
            ttl_seconds=TTL["h2h"],  # team IDs are stable, can cache long
        )
        items = data.get("response") or []
        if not items:
            return None
        # Prefer the best match by exact-name then lowest id (senior squad
        # usually has the lowest id in a club's family of teams).
        def _score(item: dict[str, Any]) -> tuple[int, int]:
            t = item.get("team", {}) if self.sport == "football" else item
            tname = (t.get("name") or "").lower()
            exact = 0 if tname == name.lower() else 1
            return (exact, int(t.get("id") or 1_000_000))

        best = min(items, key=_score)
        team = best.get("team", {}) if self.sport == "football" else best
        team_id = team.get("id")
        team_name = team.get("name")
        if team_id is None or team_name is None:
            return None
        return str(team_id), str(team_name)

    async def find_match(
        self,
        home_team_id: str,
        away_team_id: str,
        kickoff_utc: datetime,
    ) -> str | None:
        if self.sport != "football":
            # Other sports use /games?team=... — implement when needed.
            return None
        date_str = kickoff_utc.date().isoformat()
        data = await self._get(
            self._cfg.match_endpoint,
            params={"h2h": f"{home_team_id}-{away_team_id}", "date": date_str},
            ttl_seconds=TTL["h2h"],
        )
        items = data.get("response") or []
        for it in items:
            fx = it.get("fixture", {})
            teams = it.get("teams", {})
            if (
                str(teams.get("home", {}).get("id")) == str(home_team_id)
                and str(teams.get("away", {}).get("id")) == str(away_team_id)
            ):
                return str(fx.get("id"))
        return None

    # ----------------------------------------------------------------- Form

    # Latest season the API-Sports free tier reliably exposes for football.
    # Pro plans can override via `season=` keyword.
    _FREE_TIER_FALLBACK_SEASON = 2023

    async def _fetch_team_fixtures(
        self,
        team_id: str,
        last_n: int,
        season: int | None = None,
    ) -> dict[str, Any]:
        """Fetch a team's fixtures with automatic free-tier fallback.

        Tries `?last=N` first (works on paid plans). On the specific free-tier
        rejection (`Free plans do not have access to the Last parameter.`),
        retries with `?season=<fallback>` and lets the caller take the most
        recent N from the result.
        """
        params_last: dict[str, Any] = {"team": team_id, "last": last_n}
        if season is not None:
            params_last["season"] = season
        try:
            return await self._get(
                self._cfg.match_endpoint,
                params=params_last,
                ttl_seconds=TTL["team_form"],
            )
        except ProviderError as e:
            if "Last parameter" not in str(e):
                raise
            log.info("Free-tier fallback: retrying without 'last', season=%s",
                     season or self._FREE_TIER_FALLBACK_SEASON)
            return await self._get(
                self._cfg.match_endpoint,
                params={
                    "team": team_id,
                    "season": season or self._FREE_TIER_FALLBACK_SEASON,
                },
                ttl_seconds=TTL["team_form"],
            )

    async def get_team_form(
        self,
        team_id: str,
        last_n: int = 10,
        season: int | None = None,
    ) -> TeamForm:
        data = await self._fetch_team_fixtures(team_id, last_n, season=season)
        if self.sport == "football":
            return _parse_form_football(team_id, last_n, data)
        return _parse_form_generic(team_id, last_n, data)

    # ----------------------------------------------------------------- H2H

    async def get_h2h(
        self,
        team_a_id: str,
        team_b_id: str,
        last_n: int = 10,
    ) -> list[MatchResult]:
        if not self._cfg.h2h_endpoint:
            return []
        params: dict[str, Any] = {"last": last_n}
        if self.sport == "football":
            params["h2h"] = f"{team_a_id}-{team_b_id}"
        else:
            params["h2h"] = f"{team_a_id}-{team_b_id}"
        data = await self._get(
            self._cfg.h2h_endpoint,
            params=params,
            ttl_seconds=TTL["h2h"],
        )
        items = data.get("response") or []
        return [m for m in (_parse_match_result(it, self.sport) for it in items) if m]

    # ----------------------------------------------------------- Squad / lineups

    async def get_squad_status(
        self,
        team_id: str,
        match_id: str | None = None,
    ) -> list[PlayerStatus]:
        if self.sport != "football" or not self._cfg.injuries_endpoint:
            return []
        params: dict[str, Any] = {"team": team_id}
        if match_id:
            params["fixture"] = match_id
        else:
            params["season"] = datetime.now(tz=timezone.utc).year
        data = await self._get(
            self._cfg.injuries_endpoint,
            params=params,
            ttl_seconds=TTL["squad"],
        )
        out: list[PlayerStatus] = []
        for it in data.get("response") or []:
            player = it.get("player", {})
            reason = (it.get("player", {}).get("reason") or "").lower()
            status = "injured" if "injur" in reason else (
                "suspended" if "suspen" in reason or "card" in reason else "doubtful"
            )
            out.append(
                PlayerStatus(
                    name=player.get("name") or "Unknown",
                    position=player.get("position") or "?",
                    status=status,  # type: ignore[arg-type]
                    impact_rating=5,  # API doesn't expose impact; default mid
                    notes=it.get("player", {}).get("reason"),
                )
            )
        return out

    async def get_lineups(
        self,
        match_id: str,
    ) -> tuple[list[str], list[str]] | None:
        if self.sport != "football" or not self._cfg.lineups_endpoint:
            return None
        data = await self._get(
            self._cfg.lineups_endpoint,
            params={"fixture": match_id},
            ttl_seconds=TTL["lineups"],
        )
        items = data.get("response") or []
        if len(items) < 2:
            return None
        home_xi = [p.get("player", {}).get("name") for p in items[0].get("startXI", []) if p]
        away_xi = [p.get("player", {}).get("name") for p in items[1].get("startXI", []) if p]
        home_xi = [n for n in home_xi if n]
        away_xi = [n for n in away_xi if n]
        if not home_xi or not away_xi:
            return None
        return home_xi, away_xi

    # ------------------------------------------------------------ Referee

    async def get_referee_stats(self, match_id: str) -> RefereeStats | None:
        if self.sport != "football":
            return None
        data = await self._get(
            self._cfg.match_endpoint,
            params={"id": match_id},
            ttl_seconds=TTL["referee"],
        )
        items = data.get("response") or []
        if not items:
            return None
        ref_name = items[0].get("fixture", {}).get("referee")
        if not ref_name:
            return None
        # API-Sports doesn't expose per-referee stats on free tier —
        # return a name-only record so prompts can still mention them.
        return RefereeStats(
            name=ref_name,
            matches_officiated=0,
            yellow_cards_avg=0.0,
            red_cards_avg=0.0,
            penalties_awarded_avg=0.0,
        )

    # -------------------------------------------------------------- Odds

    async def get_market_odds(self, match_id: str) -> MarketOdds:
        if not self._cfg.odds_endpoint:
            return MarketOdds()
        data = await self._get(
            self._cfg.odds_endpoint,
            params={"fixture" if self.sport == "football" else "game": match_id},
            ttl_seconds=TTL["odds"],
        )
        items = data.get("response") or []
        if not items:
            return MarketOdds()
        return _parse_odds_football(items[0]) if self.sport == "football" else MarketOdds()


# ----------------------------------------------------------------------------
# Parsing helpers (module-level so they're easy to unit-test)
# ----------------------------------------------------------------------------


def _parse_match_result(item: dict[str, Any], sport: Sport) -> MatchResult | None:
    if sport == "football":
        fx = item.get("fixture", {})
        teams = item.get("teams", {})
        goals = item.get("goals", {})
        date = fx.get("date")
        if not date:
            return None
        try:
            dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
        except ValueError:
            return None
        return MatchResult(
            date=dt,
            home_team=(teams.get("home", {}) or {}).get("name") or "",
            away_team=(teams.get("away", {}) or {}).get("name") or "",
            home_goals=int(goals.get("home") or 0),
            away_goals=int(goals.get("away") or 0),
            competition=(item.get("league", {}) or {}).get("name"),
        )
    # Basketball / hockey / others — adapt when implementing.
    return None


def _parse_form_football(team_id: str, last_n: int, data: dict[str, Any]) -> TeamForm:
    """Compute form over a team's most recent `last_n` *finished* matches.

    The API may return either a curated `?last=N` slice or a whole season
    (free-tier fallback). We always sort by kickoff date desc and trim
    to `last_n`, ignoring anything that isn't full-time / completed.
    """
    items = data.get("response") or []
    name = ""

    # Filter to this team's finished matches and pre-extract sortable date.
    rows: list[tuple[datetime, dict[str, Any], bool]] = []
    for it in items:
        teams = it.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        home_id = str(home.get("id"))
        away_id = str(away.get("id"))
        if str(team_id) not in (home_id, away_id):
            continue
        status_short = (it.get("fixture") or {}).get("status", {}).get("short")
        if status_short not in {"FT", "AET", "PEN"}:
            continue
        date_str = (it.get("fixture") or {}).get("date")
        if not date_str:
            continue
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        is_home = home_id == str(team_id)
        if not name:
            name = (home if is_home else away).get("name") or ""
        rows.append((dt, it, is_home))

    # Newest first, take last_n.
    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[:last_n]

    matches: list[MatchResult] = []
    scored = conceded = clean_sheets = btts = over_25 = 0
    for _, it, is_home in rows:
        goals = it.get("goals") or {}
        gh = int(goals.get("home") or 0)
        ga = int(goals.get("away") or 0)
        my, opp = (gh, ga) if is_home else (ga, gh)
        m = _parse_match_result(it, "football")
        if m:
            matches.append(m)
        scored += my
        conceded += opp
        if opp == 0:
            clean_sheets += 1
        if gh > 0 and ga > 0:
            btts += 1
        if gh + ga > 2:
            over_25 += 1

    n = max(len(rows), 1)
    return TeamForm(
        team_id=str(team_id),
        team_name=name or f"team:{team_id}",
        last_5_matches=matches[:5],
        last_5_home_or_away=matches[:5],
        goals_scored_avg=round(scored / n, 2),
        goals_conceded_avg=round(conceded / n, 2),
        clean_sheets_pct=round(100 * clean_sheets / n, 1),
        btts_pct=round(100 * btts / n, 1),
        over_2_5_pct=round(100 * over_25 / n, 1),
    )


def _parse_form_generic(team_id: str, last_n: int, data: dict[str, Any]) -> TeamForm:
    """Sport-agnostic form: scoring averages only, no football-specific %s."""
    items = data.get("response") or []
    name = ""
    scored = 0
    conceded = 0
    counted = 0

    for it in items:
        teams = it.get("teams") or {}
        scores = it.get("scores") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        home_id = str(home.get("id"))
        away_id = str(away.get("id"))
        if str(team_id) not in (home_id, away_id):
            continue
        is_home = home_id == str(team_id)
        if not name:
            name = (home if is_home else away).get("name") or ""
        # API-Sports scores: {home: {total: N}, away: {total: N}}
        h_total = (scores.get("home") or {}).get("total") if isinstance(scores, dict) else None
        a_total = (scores.get("away") or {}).get("total") if isinstance(scores, dict) else None
        if h_total is None or a_total is None:
            continue
        my, opp = (int(h_total), int(a_total)) if is_home else (int(a_total), int(h_total))
        scored += my
        conceded += opp
        counted += 1

    n = max(counted, 1)
    return TeamForm(
        team_id=str(team_id),
        team_name=name or f"team:{team_id}",
        goals_scored_avg=round(scored / n, 2),
        goals_conceded_avg=round(conceded / n, 2),
    )


def _parse_odds_football(item: dict[str, Any]) -> MarketOdds:
    """Pluck the most useful pre-match markets from the first bookmaker."""
    bookmakers = item.get("bookmakers") or []
    if not bookmakers:
        return MarketOdds()
    bm = bookmakers[0]
    bets = {b.get("name"): b.get("values") or [] for b in bm.get("bets") or []}

    def _odd(market: str, label: str) -> float | None:
        for v in bets.get(market) or []:
            if str(v.get("value")).lower() == label.lower():
                try:
                    return float(v.get("odd"))
                except (TypeError, ValueError):
                    return None
        return None

    return MarketOdds(
        one_x_two_home=_odd("Match Winner", "Home"),
        one_x_two_draw=_odd("Match Winner", "Draw"),
        one_x_two_away=_odd("Match Winner", "Away"),
        over_2_5=_odd("Goals Over/Under", "Over 2.5"),
        under_2_5=_odd("Goals Over/Under", "Under 2.5"),
        btts_yes=_odd("Both Teams Score", "Yes"),
        btts_no=_odd("Both Teams Score", "No"),
        bookmaker=bm.get("name"),
    )
