"""Telegram groups of a connected account: synchronise from Telegram, list, and persist the user's selection.

Isolation rules (every method):
* all SQL is scoped by BOTH ``user_id`` and ``account_id``; ids from the browser are never trusted;
* a foreign / unknown account or group looks exactly like a missing one (AccountNotFound / GroupNotFound);
* the Telegram session is decrypted through TelegramConnectionService.load_session, which refuses foreign accounts;
* nothing here sends, joins, leaves or changes anything on Telegram: the only Telegram call is a read-only listing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..db.models import new_id
from .connect import AccountNotFound, TelegramConnectionService
from .errors import FloodWait, SessionRevoked
from .groups import MAX_DIALOGS, OK, REASONS, Assessed, assess
from .service import TelegramClientService

log = logging.getLogger(__name__)

MAX_SELECTED_GROUPS = 200
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class GroupNotFound(Exception):
    """A submitted group id is not a group of this user's account (never says which)."""


class NotSelectable(Exception):
    """A submitted group exists but cannot be a posting target (no permission / not accessible)."""


class TooManySelected(Exception):
    pass


class SyncBlocked(Exception):
    """A FloodWait is still running for this account."""

    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        super().__init__(str(seconds))


@dataclass(frozen=True)
class SyncResult:
    total: int
    postable: int
    truncated: bool


def is_uuid(value: str) -> bool:
    return bool(_UUID.match(value or ""))


