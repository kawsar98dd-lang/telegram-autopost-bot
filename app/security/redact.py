"""Scrub secrets from log output.

Secrets should never be logged in the first place; this is a safety net that
masks common patterns (key=value pairs, Fernet tokens, license keys and very
long base64 blobs such as Telegram string sessions).
"""

from __future__ import annotations

import logging
import re

MASK = "[REDACTED]"

_KEY_VALUE = re.compile(
    r"""(?ix)
    (["']?
      (?:password|passwd|api[_-]?hash|api[_-]?key|secret|token|session(?:[_-]?(?:string|data))?
         |license[_-]?key|authorization|cookie|verification[_-]?code|otp|encryption[_-]?key)
      ["']?\s*[:=]\s*)
    ("[^"]*"|'[^']*'|[^\s,;&}\]]+)
    """
)
_FERNET = re.compile(r"gAAAA[A-Za-z0-9_\-]{20,}={0,2}")
_LICENSE_KEY = re.compile(r"\b[A-Z0-9]{3,4}(?:-[A-Z0-9]{5}){4}\b")
_LONG_BLOB = re.compile(r"[A-Za-z0-9+/_\-]{120,}={0,2}")


def redact(text: str) -> str:
    text = _KEY_VALUE.sub(lambda m: m.group(1) + MASK, text)
    text = _FERNET.sub(MASK, text)
    text = _LICENSE_KEY.sub(MASK, text)
    text = _LONG_BLOB.sub(MASK, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
