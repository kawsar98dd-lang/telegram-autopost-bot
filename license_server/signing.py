"""Seller-side signing (kept independent of the customer application)."""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def canonical_json(payload: dict[str, Any]) -> bytes:
    # MUST stay identical to app/licensing/protocol.py (a test enforces this).
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def generate_keypair() -> tuple[str, str]:
    """Return (private_key_b64url, public_key_b64url), raw 32-byte Ed25519 keys."""
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    raw_public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return _b64url(raw_private), _b64url(raw_public)


def load_private_key(private_b64url: str) -> Ed25519PrivateKey:
    raw = base64.urlsafe_b64decode(private_b64url + "=" * (-len(private_b64url) % 4))
    return Ed25519PrivateKey.from_private_bytes(raw)


def sign_payload(private_b64url: str, payload: dict[str, Any]) -> dict[str, Any]:
    signature = load_private_key(private_b64url).sign(canonical_json(payload))
    return {"payload": payload, "signature": _b64url(signature)}


def generate_license_key() -> str:
    body = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(20))
    return "TAP-" + "-".join(body[i : i + 5] for i in range(0, 20, 5))
