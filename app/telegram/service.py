"""Thin, Telethon-isolated layer for the Telegram login flow.

Everything Telethon-specific lives in ``TelethonAdapter`` (imported lazily, so the rest of the
application and the test-suite do not need Telethon or a Telegram account). Every operation opens a
client, does its work, and ALWAYS disconnects again. Nothing in this module logs secrets: codes,
passwords, API hashes and session strings never appear in log messages or exceptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from .. import __version__
from .errors import NetworkProblem, TelegramError, map_telethon_exception

log = logging.getLogger(__name__)
OPERATION_TIMEOUT = 40.0

_PHONE = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone(raw: str) -> str:
    """'+880 1712-345678' -> '+8801712345678'. Raises ValueError if it is not international format."""
    cleaned = re.sub(r"[\s\-().]", "", raw or "")
    if not _PHONE.match(cleaned):
        raise ValueError("invalid phone number")
    return cleaned


def mask_phone(phone: str) -> str:
    """'+8801712345678' -> '+88•••••••••78' (never the full number)."""
    return phone[:3] + "•" * max(len(phone) - 5, 3) + phone[-2:]


@dataclass(frozen=True)
class TelegramProfile:
    tg_user_id: int
    username: str = ""
    first_name: str = ""


@dataclass(frozen=True)
class SentCode:
    phone_code_hash: str
    session: str  # pending (not yet authorised) session, needed to finish the login


@dataclass(frozen=True)
class SignInResult:
    password_needed: bool
    session: str
    profile: TelegramProfile | None = None


class ClientAdapter(Protocol):
    """What the service needs from a client. TelethonAdapter and the test fake both implement it."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def send_code(self, phone: str) -> str: ...
    async def sign_in_code(self, phone: str, code: str, phone_code_hash: str) -> TelegramProfile | None: ...
    async def sign_in_password(self, password: str) -> TelegramProfile: ...
    async def current_profile(self) -> TelegramProfile | None: ...
    async def log_out(self) -> None: ...
    def export_session(self) -> str: ...


ClientFactory = Callable[[int, str, str], ClientAdapter]


class TelethonAdapter:
    """The only place that imports and calls Telethon."""

    def __init__(self, api_id: int, api_hash: str, session: str) -> None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        self._client = TelegramClient(
            StringSession(session or None), api_id, api_hash,
            device_model="Telegram Auto Poster", app_version=__version__, system_version="Server",
            lang_code="en", receive_updates=False, connection_retries=2, request_retries=2, timeout=15,
            flood_sleep_threshold=0,  # never sleep silently on FloodWait: surface it instead
        )

    @staticmethod
    def _profile(me: Any) -> TelegramProfile:
        return TelegramProfile(int(me.id), getattr(me, "username", "") or "", getattr(me, "first_name", "") or "")

    async def connect(self) -> None:
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def send_code(self, phone: str) -> str:
        sent = await self._client.send_code_request(phone)
        return sent.phone_code_hash

    async def sign_in_code(self, phone: str, code: str, phone_code_hash: str) -> TelegramProfile | None:
        try:
            me = await self._client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except Exception as exc:
            mapped = map_telethon_exception(exc)
            if mapped.code == "password_required":
                return None
            raise mapped from None
        return self._profile(me)

    async def sign_in_password(self, password: str) -> TelegramProfile:
        me = await self._client.sign_in(password=password)
        return self._profile(me)

    async def current_profile(self) -> TelegramProfile | None:
        if not await self._client.is_user_authorized():
            return None
        return self._profile(await self._client.get_me())

    async def log_out(self) -> None:
        await self._client.log_out()

    def export_session(self) -> str:
        return self._client.session.save()


def _default_factory(api_id: int, api_hash: str, session: str) -> ClientAdapter:
    return TelethonAdapter(api_id, api_hash, session)


class TelegramClientService:
    def __init__(self, factory: ClientFactory = _default_factory, timeout: float = OPERATION_TIMEOUT) -> None:
        self._factory = factory
        self._timeout = timeout

    @contextlib.asynccontextmanager
    async def _open(self, api_id: int, api_hash: str, session: str):
        client = self._factory(api_id, api_hash, session)
        try:
            try:
                await asyncio.wait_for(client.connect(), self._timeout)
            except TelegramError:
                raise
            except asyncio.TimeoutError:
                raise NetworkProblem() from None
            except Exception as exc:
                raise map_telethon_exception(exc) from None
            yield client
        finally:  # cleanup even when the operation failed or timed out
            try:
                await asyncio.wait_for(client.disconnect(), 10)
            except Exception:
                log.warning("telegram client did not disconnect cleanly")

    async def _run(self, coro: Awaitable):
        try:
            return await asyncio.wait_for(coro, self._timeout)
        except TelegramError:
            raise
        except asyncio.TimeoutError:
            raise NetworkProblem() from None
        except Exception as exc:
            mapped = map_telethon_exception(exc)
            log.info("telegram operation failed (%s)", mapped.code)  # class-level reason only
            raise mapped from None

    async def send_code(self, api_id: int, api_hash: str, phone: str) -> SentCode:
        async with self._open(api_id, api_hash, "") as client:
            code_hash = await self._run(client.send_code(phone))
            return SentCode(code_hash, client.export_session())

    async def sign_in_code(self, api_id: int, api_hash: str, session: str, phone: str, code: str,
                           phone_code_hash: str) -> SignInResult:
        async with self._open(api_id, api_hash, session) as client:
            profile = await self._run(client.sign_in_code(phone, code, phone_code_hash))
            return SignInResult(profile is None, client.export_session(), profile)

    async def sign_in_password(self, api_id: int, api_hash: str, session: str, password: str) -> SignInResult:
        async with self._open(api_id, api_hash, session) as client:
            profile = await self._run(client.sign_in_password(password))
            return SignInResult(False, client.export_session(), profile)

    async def check_authorization(self, api_id: int, api_hash: str, session: str) -> TelegramProfile | None:
        """Profile if the session is still authorised, None if Telegram no longer accepts it."""
        async with self._open(api_id, api_hash, session) as client:
            try:
                return await self._run(client.current_profile())
            except TelegramError as exc:
                if exc.code == "session_revoked":
                    return None
                raise

    async def log_out(self, api_id: int, api_hash: str, session: str) -> None:
        """Ends the session on Telegram's side too. Already-dead sessions count as success."""
        async with self._open(api_id, api_hash, session) as client:
            try:
                await self._run(client.log_out())
            except TelegramError as exc:
                if exc.code != "session_revoked":
                    raise
