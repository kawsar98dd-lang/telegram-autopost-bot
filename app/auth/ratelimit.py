"""Database-backed fixed-window rate limiter (shared by all web processes)."""

from __future__ import annotations

import hashlib
from typing import Any


def key_part(value: str) -> str:
    """Identifiers (IPs, emails) are hashed so no personal data sits in the table."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


class RateLimiter:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def retry_after(self, key: str, limit: int, window: int, now: int) -> int:
        """Seconds until allowed again, or 0 if the key is not currently blocked."""
        row = await self._db.fetchrow("SELECT window_start, count FROM rate_limits WHERE key = $1", key)
        if row is None:
            return 0
        end = row["window_start"] + window
        if now < end and row["count"] >= limit:
            return end - now
        return 0

    async def record(self, key: str, window: int, now: int) -> None:
        await self._db.fetchrow(
            """
            INSERT INTO rate_limits (key, window_start, count)
            VALUES ($1, CAST($2 AS BIGINT), 1)
            ON CONFLICT (key) DO UPDATE SET
              count = CASE WHEN rate_limits.window_start + CAST($3 AS BIGINT) <= CAST($2 AS BIGINT)
                           THEN 1 ELSE rate_limits.count + 1 END,
              window_start = CASE WHEN rate_limits.window_start + CAST($3 AS BIGINT) <= CAST($2 AS BIGINT)
                                  THEN CAST($2 AS BIGINT) ELSE rate_limits.window_start END
            RETURNING count
            """,
            key, now, window,
        )

    async def reset(self, key: str) -> None:
        await self._db.execute("DELETE FROM rate_limits WHERE key = $1", key)

    async def purge(self, now: int, older_than: int = 86400) -> None:
        await self._db.execute("DELETE FROM rate_limits WHERE window_start < $1", now - older_than)
