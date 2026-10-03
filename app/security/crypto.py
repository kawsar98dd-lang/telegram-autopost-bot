"""Fernet encryption for secrets at rest (Telegram sessions, license key).

* Keys come from the environment (SESSION_ENCRYPTION_KEY), never from code.
* Several comma-separated keys are allowed: the first encrypts, all decrypt.
  This makes key rotation possible without losing existing data.
* A ``context`` string (for example ``telegram_session:<account id>``) is bound
  into the ciphertext. A token copied from one row/user to another fails to
  decrypt, which supports per-user session isolation.
"""

from __future__ import annotations

import hmac
import re

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_ENVELOPE_VERSION = b"v1"


class CryptoError(Exception):
    """Invalid key material or misuse."""


class DecryptionError(CryptoError):
    """Ciphertext is corrupt, wrong key, or bound to a different context."""


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def _pad(key: str) -> str:
    """Restore missing base64 '=' padding. The key must still decode to exactly 32 bytes (Fernet checks)."""
    return key + "=" * (-len(key) % 4)


def parse_keys(raw: str) -> list[str]:
    keys = [_pad(k) for k in re.split(r"[,\s]+", raw or "") if k]
    if not keys:
        raise CryptoError("no encryption key provided")
    for key in keys:
        try:
            Fernet(key.encode("ascii"))
        except (ValueError, TypeError, UnicodeEncodeError) as exc:
            raise CryptoError(
                "SESSION_ENCRYPTION_KEY is not a valid Fernet key "
                "(generate one with: python scripts/generate_keys.py)"
            ) from exc
    return keys


class Cipher:
    def __init__(self, keys_raw: str) -> None:
        keys = parse_keys(keys_raw)
        self._multi = MultiFernet([Fernet(k.encode("ascii")) for k in keys])

    @staticmethod
    def _check_context(context: str) -> bytes:
        if "\x00" in context:
            raise ValueError("context must not contain NUL")
        return context.encode("utf-8")

    def encrypt(self, plaintext: str, context: str = "") -> str:
        ctx = self._check_context(context)
        data = _ENVELOPE_VERSION + b"\x00" + ctx + b"\x00" + plaintext.encode("utf-8")
        return self._multi.encrypt(data).decode("ascii")

    def decrypt(self, token: str, context: str = "") -> str:
        ctx = self._check_context(context)
        try:
            data = self._multi.decrypt(token.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError) as exc:
            raise DecryptionError("cannot decrypt value (wrong key or corrupt data)") from exc
        parts = data.split(b"\x00", 2)
        if len(parts) != 3 or parts[0] != _ENVELOPE_VERSION:
            raise DecryptionError("unsupported ciphertext format")
        if not hmac.compare_digest(parts[1], ctx):
            raise DecryptionError("ciphertext belongs to a different context")
        return parts[2].decode("utf-8")

    def rotate(self, token: str) -> str:
        """Re-encrypt a token with the current primary key."""
        try:
            return self._multi.rotate(token.encode("ascii")).decode("ascii")
        except (InvalidToken, UnicodeEncodeError) as exc:
            raise DecryptionError("cannot rotate value (wrong key or corrupt data)") from exc


_cipher: Cipher | None = None


def init_cipher(keys_raw: str) -> Cipher:
    global _cipher
    _cipher = Cipher(keys_raw)
    return _cipher


def get_cipher() -> Cipher:
    if _cipher is None:
        raise CryptoError("cipher not initialised (call init_cipher at startup)")
    return _cipher
