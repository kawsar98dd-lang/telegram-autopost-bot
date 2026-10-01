"""HTTP client for the seller's license server (standard library only)."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from .protocol import PROTOCOL_VERSION

Transport = Callable[[str, dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]]

_MAX_RESPONSE_BYTES = 64 * 1024


class LicenseError(Exception):
    code = "license_error"

    def __init__(self, message: str = "", code: str | None = None) -> None:
        super().__init__(message or self.code)
        if code:
            self.code = code


class LicenseUnreachable(LicenseError):
    code = "unreachable"


class LicenseRejected(LicenseError):
    """The server answered with an (unsigned) refusal, e.g. invalid key or activation limit."""


class LicenseResponseInvalid(LicenseError):
    code = "invalid_response"


def _is_local(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1")


class LicenseClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        allow_insecure: bool = False,
        transport: Transport | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("license server URL must be an http(s) URL")
        if parts.scheme == "http" and not (allow_insecure or _is_local(parts.hostname)):
            raise ValueError("license server URL must use https")
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport or self._http_transport

    async def call(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        body = {**body, "protocol": PROTOCOL_VERSION}
        try:
            return await self._transport(path, body)
        except LicenseError:
            raise
        except Exception as exc:  # network errors, timeouts, bad JSON
            raise LicenseUnreachable(f"license server not reachable ({type(exc).__name__})") from exc

    async def _http_transport(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return await asyncio.to_thread(self._blocking_post, path, body)

    def _blocking_post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(
            self._base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "telegram-auto-poster"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:  # noqa: S310
                status, raw = resp.status, resp.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise LicenseResponseInvalid("response too large")
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        return status, data
