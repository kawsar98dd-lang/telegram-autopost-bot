"""Password hashing with scrypt (standard library, no extra dependency)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

_N, _R, _P = 2**15, 8, 1
_MAXMEM = 128 * 1024 * 1024
_SALT_BYTES = 16
_DKLEN = 32
_PREFIX = "scrypt"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=_DKLEN
    )


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(_SALT_BYTES)
    digest = _derive(password, salt, _N, _R, _P)
    return f"{_PREFIX}${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def _parse(stored: str):
    prefix, n, r, p, salt, digest = stored.split("$")
    if prefix != _PREFIX:
        raise ValueError("unknown hash format")
    return int(n), int(r), int(p), base64.b64decode(salt), base64.b64decode(digest)


def verify_password(password: str, stored: str) -> bool:
    try:
        n, r, p, salt, expected = _parse(stored)
        actual = _derive(password, salt, n, r, p)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    try:
        n, r, p, _, _ = _parse(stored)
    except (ValueError, TypeError):
        return True
    return (n, r, p) != (_N, _R, _P)
