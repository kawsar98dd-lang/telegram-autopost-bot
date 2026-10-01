"""FastAPI host application: lifespan, health checks, static files, license gate, web pages."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__, branding
from ..config import Settings, load_settings
from ..db.migrate import ensure_schema
from ..db.pool import PoolDb, create_pool
from ..licensing.gate import LicenseGateMiddleware
from ..licensing.setup import build_license_manager
from ..logging_setup import setup_logging
from ..security.crypto import init_cipher
from .asgi import ASGIAdapter
from .context import build_context
from .pipeline import WebApp
from .routes import ROUTES

log = logging.getLogger(__name__)
LICENSE_CHECK_SECONDS = 300
MAINTENANCE_SECONDS = 3600


async def _background(ctx) -> None:
    """License re-verification (frequent) and housekeeping of expired sessions / old counters."""
    ticks = 0
    while True:
        try:
            await ctx.license.ensure_fresh()
            if ticks % (MAINTENANCE_SECONDS // LICENSE_CHECK_SECONDS) == 0:
                await ctx.sessions.purge_expired()
                await ctx.limiter.purge(int(ctx.clock()))
                await ctx.telegram_connect.purge_expired()
        except Exception:  # keep the loop alive; secrets are redacted in logs
            log.exception("background task failed")
        ticks += 1
        await asyncio.sleep(LICENSE_CHECK_SECONDS)


def create_app(settings: Settings | None = None, *, db=None, license_manager=None, clock=time.time) -> FastAPI:
    """Build the real FastAPI application.

    Production calls it without arguments (``uvicorn --factory app.web.main:create_app``): settings come from
    the environment, PostgreSQL is opened and migrated in the lifespan. Tests may inject ``db`` and
    ``license_manager`` to run the very same application without PostgreSQL / a license server.
    """
    settings = settings or load_settings()
    setup_logging(settings.log_level)
    cipher = init_cipher(settings.session_encryption_key)
    holder: dict = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        owns_db = db is None
        if owns_db:
            if settings.auto_migrate:
                await ensure_schema(settings.database_url)
            database = PoolDb(await create_pool(settings.database_url))
        else:
            database = db
        manager = license_manager or await build_license_manager(settings, database, cipher)
        await manager.ensure_fresh()
        ctx = build_context(settings, database, manager, clock=clock)
        holder["ctx"] = ctx
        app.state.ctx = ctx
        task = asyncio.create_task(_background(ctx))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            holder.pop("ctx", None)
            if owns_db:
                await database.close()

    app = FastAPI(title=branding.APP_NAME, version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict:
        await holder["ctx"].db.fetchrow("SELECT 1 AS ok")
        return {"status": "ready"}

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    pages = ASGIAdapter(WebApp(lambda: holder["ctx"], ROUTES), lambda: holder["ctx"].settings.trust_proxy_headers)
    app.mount("/", pages)  # every other path is handled by the page pipeline (404s included)

    class _Gate:
        """Applies the license gate once the manager exists (it is created during start-up)."""

        def __init__(self, inner) -> None:
            self.inner, self.gate = inner, None

        async def __call__(self, scope, receive, send):
            if scope["type"] == "lifespan":
                return await self.inner(scope, receive, send)
            if self.gate is None:
                self.gate = LicenseGateMiddleware(self.inner, holder["ctx"].license)
            return await self.gate(scope, receive, send)

    app.add_middleware(_Gate)
    return app
