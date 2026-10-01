"""CSRF tokens.

The token is an HMAC (keyed with APP_SECRET) of a *binding* value: the session
cookie when logged in, otherwise a random pre-session cookie. So a token is only
valid for the browser that received it, and it changes whenever the session
changes. Works for normal forms (hidden field) and HTMX (X-CSRF-Token header).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

FORM_FIELD = "csrf_token"
HEADER = "x-csrf-token"


def new_binding() -> str:
    return secrets.token_urlsafe(32)


def token_for(secret: str, binding: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), b"csrf-v1:" + binding.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def verify(secret: str, binding: str, submitted: str) -> bool:
    if not binding or not submitted:
        return False
    return hmac.compare_digest(token_for(secret, binding), submitted)


def origin_ok(app_url: str, origin: str | None, referer: str | None) -> bool:
    """Extra defence: if the browser tells us where the request came from, it must be us."""
    expected = app_url.rstrip("/").lower()
    if origin is not None:
        return origin.strip().lower() == expected
    if referer:
        ref = referer.strip().lower()
        return ref == expected or ref.startswith(expected + "/")
    return True  # no header sent (privacy settings): the token check still applies
