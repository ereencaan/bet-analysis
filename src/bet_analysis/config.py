"""Environment loading and runtime config."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name, default)
    if val == "":
        return None
    return val


def _required(name: str) -> str:
    val = _get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


@dataclass(frozen=True)
class Config:
    # Data providers
    api_football_key: str | None
    sportradar_key: str | None
    odds_api_key: str | None
    openweather_key: str | None

    # AI providers
    anthropic_api_key: str | None
    openai_api_key: str | None
    google_api_key: str | None

    # Models
    claude_model: str
    gpt_model: str
    gemini_model: str

    # Behavior
    min_confidence_for_bet: float
    cache_db_path: str
    log_level: str

    @classmethod
    def load(cls) -> Config:
        return cls(
            api_football_key=_get("API_FOOTBALL_KEY"),
            sportradar_key=_get("SPORTRADAR_KEY"),
            odds_api_key=_get("ODDS_API_KEY"),
            openweather_key=_get("OPENWEATHER_KEY"),
            anthropic_api_key=_get("ANTHROPIC_API_KEY"),
            openai_api_key=_get("OPENAI_API_KEY"),
            google_api_key=_get("GOOGLE_API_KEY"),
            claude_model=_get("CLAUDE_MODEL", "claude-sonnet-4-6") or "claude-sonnet-4-6",
            gpt_model=_get("GPT_MODEL", "gpt-4o") or "gpt-4o",
            gemini_model=_get("GEMINI_MODEL", "gemini-2.5-pro") or "gemini-2.5-pro",
            min_confidence_for_bet=float(_get("MIN_CONFIDENCE_FOR_BET", "0.55") or "0.55"),
            cache_db_path=_get("CACHE_DB_PATH", "./cache.db") or "./cache.db",
            log_level=_get("LOG_LEVEL", "INFO") or "INFO",
        )


config = Config.load()
