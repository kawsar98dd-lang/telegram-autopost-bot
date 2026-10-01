"""A fake Telegram network for tests: no Telethon, no real account, no internet."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field

from app.telegram.errors import (CodeExpired, FloodWait, InvalidApiCredentials, InvalidCode, InvalidPassword,
                                 InvalidPhone, PhoneBanned, SessionRevoked)
from app.telegram.service import TelegramClientService, TelegramProfile

_counter = itertools.count(1)
GOOD_API_ID, GOOD_API_HASH = 1234567, "0123456789abcdef0123456789abcdef"


@dataclass
class FakeAccount:
    tg_user_id: int
    code: str = "12345"
    password: str | None = None
    username: str = "alice"
    first_name: str = "Alice"
    banned: bool = False


class FakeClient:
    def __init__(self, world: "FakeTelegram", api_id: int, api_hash: str, session: str) -> None:
        self.world, self.api_id, self.api_hash, self.session = world, api_id, api_hash, session
        self.connected = self.disconnected = False
        self._phone = ""
        world.clients.append(self)

    # ---- lifecycle
    async def connect(self) -> None:
        if self.world.connect_delay:
            await asyncio.sleep(self.world.connect_delay)
        if self.world.down:
            raise ConnectionError("network unreachable")
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    def export_session(self) -> str:
        return self.session

    # ---- login
    def _check_api(self) -> None:
        if (self.api_id, self.api_hash) != (GOOD_API_ID, GOOD_API_HASH):
            raise InvalidApiCredentials()

    async def send_code(self, phone: str) -> str:
        self.world.calls.append(("send_code", phone))
        self._check_api()
        if self.world.flood:
            raise FloodWait(self.world.flood)
        account = self.world.accounts.get(phone)
        if account is None:
            raise InvalidPhone()
        if account.banned:
            raise PhoneBanned()
        code_hash = f"hash{next(_counter)}"
        self.world.code_hashes[code_hash] = phone
        self.session = f"pending|{phone}|{next(_counter)}"
        return code_hash

    async def sign_in_code(self, phone: str, code: str, phone_code_hash: str) -> TelegramProfile | None:
        self.world.calls.append(("sign_in_code", phone))
        self._check_api()
        if self.world.flood:
            raise FloodWait(self.world.flood)
        if phone_code_hash in self.world.expired_hashes:
            raise CodeExpired()
        account = self.world.accounts[self.world.code_hashes[phone_code_hash]]
        if code != account.code:
            raise InvalidCode()
        if account.password is not None:
            self.session = f"password|{phone}|{next(_counter)}"
            return None
        return self._authorise(account)

    async def sign_in_password(self, password: str) -> TelegramProfile:
        self.world.calls.append(("sign_in_password", ""))
        phone = self.session.split("|")[1]
        account = self.world.accounts[phone]
        if password != account.password:
            raise InvalidPassword()
        return self._authorise(account)

    def _authorise(self, account: FakeAccount) -> TelegramProfile:
        self.session = f"live|{account.tg_user_id}|{next(_counter)}"
        self.world.live.add(self.session)
        return TelegramProfile(account.tg_user_id, account.username, account.first_name)

    # ---- authorised operations
    def _profile_for(self, session: str) -> TelegramProfile:
        tg_id = int(session.split("|")[1])
        account = next(a for a in self.world.accounts.values() if a.tg_user_id == tg_id)
        return TelegramProfile(tg_id, account.username, account.first_name)

    async def current_profile(self) -> TelegramProfile | None:
        self.world.calls.append(("current_profile", ""))
        if self.session not in self.world.live:
            return None
        return self._profile_for(self.session)

    async def log_out(self) -> None:
        self.world.calls.append(("log_out", ""))
        if self.session not in self.world.live:
            raise SessionRevoked()
        self.world.live.discard(self.session)


@dataclass
class FakeTelegram:
    accounts: dict[str, FakeAccount] = field(default_factory=dict)
    live: set[str] = field(default_factory=set)
    code_hashes: dict[str, str] = field(default_factory=dict)
    expired_hashes: set[str] = field(default_factory=set)
    clients: list[FakeClient] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    down: bool = False
    flood: int = 0
    connect_delay: float = 0

    def add_account(self, phone: str, tg_user_id: int, **kw) -> FakeAccount:
        self.accounts[phone] = FakeAccount(tg_user_id, **kw)
        return self.accounts[phone]

    def factory(self, api_id: int, api_hash: str, session: str) -> FakeClient:
        return FakeClient(self, api_id, api_hash, session)

    def service(self, timeout: float = 5.0) -> TelegramClientService:
        return TelegramClientService(self.factory, timeout=timeout)

    def revoke_everything(self) -> None:
        self.live.clear()  # e.g. "terminate all other sessions" in the Telegram app

    def assert_all_clients_closed(self) -> bool:
        return all(c.disconnected for c in self.clients)
