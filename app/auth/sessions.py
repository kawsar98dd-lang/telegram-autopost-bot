"""Server-side login sessions.

* The cookie carries a random 256-bit token; only its SHA-256 hash is stored.
* Idle timeout and absolute lifetime are both enforced.
* A new token is issued on every login (session fixation defence); callers destroy
  any session presented with the login request.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Callable

from ..db.models import new_id
from .permissions import Principal

TOUCH_INTERVAL = 60  # write last_seen at most once a minute


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionInfo:
    id: str
    token: str
    created_at: int
    last_seen_at: int
    principal: Principal


class SessionService:
    def __init__(self, db: Any, clock: Callable[[], float], idle_seconds: int, absolute_seconds: int) -> None:
        self._db = db
        self._clock = clock
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds

    async def create(self, user_id: str, user_agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = int(self._clock())
        await self._db.execute(
            "INSERT INTO auth_sessions (id, token_hash, user_id, created_at, last_seen_at, user_agent) "
            "VALUES ($1, $2, $3, $4, $4, $5)",
            new_id(), hash_token(token), user_id, now, user_agent[:200],
        )
        return token

    async def lookup(self, token: str) -> SessionInfo | None:
        if not token or len(token) > 200:
            return None
        row = await self._db.fetchrow(
            "SELECT s.id, s.created_at, s.last_seen_at, u.id AS user_id, u.email, u.display_name, "
            "u.is_admin, u.is_active FROM auth_sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = $1",
            hash_token(token),
        )
        if row is None:
            return None
        now = int(self._clock())
        expired = (
            now - row["last_seen_at"] > self.idle_seconds
            or now - row["created_at"] > self.absolute_seconds
            or not row["is_active"]
        )
        if expired:
            await self._db.execute("DELETE FROM auth_sessions WHERE id = $1", row["id"])
            return None
        last_seen = row["last_seen_at"]
        if now - last_seen >= TOUCH_INTERVAL:
            await self._db.execute("UPDATE auth_sessions SET last_seen_at = $1 WHERE id = $2", now, row["id"])
            last_seen = now
        principal = Principal(
            id=str(row["user_id"]), email=row["email"], display_name=row["display_name"] or "",
            is_admin=bool(row["is_admin"]),
        )
        return SessionInfo(str(row["id"]), token, row["created_at"], last_seen, principal)

    async def destroy(self, token: str) -> None:
        await self._db.execute("DELETE FROM auth_sessions WHERE token_hash = $1", hash_token(token))

    async def destroy_others(self, user_id: str, keep_token: str) -> None:
        await self._db.execute(
            "DELETE FROM auth_sessions WHERE user_id = $1 AND token_hash <> $2", user_id, hash_token(keep_token)
        )

    async def list_for_user(self, user_id: str) -> list[dict]:
        rows = await self._db.fetch(
            "SELECT id, token_hash, created_at, last_seen_at, user_agent FROM auth_sessions "
            "WHERE user_id = $1 ORDER BY last_seen_at DESC", user_id,
        )
        return [dict(r) for r in rows]

    async def purge_expired(self) -> None:
        now = int(self._clock())
        await self._db.execute(
            "DELETE FROM auth_sessions WHERE last_seen_at < $1 OR created_at < $2",
            now - self.idle_seconds, now - self.absolute_seconds,
        )
