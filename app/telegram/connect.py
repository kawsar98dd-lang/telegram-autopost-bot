"""Telegram account connection: the login flow, encrypted storage, disconnect and checks.

Security design
---------------
* The one-time code and the 2FA password are used once and are NEVER stored or logged.
* Everything secret in the database is Fernet-encrypted and *bound to its owner* through the
  encryption ``context``:
      telegram_session:<user_id>:<account_id>              the Telethon session
      telegram_credentials:<user_id>:<account_id>:<field>   API ID / API hash of the account
      telegram_login:<user_id>:<attempt_id>:<field>         data of a login in progress
  A ciphertext copied to another user's row (or another account) cannot be decrypted there.
* Every query filters by the authenticated user's id; a foreign id looks exactly like a missing one.
* Encrypted values are never returned to the web layer; only safe metadata is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..config import Settings
from ..db.models import new_id
from ..security.crypto import Cipher, CryptoError
from .errors import (
    CodeExpired,
    InvalidApiCredentials,
    InvalidCode,
    InvalidPassword,
    InvalidPhone,
    TelegramError,
)
from .service import TelegramClientService, TelegramProfile, mask_phone, normalize_phone

log = logging.getLogger(__name__)

ATTEMPT_TTL_SECONDS = 10 * 60
MAX_FAILED_ATTEMPTS = 5


class LoginAttemptGone(Exception):
    """The login in progress does not exist (for this user), expired, or failed too often."""

    def __init__(self, reason: str) -> None:
        self.reason = reason  # not_found | expired | too_many_failures
        super().__init__(reason)


class WrongStep(Exception):
    pass


class AccountNotFound(Exception):
    pass


@dataclass(frozen=True)
class PendingLogin:
    id: str
    state: str
    phone_masked: str
    failed_attempts: int
    remaining_attempts: int
    expires_at: int


@dataclass(frozen=True)
class ConnectOutcome:
    password_needed: bool
    account_id: str = ""


@dataclass(frozen=True)
class UsableSession:
    """Decrypted material, for in-process use only (the future posting worker). Never rendered."""

    account_id: str
    api_id: int
    api_hash: str
    session: str

    def __repr__(self) -> str:  # keep secrets out of logs / tracebacks
        return f"UsableSession(account_id={self.account_id!r}, <secrets hidden>)"


def session_context(user_id: str, account_id: str) -> str:
    return f"telegram_session:{user_id}:{account_id}"


def credential_context(user_id: str, account_id: str, field: str) -> str:
    return f"telegram_credentials:{user_id}:{account_id}:{field}"


def login_context(user_id: str, attempt_id: str, field: str) -> str:
    return f"telegram_login:{user_id}:{attempt_id}:{field}"


class TelegramConnectionService:
    def __init__(self, db: Any, cipher: Cipher, telegram: TelegramClientService, settings: Settings,
                 clock: Callable[[], float]) -> None:
        self._db, self._cipher, self._tg, self._settings, self._clock = db, cipher, telegram, settings, clock

    # ---- start ----------------------------------------------------------------------------------
    def _resolve_credentials(self, api_id_raw: str, api_hash_raw: str) -> tuple[int, str]:
        api_id_raw, api_hash_raw = (api_id_raw or "").strip(), (api_hash_raw or "").strip()
        if not api_id_raw and not api_hash_raw and self._settings.telegram_configured:
            return int(self._settings.telegram_api_id), self._settings.telegram_api_hash  # server defaults
        if not api_id_raw.isdigit() or not (1 <= len(api_id_raw) <= 12) or not (16 <= len(api_hash_raw) <= 64) \
                or not api_hash_raw.isalnum():
            raise InvalidApiCredentials()
        return int(api_id_raw), api_hash_raw

    async def start(self, user_id: str, phone_raw: str, api_id_raw: str, api_hash_raw: str) -> str:
        try:
            phone = normalize_phone(phone_raw)
        except ValueError:
            raise InvalidPhone() from None
        api_id, api_hash = self._resolve_credentials(api_id_raw, api_hash_raw)
        sent = await self._tg.send_code(api_id, api_hash, phone)  # raises TelegramError subclasses

        attempt_id, now = new_id(), int(self._clock())
        enc = lambda field, value: self._cipher.encrypt(value, login_context(user_id, attempt_id, field))  # noqa: E731
        async with self._db.transaction() as tx:
            await tx.execute("DELETE FROM telegram_login_attempts WHERE user_id = $1", user_id)  # one at a time
            await tx.execute(
                "INSERT INTO telegram_login_attempts (id, user_id, state, api_id_enc, api_hash_enc, phone_enc, "
                "phone_masked, phone_code_hash_enc, pending_session_enc, failed_attempts, created_at, expires_at) "
                "VALUES ($1, $2, 'code_sent', $3, $4, $5, $6, $7, $8, 0, $9, $10)",
                attempt_id, user_id, enc("api_id", str(api_id)), enc("api_hash", api_hash), enc("phone", phone),
                mask_phone(phone), enc("code_hash", sent.phone_code_hash), enc("session", sent.session),
                now, now + ATTEMPT_TTL_SECONDS,
            )
        return attempt_id

    # ---- login in progress ----------------------------------------------------------------------
    async def _load_attempt(self, user_id: str, attempt_id: str):
        row = await self._db.fetchrow(
            "SELECT * FROM telegram_login_attempts WHERE id = $1 AND user_id = $2", attempt_id, user_id)
        if row is None:
            raise LoginAttemptGone("not_found")
        if int(self._clock()) >= row["expires_at"]:
            await self._delete_attempt(user_id, attempt_id)
            raise LoginAttemptGone("expired")
        return row

    async def _delete_attempt(self, user_id: str, attempt_id: str) -> None:
        await self._db.execute("DELETE FROM telegram_login_attempts WHERE id = $1 AND user_id = $2", attempt_id, user_id)

    async def pending(self, user_id: str, attempt_id: str) -> PendingLogin:
        row = await self._load_attempt(user_id, attempt_id)
        return PendingLogin(str(row["id"]), row["state"], row["phone_masked"], row["failed_attempts"],
                            MAX_FAILED_ATTEMPTS - row["failed_attempts"], row["expires_at"])

    def _dec(self, user_id: str, attempt_id: str, row, column: str, field: str) -> str:
        return self._cipher.decrypt(row[column], login_context(user_id, attempt_id, field))

    async def _register_failure(self, user_id: str, attempt_id: str) -> None:
        row = await self._db.fetchrow(
            "UPDATE telegram_login_attempts SET failed_attempts = failed_attempts + 1 "
            "WHERE id = $1 AND user_id = $2 RETURNING failed_attempts", attempt_id, user_id)
        if row is not None and row["failed_attempts"] >= MAX_FAILED_ATTEMPTS:
            await self._delete_attempt(user_id, attempt_id)
            raise LoginAttemptGone("too_many_failures")

    async def submit_code(self, user_id: str, attempt_id: str, code_raw: str) -> ConnectOutcome:
        row = await self._load_attempt(user_id, attempt_id)
        if row["state"] != "code_sent":
            raise WrongStep()
        code = "".join(ch for ch in (code_raw or "") if ch.isdigit())
        if not 4 <= len(code) <= 8:
            await self._register_failure(user_id, attempt_id)
            raise InvalidCode()
        api_id = int(self._dec(user_id, attempt_id, row, "api_id_enc", "api_id"))
        api_hash = self._dec(user_id, attempt_id, row, "api_hash_enc", "api_hash")
        try:
            result = await self._tg.sign_in_code(
                api_id, api_hash, self._dec(user_id, attempt_id, row, "pending_session_enc", "session"),
                self._dec(user_id, attempt_id, row, "phone_enc", "phone"), code,
                self._dec(user_id, attempt_id, row, "phone_code_hash_enc", "code_hash"))
        except InvalidCode:
            await self._register_failure(user_id, attempt_id)
            raise
        except CodeExpired:
            await self._delete_attempt(user_id, attempt_id)
            raise
        if result.password_needed:
            await self._db.execute(
                "UPDATE telegram_login_attempts SET state = 'password_needed', pending_session_enc = $1 "
                "WHERE id = $2 AND user_id = $3",
                self._cipher.encrypt(result.session, login_context(user_id, attempt_id, "session")), attempt_id, user_id)
            return ConnectOutcome(password_needed=True)
        return ConnectOutcome(False, await self._finalize(user_id, attempt_id, api_id, api_hash, result.session, result.profile))

    async def submit_password(self, user_id: str, attempt_id: str, password: str) -> ConnectOutcome:
        row = await self._load_attempt(user_id, attempt_id)
        if row["state"] != "password_needed":
            raise WrongStep()
        if not password or len(password) > 256:
            await self._register_failure(user_id, attempt_id)
            raise InvalidPassword()
        api_id = int(self._dec(user_id, attempt_id, row, "api_id_enc", "api_id"))
        api_hash = self._dec(user_id, attempt_id, row, "api_hash_enc", "api_hash")
        try:
            result = await self._tg.sign_in_password(
                api_id, api_hash, self._dec(user_id, attempt_id, row, "pending_session_enc", "session"), password)
        except InvalidPassword:
            await self._register_failure(user_id, attempt_id)
            raise
        return ConnectOutcome(False, await self._finalize(user_id, attempt_id, api_id, api_hash, result.session, result.profile))

    async def cancel(self, user_id: str, attempt_id: str) -> None:
        await self._delete_attempt(user_id, attempt_id)

    async def _finalize(self, user_id: str, attempt_id: str, api_id: int, api_hash: str, session: str,
                        profile: TelegramProfile) -> str:
        row = await self._db.fetchrow("SELECT phone_masked FROM telegram_login_attempts WHERE id = $1 AND user_id = $2",
                                      attempt_id, user_id)
        masked = row["phone_masked"] if row else ""
        async with self._db.transaction() as tx:
            existing = await tx.fetchrow(
                "SELECT id FROM telegram_accounts WHERE user_id = $1 AND tg_user_id = $2", user_id, profile.tg_user_id)
            account_id = str(existing["id"]) if existing else new_id()
            enc_id = self._cipher.encrypt(str(api_id), credential_context(user_id, account_id, "api_id"))
            enc_hash = self._cipher.encrypt(api_hash, credential_context(user_id, account_id, "api_hash"))
            enc_session = self._cipher.encrypt(session, session_context(user_id, account_id))
            if existing:  # reconnecting a known account
                await tx.execute(
                    "UPDATE telegram_accounts SET username = $1, first_name = $2, phone_masked = $3, status = 'connected', "
                    "api_id_enc = $4, api_hash_enc = $5, connected_at = CURRENT_TIMESTAMP, last_seen_at = CURRENT_TIMESTAMP, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = $6 AND user_id = $7",
                    profile.username or None, profile.first_name or None, masked, enc_id, enc_hash, account_id, user_id)
                await tx.execute("DELETE FROM telegram_sessions WHERE account_id = $1", account_id)
            else:
                await tx.execute(
                    "INSERT INTO telegram_accounts (id, user_id, tg_user_id, username, first_name, phone_masked, status, "
                    "api_id_enc, api_hash_enc, last_seen_at) VALUES ($1, $2, $3, $4, $5, $6, 'connected', $7, $8, CURRENT_TIMESTAMP)",
                    account_id, user_id, profile.tg_user_id, profile.username or None, profile.first_name or None,
                    masked, enc_id, enc_hash)
            await tx.execute("INSERT INTO telegram_sessions (id, account_id, session_enc, status) VALUES ($1, $2, $3, 'active')",
                             new_id(), account_id, enc_session)
            await tx.execute("DELETE FROM telegram_login_attempts WHERE id = $1 AND user_id = $2", attempt_id, user_id)
        log.info("telegram account connected (account_id=%s)", account_id)
        return account_id

    # ---- accounts ---------------------------------------------------------------------------------
    async def list_accounts(self, user_id: str) -> list[dict]:
        """Safe metadata only: no credential or session column is ever selected here."""
        rows = await self._db.fetch(
            "SELECT id, tg_user_id, username, first_name, phone_masked, status, connected_at, last_seen_at "
            "FROM telegram_accounts WHERE user_id = $1 ORDER BY created_at, id", user_id)
        return [dict(r) | {"id": str(r["id"])} for r in rows]

    async def load_session(self, user_id: str, account_id: str) -> UsableSession:
        """Decrypt an account's session for in-process use. Raises AccountNotFound for foreign/unknown ids."""
        row = await self._db.fetchrow(
            "SELECT a.api_id_enc, a.api_hash_enc, s.session_enc FROM telegram_accounts a "
            "JOIN telegram_sessions s ON s.account_id = a.id "
            "WHERE a.id = $1 AND a.user_id = $2 AND s.status = 'active'", account_id, user_id)
        if row is None or not row["api_id_enc"] or not row["api_hash_enc"]:
            raise AccountNotFound()
        try:
            return UsableSession(
                account_id, int(self._cipher.decrypt(row["api_id_enc"], credential_context(user_id, account_id, "api_id"))),
                self._cipher.decrypt(row["api_hash_enc"], credential_context(user_id, account_id, "api_hash")),
                self._cipher.decrypt(row["session_enc"], session_context(user_id, account_id)))
        except CryptoError:
            log.warning("stored telegram session could not be decrypted (account_id=%s)", account_id)
            raise AccountNotFound() from None

    async def _wipe(self, user_id: str, account_id: str, status: str, wipe_credentials: bool) -> None:
        async with self._db.transaction() as tx:
            await tx.execute("DELETE FROM telegram_sessions WHERE account_id = $1 AND EXISTS "
                             "(SELECT 1 FROM telegram_accounts WHERE id = $1 AND user_id = $2)", account_id, user_id)
            extra = ", api_id_enc = NULL, api_hash_enc = NULL" if wipe_credentials else ""
            await tx.execute(f"UPDATE telegram_accounts SET status = $1{extra}, updated_at = CURRENT_TIMESTAMP "  # noqa: S608
                             "WHERE id = $2 AND user_id = $3", status, account_id, user_id)

    async def disconnect(self, user_id: str, account_id: str) -> bool:
        """Log the session out on Telegram (best effort), then delete it and the API credentials locally."""
        if await self._db.fetchrow("SELECT 1 AS x FROM telegram_accounts WHERE id = $1 AND user_id = $2",
                                   account_id, user_id) is None:
            raise AccountNotFound()
        revoked = False
        try:
            usable = await self.load_session(user_id, account_id)
        except AccountNotFound:
            usable = None
        if usable is not None:
            try:
                await self._tg.log_out(usable.api_id, usable.api_hash, usable.session)
                revoked = True
            except TelegramError as exc:  # we still remove our copy even if Telegram was unreachable
                log.warning("telegram log-out did not complete (%s)", exc.code)
        await self._wipe(user_id, account_id, "disconnected", wipe_credentials=True)
        return revoked

    async def remove(self, user_id: str, account_id: str) -> None:
        await self.disconnect(user_id, account_id)
        await self._db.execute("DELETE FROM telegram_accounts WHERE id = $1 AND user_id = $2", account_id, user_id)

    async def check(self, user_id: str, account_id: str) -> bool:
        """True if Telegram still accepts the session; if not, the session is removed and flagged."""
        usable = await self.load_session(user_id, account_id)
        profile = await self._tg.check_authorization(usable.api_id, usable.api_hash, usable.session)
        if profile is None:
            await self._wipe(user_id, account_id, "session_expired", wipe_credentials=False)
            return False
        await self._db.execute(
            "UPDATE telegram_accounts SET status = 'connected', username = $1, first_name = $2, "
            "last_seen_at = CURRENT_TIMESTAMP WHERE id = $3 AND user_id = $4",
            profile.username or None, profile.first_name or None, account_id, user_id)
        return True

    async def purge_expired(self) -> None:
        await self._db.execute("DELETE FROM telegram_login_attempts WHERE expires_at <= $1", int(self._clock()))
