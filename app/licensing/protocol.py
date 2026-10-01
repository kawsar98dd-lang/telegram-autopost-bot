"""License wire protocol helpers (shared vocabulary between client and server)."""

from __future__ import annotations

import base64
import json
import re
import secrets
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROTOCOL_VERSION = 1

# Statuses that appear inside SIGNED payloads.
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUS_DISABLED = "disabled"
STATUS_EXPIRED = "expired"
STATUS_DEACTIVATED = "deactivated"  # activation removed/reset/transferred by the seller
SIGNED_STATUSES = {STATUS_ACTIVE, STATUS_REVOKED, STATUS_DISABLED, STATUS_EXPIRED, STATUS_DEACTIVATED}

DEFAULT_VERIFY_INTERVAL = 24 * 3600
DEFAULT_OFFLINE_GRACE = 7 * 24 * 3600


def canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_signature(public_key_b64: str, payload: dict[str, Any], signature_b64: str) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(b64url_decode(public_key_b64))
        key.verify(b64url_decode(signature_b64), canonical_json(payload))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


_KEY_CHARS = re.compile(r"[^A-Z0-9]")


def normalize_license_key(raw: str) -> str:
    """Accept keys typed with/without dashes or spaces; return TAP-XXXXX-XXXXX-XXXXX-XXXXX."""
    cleaned = _KEY_CHARS.sub("", (raw or "").upper())
    if not cleaned.startswith("TAP") or len(cleaned) != 23:
        raise ValueError("invalid license key format")
    body = cleaned[3:]
    return "TAP-" + "-".join(body[i : i + 5] for i in range(0, 20, 5))


def new_nonce() -> str:
    return secrets.token_urlsafe(24)
