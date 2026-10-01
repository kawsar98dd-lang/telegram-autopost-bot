"""ASGI middleware: blocks the application while no valid license is active.

Browsers are sent to /activate (which itself requires the admin to sign in);
API/HTMX clients get a JSON 503. Health checks, static files and the pages needed
to sign in and activate stay reachable.
"""

from __future__ import annotations

import json

from .manager import LicenseManager

DEFAULT_EXEMPT = ("/health", "/static", "/setup", "/login", "/logout", "/activate", "/license")


class LicenseGateMiddleware:
    def __init__(self, app, manager: LicenseManager, exempt_prefixes: tuple[str, ...] = DEFAULT_EXEMPT):
        self.app = app
        self.manager = manager
        self.exempt = exempt_prefixes

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        status = self.manager.status()
        if status.enabled or any(path == p or path.startswith(p + "/") for p in self.exempt):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if "application/json" in headers.get("accept", "") or "hx-request" in headers:
            body = json.dumps({"error": "license_required", "state": status.state.value}).encode()
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                                    (b"hx-redirect", b"/activate"), (b"content-length", str(len(body)).encode())]})
            return await send({"type": "http.response.body", "body": body})
        await send({"type": "http.response.start", "status": 303,
                    "headers": [(b"location", b"/activate"), (b"cache-control", b"no-store"), (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})
