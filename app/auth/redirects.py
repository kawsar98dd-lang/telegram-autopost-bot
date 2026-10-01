"""Open-redirect protection for ?next= parameters."""

from __future__ import annotations

from urllib.parse import urlsplit

_BLOCKED_PREFIXES = ("/login", "/logout", "/setup")


def safe_next(value: str | None, default: str = "/") -> str:
    if not value or len(value) > 300:
        return default
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    if parts.path.startswith(_BLOCKED_PREFIXES):
        return default
    return value