class GroupService:
    def __init__(self, db: Any, connect: TelegramConnectionService, telegram: TelegramClientService,
                 clock: Callable[[], float]) -> None:
        self._db, self._connect, self._tg, self._clock = db, connect, telegram, clock

    # ---- reading -------------------------------------------------------------------------------------
    async def account(self, user_id: str, account_id: str) -> dict:
        for acc in await self._connect.list_accounts(user_id):
            if acc["id"].lower() == (account_id or "").lower():  # the canonical id from the database is used from here on
                row = await self._db.fetchrow(
                    "SELECT groups_synced_at, groups_blocked_until FROM telegram_accounts WHERE id = $1 AND user_id = $2",
                    acc["id"], user_id)
                return {**acc, "groups_synced_at": row["groups_synced_at"], "groups_blocked_until": row["groups_blocked_until"]}
        raise AccountNotFound()

    async def groups(self, user_id: str, account_id: str) -> list[dict]:
        rows = await self._db.fetch(
            "SELECT id, tg_chat_id, title, username, chat_type, permission_status, permission_detail, "
            "permission_checked_at, is_enabled, is_present FROM telegram_groups "
            "WHERE user_id = $1 AND account_id = $2 ORDER BY is_present DESC, lower(title), id", user_id, account_id)
        return [dict(r) | {"id": str(r["id"]), "is_enabled": bool(r["is_enabled"]), "is_present": bool(r["is_present"])} for r in rows]

    # ---- synchronising ---------------------------------------------------------------------------------
    async def sync(self, user_id: str, account_id: str) -> SyncResult:
        acc = await self.account(user_id, account_id)  # raises AccountNotFound for foreign/unknown ids
        account_id = acc["id"]
        now = int(self._clock())
        blocked = acc["groups_blocked_until"]
        if blocked and now < blocked:
            raise SyncBlocked(blocked - now)
        usable = await self._connect.load_session(user_id, account_id)  # raises AccountNotFound if not connected
        try:
            chats, truncated = await self._tg.list_groups(usable.api_id, usable.api_hash, usable.session, MAX_DIALOGS)
        except FloodWait as exc:  # respect Telegram's wait; remember it so nobody retries early
            await self._db.execute("UPDATE telegram_accounts SET groups_blocked_until = $1 WHERE id = $2 AND user_id = $3",
                                   now + exc.seconds, account_id, user_id)
            raise
        except SessionRevoked:
            await self._connect.expire_session(user_id, account_id)
            raise
        verdicts = self._verdicts(chats)
        await self._store(user_id, account_id, verdicts)
        return SyncResult(len(verdicts), sum(1 for v in verdicts if v.status == OK), truncated)

    def _verdicts(self, chats) -> list[Assessed]:
        now = datetime.fromtimestamp(self._clock(), timezone.utc)
        unique: dict[int, Assessed] = {}
        for raw in chats:
            verdict = assess(raw, now)
            if verdict is not None:
                unique[verdict.chat_id] = verdict
        return list(unique.values())

    async def _store(self, user_id: str, account_id: str, verdicts: list[Assessed]) -> None:
        async with self._db.transaction() as tx:  # all or nothing: a failure keeps the previous state
            await tx.execute("UPDATE telegram_groups SET is_present = FALSE WHERE user_id = $1 AND account_id = $2",
                             user_id, account_id)
            for v in verdicts:
                await tx.execute(
                    "INSERT INTO telegram_groups (id, user_id, account_id, tg_chat_id, title, username, chat_type, "
                    "permission_status, permission_detail, permission_checked_at, is_enabled, is_present, last_synced_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP, FALSE, TRUE, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (account_id, tg_chat_id) DO UPDATE SET "
                    "title = EXCLUDED.title, username = EXCLUDED.username, chat_type = EXCLUDED.chat_type, "
                    "permission_status = EXCLUDED.permission_status, permission_detail = EXCLUDED.permission_detail, "
                    "permission_checked_at = CURRENT_TIMESTAMP, "
                    "is_enabled = CASE WHEN EXCLUDED.permission_status = 'ok' THEN telegram_groups.is_enabled ELSE FALSE END, "
                    "is_present = TRUE, last_synced_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP",
                    new_id(), user_id, account_id, v.chat_id, v.title, v.username or None, v.chat_type, v.status, v.detail or None)
            # groups the account no longer sees: unavailable, and never a selected target
            await tx.execute(
                "UPDATE telegram_groups SET permission_status = 'unavailable', permission_detail = $3, is_enabled = FALSE, "
                "last_synced_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE user_id = $1 AND account_id = $2 AND is_present = FALSE", user_id, account_id, REASONS["gone"])
            await tx.execute("UPDATE telegram_accounts SET groups_synced_at = CURRENT_TIMESTAMP, groups_blocked_until = NULL "
                             "WHERE id = $1 AND user_id = $2", account_id, user_id)

    # ---- selection ----------------------------------------------------------------------------------------
    async def save_selection(self, user_id: str, account_id: str, group_ids: set[str]) -> int:
        """Replace the selected groups of ONE of the user's accounts. Strict: anything suspicious rejects the request."""
        account_id = (await self.account(user_id, account_id))["id"]  # ownership of the account
        if len(group_ids) > MAX_SELECTED_GROUPS:
            raise TooManySelected()
        if not all(is_uuid(g) for g in group_ids):
            raise GroupNotFound()
        owned = {str(r["id"]).lower(): r for r in await self._db.fetch(
            "SELECT id, permission_status, is_present FROM telegram_groups WHERE user_id = $1 AND account_id = $2",
            user_id, account_id)}
        wanted = {g.lower() for g in group_ids}
        if not wanted <= set(owned):
            raise GroupNotFound()  # unknown id, or one that belongs to another account / user: same answer
        if any(owned[g]["permission_status"] != OK or not owned[g]["is_present"] for g in wanted):
            raise NotSelectable()
        async with self._db.transaction() as tx:
            await tx.execute("UPDATE telegram_groups SET is_enabled = FALSE, updated_at = CURRENT_TIMESTAMP "
                             "WHERE user_id = $1 AND account_id = $2", user_id, account_id)
            for gid in sorted(wanted):
                await tx.execute(
                    "UPDATE telegram_groups SET is_enabled = TRUE, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = $1 AND user_id = $2 AND account_id = $3 AND permission_status = 'ok' AND is_present = TRUE",
                    gid, user_id, account_id)
        return len(wanted)
