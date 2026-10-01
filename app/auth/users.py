"""User accounts and the first-run admin claim."""

from __future__ import annotations

import re
from typing import Any

from ..db.models import new_id

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s.]{2,}$")


class SetupAlreadyDone(Exception):
    pass


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(_EMAIL.match(email))


class UserService:
    def __init__(self, db: Any) -> None:
        self._db = db
        self._setup_done = False  # only ever cached as True (a DB reset needs a restart anyway)

    async def is_setup_complete(self) -> bool:
        if self._setup_done:
            return True
        row = await self._db.fetchrow(
            "SELECT 1 AS x FROM app_state WHERE key = 'setup_completed' "
            "UNION ALL SELECT 1 FROM users LIMIT 1"
        )
        self._setup_done = row is not None
        return self._setup_done

    async def create_initial_admin(self, email: str, password_hash: str, display_name: str) -> str:
        """Atomically claim the first-run slot and create the admin.

        The claim is an INSERT on a primary key inside the same transaction as the user
        insert, so of any number of simultaneous requests exactly one wins; the others get
        SetupAlreadyDone and nothing is written.
        """
        user_id = new_id()
        async with self._db.transaction() as tx:
            claimed = await tx.fetchrow(
                "INSERT INTO app_state (key, value) VALUES ('setup_completed', '1') "
                "ON CONFLICT (key) DO NOTHING RETURNING key"
            )
            if claimed is None or await tx.fetchrow("SELECT 1 AS x FROM users LIMIT 1") is not None:
                raise SetupAlreadyDone()
            await tx.execute(
                "INSERT INTO users (id, email, password_hash, display_name, is_admin) "
                "VALUES ($1, $2, $3, $4, TRUE)",
                user_id, email, password_hash, display_name or None,
            )
        self._setup_done = True
        return user_id

    async def find_by_email(self, email: str):
        return await self._db.fetchrow(
            "SELECT id, email, password_hash, display_name, is_admin, is_active FROM users "
            "WHERE lower(email) = $1", email,
        )

    async def touch_login(self, user_id: str) -> None:
        await self._db.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = $1", user_id)
