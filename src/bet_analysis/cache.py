"""SQLite-backed read-through cache with per-key TTL.

Used by every DataProvider so we hit external APIs as little as possible.
Cache keys follow `{provider}:{endpoint}:{params_hash}`.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite

from .config import config

# Default TTLs in seconds. Overridable per-call via `set(... ttl_seconds=...)`.
TTL = {
    "team_form": 6 * 3600,
    "h2h": 24 * 3600,
    "squad": 30 * 60,
    "lineups": 5 * 60,
    "odds": 2 * 60,
    "weather": 60 * 60,
    "referee": 24 * 3600,
}


def _hash_params(params: dict[str, Any] | None) -> str:
    if not params:
        return "_"
    payload = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def make_key(provider: str, endpoint: str, params: dict[str, Any] | None = None) -> str:
    return f"{provider}:{endpoint}:{_hash_params(params)}"


class Cache:
    """Async SQLite cache. Initialize once with `await Cache.connect(path)`."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    @classmethod
    async def connect(cls, path: str | None = None) -> Cache:
        db_path = path or config.cache_db_path
        db = await aiosqlite.connect(db_path)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)"
        )
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    async def get(self, key: str) -> Any | None:
        cur = await self._db.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return None
        value, expires_at = row
        if expires_at < int(time.time()):
            await self.delete(key)
            return None
        return json.loads(value)

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        expires_at = int(time.time()) + ttl_seconds
        payload = json.dumps(value, default=str, separators=(",", ":"))
        await self._db.execute(
            "INSERT OR REPLACE INTO cache(key, value, expires_at) VALUES (?, ?, ?)",
            (key, payload, expires_at),
        )
        await self._db.commit()

    async def delete(self, key: str) -> None:
        await self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
        await self._db.commit()

    async def clear(self, prefix: str | None = None) -> int:
        if prefix:
            cur = await self._db.execute(
                "DELETE FROM cache WHERE key LIKE ?", (f"{prefix}%",)
            )
        else:
            cur = await self._db.execute("DELETE FROM cache")
        await self._db.commit()
        return cur.rowcount or 0

    async def get_or_fetch(
        self,
        key: str,
        ttl_seconds: int,
        fetcher: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Read-through: return cached value if fresh, else call `fetcher` and store."""
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await fetcher()
        if value is not None:
            await self.set(key, value, ttl_seconds)
        return value
