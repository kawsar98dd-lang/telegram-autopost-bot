"""Test harness for the web layer: real ASGI calls, a cookie jar, and a fake license server."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

from tests.fake_telegram import FakeTelegram
from tests.support import Clock, FakeLicenseServer, migrated_db, signing
from app.auth.ratelimit import RateLimiter
from app.config import settings_from_env
from app.db.models import new_id
from app.licensing.client import LicenseClient
from app.licensing.gate import LicenseGateMiddleware
from app.licensing.manager import LicenseManager
from app.security.crypto import Cipher, generate_key
from app.security.passwords import hash_password
from app.web.asgi import ASGIAdapter
from app.web.context import build_context
from app.web.pipeline import WebApp
from app.web.routes import ROUTES

KEY = "TAP-ABCDE-FGHJK-MNPQR-STVWX"
PASSWORD = "correct-horse-battery"
BASE_ENV = {
    "APP_ENV": "production", "APP_URL": "https://poster.example.com", "APP_SECRET": "s" * 48,
    "DATABASE_URL": "postgresql://u:p@db/x", "SESSION_ENCRYPTION_KEY": generate_key(),
}
_HASH: str | None = None


def password_hash() -> str:
    global _HASH
    if _HASH is None:
        _HASH = hash_password(PASSWORD)
    return _HASH


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    set_cookies: list[str]
    body: str

    @property
    def location(self) -> str:
        return self.headers.get("location", "")


@dataclass
class Client:
    app: object
    ip: str = "203.0.113.5"
    cookies: dict[str, str] = field(default_factory=dict)
    origin: str | None = "https://poster.example.com"
    log: list[Reply] = field(default_factory=list)

    async def request(self, method, path, *, form=None, headers=None, query="", origin=..., ip=None) -> Reply:
        h = {"host": "poster.example.com", "user-agent": "TestBrowser/1.0"}
        if self.cookies:
            h["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        use_origin = self.origin if origin is ... else origin
        if method != "GET" and use_origin is not None:
            h["origin"] = use_origin
        body = b""
        if form is not None:
            body = urlencode(form).encode()
            h["content-type"] = "application/x-www-form-urlencoded"
        h.update({k.lower(): v for k, v in (headers or {}).items()})
        scope = {"type": "http", "method": method, "path": path, "query_string": query.encode(),
                 "headers": [(k.encode(), v.encode()) for k, v in h.items()],
                 "client": (ip or self.ip, 5555)}
        sent = []
        queue = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return queue.pop(0) if queue else {"type": "http.disconnect"}

        async def send(msg):
            sent.append(msg)

        await self.app(scope, receive, send)
        start = sent[0]
        hdrs, cookies = {}, []
        for k, v in start["headers"]:
            k, v = k.decode(), v.decode()
            if k == "set-cookie":
                cookies.append(v)
            else:
                hdrs[k] = v
        for c in cookies:
            name, _, rest = c.partition("=")
            value = rest.split(";")[0]
            if "Max-Age=0" in c:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        reply = Reply(start["status"], hdrs, cookies, b"".join(m.get("body", b"") for m in sent[1:]).decode())
        self.log.append(reply)
        return reply

    async def get(self, path, **kw):
        return await self.request("GET", path, **kw)

    async def post(self, path, form=None, **kw):
        return await self.request("POST", path, form=form if form is not None else {}, **kw)

    def csrf(self, html: str) -> str:
        m = re.search(r'name="csrf-token" content="([^"]+)"', html)
        assert m, "page has no CSRF token"
        return m.group(1)

    async def token_from(self, path="/login") -> str:
        return self.csrf((await self.get(path)).body)

    def session_cookie(self) -> str:
        return self.cookies.get("__Host-tap_session", "")


class Env:
    """A complete installation on an in-memory database with a fake license server."""

    def __init__(self) -> None:
        self.clock = Clock()

    async def start(self, *, env_overrides=None, activated=False, with_admin=False, enforcement=True) -> "Env":
        self.settings = settings_from_env({**BASE_ENV, **(env_overrides or {})})
        self.db = await migrated_db()
        private, public = signing.generate_keypair()
        self.private = private
        self.server = FakeLicenseServer(private, self.clock)
        self.server.add_license(KEY)
        from app.licensing.store import MemoryLicenseStore

        self.manager = LicenseManager(
            store=MemoryLicenseStore(), client=LicenseClient("https://license.example.com", transport=self.server.transport),
            public_key=public, product="telegram-auto-poster", installation_id="inst-1",
            host=self.settings.app_host, app_version="0.1.0", enforcement=enforcement, clock=self.clock)
        self.world = FakeTelegram()
        self.cipher = Cipher(self.settings.session_encryption_key)
        self.ctx = build_context(self.settings, self.db, self.manager, cipher=self.cipher,
                                 telegram=self.world.service(), clock=self.clock)
        self.web = WebApp(lambda: self.ctx, ROUTES)
        adapter = ASGIAdapter(self.web, lambda: self.settings.trust_proxy_headers)
        self.asgi = LicenseGateMiddleware(adapter, self.manager)
        if activated:
            await self.manager.activate(KEY)
        if with_admin:
            self.admin_id = await self.add_user("admin@example.com", admin=True)
        return self

    async def add_user(self, email, *, admin=False, active=True, display_name="") -> str:
        uid = new_id()
        self.db.conn.execute(
            "INSERT INTO users (id, email, password_hash, is_admin, is_active, display_name) VALUES (?,?,?,?,?,?)",
            (uid, email, password_hash(), admin, active, display_name or None))
        if admin:
            self.db.conn.execute("INSERT OR IGNORE INTO app_state (key, value) VALUES ('setup_completed', '1')")
        self.db.conn.commit()
        return uid

    def client(self, **kw) -> Client:
        return Client(self.asgi, **kw)

    async def login(self, client: Client, email="admin@example.com", password=PASSWORD, next_="") -> Reply:
        token = await client.token_from("/login")
        return await client.post("/login", {"csrf_token": token, "email": email, "password": password, "next": next_})
