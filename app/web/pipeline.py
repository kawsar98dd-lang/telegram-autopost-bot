"""Request pipeline: routing, session, setup gate, authentication, authorization, CSRF, headers, errors."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable
from urllib.parse import quote

from ..auth import csrf
from .errors import HttpError, error_response
from .http import Request, Response, redirect

log = logging.getLogger(__name__)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_BINDING = re.compile(r"^[A-Za-z0-9_\-]{20,100}$")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


class Auth(Enum):
    ANONYMOUS = "anonymous"  # login / first-run setup: signed-in users are sent to the dashboard
    USER = "user"
    ADMIN = "admin"


Handler = Callable[[Request], Awaitable[Response]]


@dataclass(frozen=True)
class Route:
    name: str
    method: str
    path: str
    handler: Handler
    auth: Auth = Auth.USER
    permission: str | None = None


class WebApp:
    def __init__(self, ctx_provider, routes: list[Route]) -> None:
        self._ctx = ctx_provider
        self._routes: dict[str, dict[str, Route]] = {}
        for route in routes:
            self._routes.setdefault(route.path, {})[route.method] = route
        # Paths such as /telegram/verify/{attempt_id}: ids are 36-character UUIDs, nothing else matches.
        self._patterns: list[tuple[re.Pattern, dict[str, Route]]] = [
            (re.compile("^" + re.sub(r"\\\{([a-z_]+)\\\}", r"(?P<\1>[0-9a-fA-F-]{36})", re.escape(path)) + "$"), methods)
            for path, methods in self._routes.items() if "{" in path
        ]

    # -- entry points used by the ASGI adapter --------------------------------------------
    async def handle(self, request: Request) -> Response:
        request.ctx = self._ctx()
        try:
            response = await self._dispatch(request)
        except HttpError as exc:
            response = error_response(request, exc.status, retry_after=exc.retry_after)
        except Exception:
            reference = uuid.uuid4().hex[:8]
            log.exception("unhandled error (reference %s)", reference)  # redacting formatter is active
            response = error_response(request, 500, reference=reference)
        self._finish(request, response)
        return response

    def error(self, request: Request, status: int) -> Response:
        request.ctx = self._ctx()
        response = error_response(request, status)
        self._finish(request, response)
        return response

    # -- pipeline ---------------------------------------------------------------------------
    async def _dispatch(self, request: Request) -> Response:
        ctx = request.ctx
        methods = self._routes.get(request.path) if "{" not in request.path else None
        if methods is None:
            for pattern, candidate in self._patterns:
                match = pattern.match(request.path)
                if match:
                    methods, request.path_params = candidate, match.groupdict()
                    break
        if methods is None:
            raise HttpError(404)
        method = "GET" if request.method == "HEAD" else request.method
        route = methods.get(method)
        if route is None:
            raise HttpError(405)

        await self._load_session(request)
        self._prepare_csrf(request)

        setup_complete = await ctx.users.is_setup_complete()
        if route.name == "setup":
            if setup_complete:
                raise HttpError(404)  # permanently disabled once an admin exists
        elif not setup_complete:
            return redirect(request, "/setup")

        if route.auth is Auth.ANONYMOUS:
            if request.user is not None:
                return redirect(request, "/")
        else:
            if request.user is None:
                return self._unauthenticated(request)
            if route.auth is Auth.ADMIN and not request.user.is_admin:
                raise HttpError(403)
            if route.permission and not request.user.can(route.permission):
                raise HttpError(403)

        if request.method in UNSAFE_METHODS:
            self._check_csrf(request)
        return await route.handler(request)

    async def _load_session(self, request: Request) -> None:
        ctx = request.ctx
        token = request.cookies.get(ctx.session_cookie, "")
        if not token:
            return
        info = await ctx.sessions.lookup(token)
        if info is None:
            request.clear_session = True
            return
        request.session, request.user = info, info.principal

    def _prepare_csrf(self, request: Request) -> None:
        ctx = request.ctx
        binding = request.session.token if request.session else ""
        if not binding:
            cookie = request.cookies.get(ctx.csrf_cookie, "")
            if _BINDING.match(cookie):
                binding = cookie
            else:
                binding = request.new_csrf_binding = csrf.new_binding()
        request.csrf_binding = binding
        request.csrf_token = csrf.token_for(ctx.settings.app_secret, binding)

    def _check_csrf(self, request: Request) -> None:
        ctx = request.ctx
        if not csrf.origin_ok(ctx.settings.app_url, request.headers.get("origin"), request.headers.get("referer")):
            raise HttpError(403)
        if request.new_csrf_binding:  # browser never received a token from us
            raise HttpError(403)
        submitted = request.form.get(csrf.FORM_FIELD) or request.headers.get(csrf.HEADER, "")
        if not csrf.verify(ctx.settings.app_secret, request.csrf_binding, submitted):
            raise HttpError(403)

    def _unauthenticated(self, request: Request) -> Response:
        if request.wants_json:
            raise HttpError(401)
        target = "/login"
        if request.method == "GET" and not request.is_htmx:
            path = request.path + ("?" + "&".join(f"{quote(k)}={quote(v)}" for k, v in request.query.items()) if request.query else "")
            target = "/login?next=" + quote(path, safe="")
        return redirect(request, target)

    def _finish(self, request: Request, response: Response) -> None:
        ctx = request.ctx
        secure = ctx.settings.cookie_secure
        if request.new_csrf_binding:
            response.set_cookie(ctx.csrf_cookie, request.new_csrf_binding, max_age=12 * 3600, secure=secure)
        if request.clear_session:
            response.delete_cookie(ctx.session_cookie, secure=secure)
        response.header("Content-Security-Policy", CSP)
        response.header("X-Content-Type-Options", "nosniff")
        response.header("X-Frame-Options", "DENY")
        response.header("Referrer-Policy", "same-origin")
        response.header("Cache-Control", "no-store")
        if secure:
            response.header("Strict-Transport-Security", "max-age=31536000")
