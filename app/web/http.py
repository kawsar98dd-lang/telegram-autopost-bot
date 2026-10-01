"""Framework-neutral request/response objects used by the web layer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..auth.permissions import Principal

_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9_\-\.~]*$")


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)  # lower-case names
    cookies: dict[str, str] = field(default_factory=dict)
    path_params: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)
    client_ip: str = "0.0.0.0"
    # filled in by the pipeline
    ctx: Any = None
    user: Principal | None = None
    session: Any = None
    csrf_token: str = ""
    new_csrf_binding: str = ""
    csrf_binding: str = ""
    clear_session: bool = False

    @property
    def is_htmx(self) -> bool:
        return self.headers.get("hx-request", "").lower() == "true"

    @property
    def wants_json(self) -> bool:
        return "application/json" in self.headers.get("accept", "")


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)

    def header(self, name: str, value: str) -> "Response":
        if "\r" in value or "\n" in value:
            raise ValueError("header injection attempt")
        self.headers.append((name, value))
        return self

    def set_cookie(self, name: str, value: str, *, max_age: int, secure: bool) -> "Response":
        if not _COOKIE_VALUE.match(value) or not re.match(r"^[A-Za-z0-9_\-]+$", name):
            raise ValueError("invalid cookie")
        parts = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
        if secure:
            parts.append("Secure")
        return self.header("Set-Cookie", "; ".join(parts))

    def delete_cookie(self, name: str, *, secure: bool) -> "Response":
        return self.set_cookie(name, "", max_age=0, secure=secure)


def redirect(request: Request, location: str) -> Response:
    """303 for browsers; HX-Redirect (full page navigation) for HTMX requests."""
    if request.is_htmx:
        return Response(200).header("HX-Redirect", location)
    return Response(303).header("Location", location)


def json_response(data: dict, status: int = 200) -> Response:
    import json

    return Response(status, json.dumps(data).encode("utf-8"), "application/json")


def parse_cookies(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for chunk in header.split(";"):
        name, sep, value = chunk.strip().partition("=")
        if sep and name and name not in cookies:
            cookies[name] = value.strip('"')
    return cookies
