"""Fernet encryption for secrets at rest (Telegram sessions, license key).

* Keys come from the environment (SESSION_ENCRYPTION_KEY), never from code and never from the database.
* Each entry may be EITHER a ready-made Fernet key (used as is) OR any other high-entropy secret of at least
  32 characters, for example the value a hosting platform generates for you. The latter is turned into a Fernet key
  with HKDF-SHA256 (RFC 5869) using fixed, application-specific salt/info values. The derivation is deterministic:
  the same secret yields the same key on every start and every deployment, and nothing derived is stored or logged.
* Several comma-separated entries are allowed: the first encrypts, all decrypt (key rotation). Entries must not contain
  commas or white space.
* A ``context`` string (for example ``telegram_session:<account id>``) is bound into the ciphertext. A token copied
  from one row/user to another fails to decrypt, which supports per-user session isolation.
* Error messages never contain key material.
"""

from __future__ import annotations

import base64
import hmac
import re

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_ENVELOPE_VERSION = b"v1"


class CryptoError(Exception):
    """Invalid key material or misuse."""


class DecryptionError(CryptoError):
    """Ciphertext is corrupt, wrong key, or bound to a different context."""


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


_KDF_SALT = b"telegram-auto-poster/session-encryption/salt/v1"
_KDF_INFO = b"telegram-auto-poster/session-encryption/fernet-key/v1"
MIN_SECRET_LENGTH = 32  # for secrets that are not already Fernet keys
MIN_DISTINCT_CHARACTERS = 10  # rejects "aaaa..." and other obviously non-random values
_PLACEHOLDERS = ("change", "replace", "example", "your-", "secret", "password")

_INVALID_MESSAGE = (
    "SESSION_ENCRYPTION_KEY must be a Fernet key (python scripts/generate_keys.py) or a random secret of at least "
    f"{MIN_SECRET_LENGTH} characters without spaces or commas"
)


def _pad(key: str) -> str:
    """Restore missing base64 '=' padding. The key must still decode to exactly 32 bytes (Fernet checks)."""
    return key + "=" * (-len(key) % 4)


def _is_fernet_key(key: str) -> bool:
    try:
        Fernet(key.encode("ascii"))
        return True
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def _derive_fernet_key(secret: str) -> str:
    """HKDF-SHA256 of a high-entropy secret -> 32 bytes -> url-safe base64 (the Fernet key format)."""
    material = HKDF(algorithm=hashes.SHA256(), length=32, salt=_KDF_SALT, info=_KDF_INFO).derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(material).decode("ascii")


def _resolve_entry(entry: str) -> str:
    padded = _pad(entry)
    if _is_fernet_key(padded):  # backward compatible: an existing valid Fernet key is used directly
        return padded
    lowered = entry.lower()
    if (len(entry) < MIN_SECRET_LENGTH or len(set(entry)) < MIN_DISTINCT_CHARACTERS
            or any(lowered.startswith(p) for p in _PLACEHOLDERS)):
        raise CryptoError(_INVALID_MESSAGE)
    return _derive_fernet_key(entry)


def parse_keys(raw: str) -> list[str]:
    """Resolve every configured entry to a Fernet key. Never echoes input in errors."""
    entries = [e for e in re.split(r"[,\s]+", raw or "") if e]
    if not entries:
        raise CryptoError("no encryption key provided")
    return [_resolve_entry(e) for e in entries]


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
