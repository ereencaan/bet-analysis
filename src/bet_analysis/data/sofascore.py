"""SofaScore unofficial-API provider.

SofaScore exposes a rich JSON API at api.sofascore.com that powers their
own web/mobile apps. It is not officially documented or supported, but
endpoints have been stable for years and cover every major sport with
live current-season data — exactly what API-Sports' free tier lacks.

Cloudflare gates the API based on TLS fingerprint + headers, so we use
`curl_cffi` to impersonate a real Chrome handshake. Plain `httpx`/`requests`
get a 403.

Sport coverage (slug):
    football, basketball (incl. NBA), tennis, ice-hockey, american-football,
    handball, baseball, rugby, mma, motorsport, esports, ...

Most endpoints are sport-agnostic at the URL level — the sport is implicit
in the team/event id. We accept a `sport` argument for the `find_team`
filter only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from curl_cffi.requests import AsyncSession

from ..cache import TTL, Cache, make_key
from ..models import (
    MarketOdds,
    MatchResult,
    PlayerStatus,
    RefereeStats,
    Sport,
    TeamForm,
)
from .base import DataProvider, ProviderError

log = logging.getLogger("bet_analysis.sofascore")

_BASE_URL = "https://api.sofascore.com/api/v1"

# Map our internal Sport literal to SofaScore's slug system.
_SPORT_SLUGS: dict[Sport, str] = {
    "football": "football",
    "basketball": "basketball",
    "tennis": "tennis",
    "baseball": "baseball",
    "icehockey": "ice-hockey",
    "americanfootball": "american-football",
}

# Status codes that mark a finished match across SofaScore sports.
_FINISHED_STATUS_TYPES = {"finished"}


class SofaScoreProvider(DataProvider):
    """Read-only SofaScore adapter with TLS-impersonating transport."""

    def __init__(
        self,
        sport: Sport,
        cache: Cache | None = None,
        impersonate: str = "chrome",
    ) -> None:
        if sport not in _SPORT_SLUGS:
            raise ValueError(f"Unsupported sport for SofaScore: {sport}")
        self.sport = sport
        self._sport_slug = _SPORT_SLUGS[sport]
        self._cache = cache
        self._impersonate = impersonate
        self._session: AsyncSession | None = None

    # ------------------------------------------------------------------ HTTP

    async def _ensure_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(
                impersonate=self._impersonate,
                timeout=20,
                headers={
                    "Accept": "application/json",
                    "Referer": "https://www.sofascore.com/",
                },
            )
        return self._session

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        ttl_seconds: int = TTL["team_form"],
    ) -> dict[str, Any]:
        cache_key = make_key(f"sofascore:{self.sport}", path, params)

        async def _fetch() -> dict[str, Any]:
            session = await self._ensure_session()
            url = f"{_BASE_URL}{path}"
            log.debug("GET %s params=%s", url, params)
            r = await session.get(url, params=params or {})
            if r.status_code == 404:
                # 404 is a "no data" signal in SofaScore (no events yet,
                # no lineups published, etc.) — treat as empty response.
                return {}
            if r.status_code != 200:
                raise ProviderError(
                    f"HTTP {r.status_code} on {path}: {r.text[:200]}",
                    status_code=r.status_code,
                    endpoint=path,
                )
            try:
                return r.json()
            except Exception as e:  # pragma: no cover — malformed response
                raise ProviderError(
                    f"Bad JSON on {path}: {e}", endpoint=path
                ) from e

        if self._cache is None:
            return await _fetch()
        return await self._cache.get_or_fetch(cache_key, ttl_seconds, _fetch)

    # ------------------------------------------------------------ Team search

    async def find_team(self, name: str) -> tuple[str, str] | None:
        data = await self._get(
            "/search/all",
            params={"q": name},
            ttl_seconds=TTL["h2h"],
        )
        results = data.get("results") or []
        candidates: list[dict[str, Any]] = []
        for r in results:
            if r.get("type") != "team":
                continue
            entity = r.get("entity") or {}
            slug = (entity.get("sport") or {}).get("slug")
            if slug != self._sport_slug:
                continue
            candidates.append(entity)
        if not candidates:
            return None

        # Prefer national/senior teams: lowest id usually = senior squad,
        # exact-name match wins, else first match.
        def _score(e: dict[str, Any]) -> tuple[int, int, int]:
            n = (e.get("name") or "").lower()
            sn = (e.get("shortName") or "").lower()
            target = name.lower()
            exact = 0 if target in {n, sn} else 1
            is_national = 0 if e.get("national") else 1  # club > national usually
            return (exact, is_national, int(e.get("id") or 1_000_000))

        best = min(candidates, key=_score)
        return str(best.get("id")), str(best.get("name"))

    # ------------------------------------------------------------ Match search

    async def find_match(
        self,
        home_team_id: str,
        away_team_id: str,
        kickoff_utc: datetime,
    ) -> str | None:
        # Look in upcoming and recent; SofaScore returns ~30 each.
        for slot in ("next", "last"):
            data = await self._get(
                f"/team/{home_team_id}/events/{slot}/0",
                ttl_seconds=TTL["h2h"],
            )
            for ev in data.get("events") or []:
                home = (ev.get("homeTeam") or {}).get("id")
                away = (ev.get("awayTeam") or {}).get("id")
                if str(home) == str(home_team_id) and str(away) == str(away_team_id):
                    # Optional: tighten by kickoff date when given.
                    if kickoff_utc:
                        ts = ev.get("startTimestamp")
                        if ts:
                            ev_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                            if abs((ev_dt - kickoff_utc).days) > 2:
                                continue
                    return str(ev.get("id"))
        return None

    # ----------------------------------------------------------------- Form

    async def get_team_form(self, team_id: str, last_n: int = 10) -> TeamForm:
        data = await self._get(
            f"/team/{team_id}/events/last/0",
            ttl_seconds=TTL["team_form"],
        )
        return _parse_form(team_id, last_n, data, self.sport)

    # ------------------------------------------------------------------ H2H

    async def get_h2h(
        self,
        team_a_id: str,
        team_b_id: str,
        last_n: int = 10,
    ) -> list[MatchResult]:
        """Past meetings between two teams, newest first.

        SofaScore exposes only an aggregate `/event/{id}/h2h` summary —
        no list of past meetings. We reconstruct the list by walking
        team_a's recent-events pages (3 × 30 = ~90 matches deep) and
        keeping fixtures where the opponent is team_b.
        """
        seen_ids: set[int] = set()
        results: list[tuple[int, MatchResult]] = []
        for page in range(3):
            data = await self._get(
                f"/team/{team_a_id}/events/last/{page}",
                ttl_seconds=TTL["h2h"],
            )
            events = data.get("events") or []
            if not events:
                break
            for ev in events:
                home_id = (ev.get("homeTeam") or {}).get("id")
                away_id = (ev.get("awayTeam") or {}).get("id")
                if str(team_b_id) not in {str(home_id), str(away_id)}:
                    continue
                eid = ev.get("id")
                if eid in seen_ids:
                    continue
                m = _parse_event_to_match_result(ev)
                if m:
                    seen_ids.add(eid)
                    results.append((int(ev.get("startTimestamp") or 0), m))
            if len(results) >= last_n:
                break
        results.sort(key=lambda r: r[0], reverse=True)
        return [m for _, m in results[:last_n]]

    # ------------------------------------------------------------ Squad / injuries

    async def get_squad_status(
        self,
        team_id: str,
        match_id: str | None = None,
    ) -> list[PlayerStatus]:
        data = await self._get(
            f"/team/{team_id}/missing-players",
            ttl_seconds=TTL["squad"],
        )
        out: list[PlayerStatus] = []
        for it in data.get("players") or []:
            player = it.get("player") or {}
            reason = (it.get("reason") or "").lower()
            type_ = (it.get("type") or "").lower()
            if "suspen" in reason or "card" in reason or type_ == "suspension":
                status = "suspended"
            elif "injur" in reason or type_ == "missing":
                status = "injured"
            else:
                status = "doubtful"
            out.append(
                PlayerStatus(
                    name=player.get("name") or "Unknown",
                    position=player.get("position") or "?",
                    status=status,  # type: ignore[arg-type]
                    impact_rating=_player_impact(player),
                    notes=it.get("reason"),
                )
            )
        return out

    # ----------------------------------------------------------------- Lineups

    async def get_lineups(
        self,
        match_id: str,
    ) -> tuple[list[str], list[str]] | None:
        data = await self._get(
            f"/event/{match_id}/lineups",
            ttl_seconds=TTL["lineups"],
        )
        confirmed = data.get("confirmed", False)
        home = data.get("home") or {}
        away = data.get("away") or {}
        home_xi = _starting_xi(home)
        away_xi = _starting_xi(away)
        if not (confirmed and home_xi and away_xi):
            return None
        return home_xi, away_xi

    # ----------------------------------------------------------------- Referee

    async def get_referee_stats(self, match_id: str) -> RefereeStats | None:
        data = await self._get(
            f"/event/{match_id}",
            ttl_seconds=TTL["referee"],
        )
        ev = data.get("event") or {}
        ref = ev.get("referee") or {}
        if not ref.get("name"):
            return None
        return RefereeStats(
            name=ref["name"],
            matches_officiated=int(ref.get("games") or 0),
            yellow_cards_avg=float(ref.get("yellowCards") or 0)
            / max(int(ref.get("games") or 1), 1),
            red_cards_avg=float(ref.get("redCards") or 0)
            / max(int(ref.get("games") or 1), 1),
            penalties_awarded_avg=0.0,
        )

    # -------------------------------------------------------------- Odds

    async def get_market_odds(self, match_id: str) -> MarketOdds:
        data = await self._get(
            f"/event/{match_id}/odds/1/all",
            ttl_seconds=TTL["odds"],
        )
        markets = data.get("markets") or []
        if not markets:
            return MarketOdds()
        return _parse_odds(markets, self.sport)


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------


def _player_impact(player: dict[str, Any]) -> int:
    """Heuristic 1-10 rating for how critical a missing player is.

    SofaScore doesn't expose a direct rating. Use position as a proxy:
    GK and central forwards tend to be hardest to replace 1-for-1.
    """
    pos = (player.get("position") or "").upper()
    if pos in {"G", "GK"}:
        return 8
    if pos in {"F", "ST", "CF"}:
        return 7
    if pos in {"M", "AM", "CM"}:
        return 6
    if pos in {"D", "CB"}:
        return 5
    return 5


def _starting_xi(side: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for p in side.get("players") or []:
        if p.get("substitute"):
            continue
        name = (p.get("player") or {}).get("name")
        if name:
            out.append(name)
    return out


def _parse_event_to_match_result(ev: dict[str, Any]) -> MatchResult | None:
    ts = ev.get("startTimestamp")
    if not ts:
        return None
    home = (ev.get("homeTeam") or {}).get("name")
    away = (ev.get("awayTeam") or {}).get("name")
    if not home or not away:
        return None
    hs = (ev.get("homeScore") or {}).get("current")
    as_ = (ev.get("awayScore") or {}).get("current")
    if hs is None or as_ is None:
        return None
    return MatchResult(
        date=datetime.fromtimestamp(int(ts), tz=timezone.utc),
        home_team=home,
        away_team=away,
        home_goals=int(hs),
        away_goals=int(as_),
        competition=(ev.get("tournament") or {}).get("name"),
    )


def _parse_form(
    team_id: str,
    last_n: int,
    data: dict[str, Any],
    sport: Sport,
) -> TeamForm:
    """Compute scoring averages + (football-only) BTTS / O2.5 / clean sheets."""
    items = data.get("events") or []
    # Sort newest first, keep finished only, take last_n.
    items = [
        ev
        for ev in items
        if (ev.get("status") or {}).get("type") in _FINISHED_STATUS_TYPES
    ]
    items.sort(key=lambda ev: int(ev.get("startTimestamp") or 0), reverse=True)
    items = items[:last_n]

    name = ""
    matches: list[MatchResult] = []
    scored = conceded = clean_sheets = btts = over_25 = 0
    counted = 0

    for ev in items:
        home = ev.get("homeTeam") or {}
        away = ev.get("awayTeam") or {}
        hs = (ev.get("homeScore") or {}).get("current")
        as_ = (ev.get("awayScore") or {}).get("current")
        if hs is None or as_ is None:
            continue
        is_home = str(home.get("id")) == str(team_id)
        if not name:
            name = (home if is_home else away).get("name") or ""
        my, opp = (int(hs), int(as_)) if is_home else (int(as_), int(hs))
        m = _parse_event_to_match_result(ev)
        if m:
            matches.append(m)
        scored += my
        conceded += opp
        if opp == 0:
            clean_sheets += 1
        if hs > 0 and as_ > 0:
            btts += 1
        if hs + as_ > 2:
            over_25 += 1
        counted += 1

    n = max(counted, 1)
    form_kwargs: dict[str, Any] = {
        "team_id": str(team_id),
        "team_name": name or f"team:{team_id}",
        "last_5_matches": matches[:5],
        "last_5_home_or_away": matches[:5],
        "goals_scored_avg": round(scored / n, 2),
        "goals_conceded_avg": round(conceded / n, 2),
    }
    if sport == "football":
        form_kwargs.update(
            clean_sheets_pct=round(100 * clean_sheets / n, 1),
            btts_pct=round(100 * btts / n, 1),
            over_2_5_pct=round(100 * over_25 / n, 1),
        )
    return TeamForm(**form_kwargs)


def _parse_odds(markets: list[dict[str, Any]], sport: Sport) -> MarketOdds:
    """Pluck major pre-match markets from SofaScore's /odds/1/all payload."""

    def _market(name_substr: str) -> dict[str, Any] | None:
        target = name_substr.lower()
        for m in markets:
            if target in (m.get("marketName") or "").lower():
                return m
        return None

    def _choice_odd(market: dict[str, Any] | None, label: str) -> float | None:
        if not market:
            return None
        for c in market.get("choices") or []:
            if str(c.get("name", "")).lower() == label.lower():
                frac = c.get("fractionalValue")  # e.g. "5/4"
                dec = _frac_to_decimal(frac) if frac else None
                if dec is None:
                    try:
                        dec = float(c.get("initialFractionalValue") or 0) or None
                    except (TypeError, ValueError):
                        dec = None
                return dec
        return None

    odds = MarketOdds(bookmaker="SofaScore")

    if sport == "football":
        winner = _market("full time")
        if winner:
            odds = odds.model_copy(update={
                "one_x_two_home": _choice_odd(winner, "1"),
                "one_x_two_draw": _choice_odd(winner, "X"),
                "one_x_two_away": _choice_odd(winner, "2"),
            })
        ou = _market("total goals (over/under)") or _market("over/under")
        if ou:
            odds = odds.model_copy(update={
                "over_2_5": _choice_odd(ou, "over 2.5"),
                "under_2_5": _choice_odd(ou, "under 2.5"),
            })
        btts = _market("both teams to score")
        if btts:
            odds = odds.model_copy(update={
                "btts_yes": _choice_odd(btts, "yes"),
                "btts_no": _choice_odd(btts, "no"),
            })
    elif sport in {"basketball", "americanfootball", "icehockey"}:
        ml = _market("home/away") or _market("moneyline") or _market("winner")
        if ml:
            odds = odds.model_copy(update={
                "moneyline_home": _choice_odd(ml, "1") or _choice_odd(ml, "home"),
                "moneyline_away": _choice_odd(ml, "2") or _choice_odd(ml, "away"),
            })

    return odds


def _frac_to_decimal(frac: str) -> float | None:
    """Convert SofaScore fractional odds 'a/b' to decimal (1 + a/b)."""
    try:
        a, b = frac.split("/")
        return round(1.0 + float(a) / float(b), 3)
    except (ValueError, ZeroDivisionError):
        return None
