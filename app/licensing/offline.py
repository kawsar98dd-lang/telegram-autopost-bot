"""Offline signed license file: verified locally with the seller's PUBLIC key, no license server, no network.

File format (JSON, written by license_server/issue_license.py, which only the seller owns):

    {"payload": {"type": "offline_license", "v": 1, "license_id": "...", "product": "telegram-auto-poster",
                 "customer_ref": "...", "license_type": "standard", "issued_at": 1800000000,
                 "perpetual": true, "expires_at": null, "host": "optional"},
     "signature": "<base64url Ed25519 signature over the canonical JSON of payload>"}

What this DOES provide: a license that cannot be created, extended or edited without the seller's private key, bound to the
product (and optionally to the public address APP_URL), with an optional expiry. What it can NOT provide (documented in
docs/LICENSING.md): activation counts, revocation, or protection from someone who edits the source code. The customer
runtime only ever holds the PUBLIC key (app/licensing/constants.py); the private key never belongs in this project.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .client import LicenseRejected
from .manager import LicenseState, LicenseStatus, LicenseSummary
from .protocol import b64url_decode, verify_signature

log = logging.getLogger(__name__)

LICENSE_TYPE_MARK = "offline_license"   # domain separation: other signed payloads (online protocol) can never pass as a license file
FORMAT_VERSION = 1
LICENSE_TYPES = ("standard", "commercial", "trial")
MAX_LICENSE_BYTES = 8192
CLOCK_TOLERANCE = 24 * 3600            # an offline clock may be a little off; a license issued "in the future" beyond this is refused
RELOAD_SECONDS = 30                    # how often the file is looked at again (so a replaced file takes effect without restart)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,63}$")
_REF = re.compile(r"^[^\x00-\x1f\x7f]{1,100}$")


class LicenseFileError(Exception):
    """The license file is unusable. ``state`` is the LicenseState to show; ``message`` is safe to display."""

    def __init__(self, state: LicenseState, message: str, *, license_id: str = "", expires_at: int | None = None) -> None:
        self.state, self.message, self.license_id, self.expires_at = state, message, license_id, expires_at
        super().__init__(message)


@dataclass(frozen=True)
class OfflineLicense:
    license_id: str
    product: str
    customer_ref: str
    license_type: str
    issued_at: int
    perpetual: bool
    expires_at: int | None
    host: str


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def decode_inline(content: str) -> str:
    """LICENSE_FILE_CONTENT may be the JSON itself or its base64 form (easier inside .env files and hosting dashboards)."""
    text = (content or "").strip()
    if text.startswith("{"):
        return text
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=False)
        return raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise LicenseFileError(LicenseState.TAMPERED, "The license text is not valid JSON or base64.") from None


def verify_license_text(text: str, *, public_key: str, product: str, host: str, now: int) -> OfflineLicense:
    """Parse and fully verify a license file. Returns the license or raises LicenseFileError (never anything else)."""
    bad = lambda msg, state=LicenseState.TAMPERED: LicenseFileError(state, msg)  # noqa: E731
    if len(text.encode("utf-8", "replace")) > MAX_LICENSE_BYTES:
        raise bad("The license file is too large to be a license.")
    try:
        doc = json.loads(text)
    except ValueError:
        raise bad("The license file is not valid JSON. Use the file exactly as you received it.") from None
    if not isinstance(doc, dict) or set(doc) != {"payload", "signature"}:
        raise bad("The license file has an unexpected structure.")
    payload, signature = doc["payload"], doc["signature"]
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise bad("The license file has an unexpected structure.")
    try:
        b64url_decode(signature)
    except (binascii.Error, ValueError):
        raise bad("The license signature is malformed.") from None
    if not verify_signature(public_key, payload, signature):
        raise bad("The license signature is not valid for this product build. The file may be modified, or it was issued by someone else.")

    # From here on the content is authentic (signed by the seller); still validate its shape strictly.
    if payload.get("type") != LICENSE_TYPE_MARK or payload.get("v") != FORMAT_VERSION:
        raise bad("This is not a license file of a supported format.")
    license_id = payload.get("license_id")
    if not isinstance(license_id, str) or not _ID.match(license_id):
        raise bad("The license ID is malformed.")
    if payload.get("product") != product:
        raise LicenseFileError(LicenseState.MISMATCH, "This license is for a different product.", license_id=license_id)
    ref, ltype = payload.get("customer_ref"), payload.get("license_type")
    issued, perpetual, expires = _int(payload.get("issued_at")), payload.get("perpetual"), payload.get("expires_at")
    if not isinstance(ref, str) or not _REF.match(ref) or ltype not in LICENSE_TYPES or issued is None or issued < 0 \
            or not isinstance(perpetual, bool):
        raise bad("The license data is malformed.")
    if perpetual:
        if expires is not None:
            raise bad("The license data is inconsistent (perpetual license with an expiry).")
        expires_at = None
    else:
        expires_at = _int(expires)
        if expires_at is None or expires_at <= issued:
            raise bad("The license data is inconsistent (missing or invalid expiry).")
    bound = payload.get("host", "")
    if not isinstance(bound, str):
        raise bad("The license data is malformed.")
    lic = OfflineLicense(license_id, product, ref, ltype, issued, perpetual, expires_at, bound.lower())
    if lic.host and lic.host != host.lower():
        raise LicenseFileError(LicenseState.MISMATCH, "This license is bound to a different address (APP_URL).",
                               license_id=license_id, expires_at=expires_at)
    if issued > now + CLOCK_TOLERANCE:
        raise LicenseFileError(LicenseState.TAMPERED, "The system clock is behind the license issue date. Fix the server clock.",
                               license_id=license_id, expires_at=expires_at)
    if expires_at is not None and now > expires_at:
        raise LicenseFileError(LicenseState.EXPIRED, "This license has expired. Install the renewed license file.",
                               license_id=license_id, expires_at=expires_at)
    return lic


class OfflineLicenseManager:
    """Same surface as LicenseManager (status / summary / load / ensure_fresh / verify) for the web gate and the worker."""

    offline = True

    def __init__(self, *, public_key: str, product: str, host: str, file_path: str = "./data/license.json",
                 inline_content: str = "", enforcement: bool = True, clock: Callable[[], float] = time.time,
                 reload_seconds: int = RELOAD_SECONDS) -> None:
        self._public_key, self._product, self._host = public_key, product, host
        self._path, self._inline, self._enforcement = file_path, inline_content, enforcement
        self._clock, self._reload_seconds = clock, reload_seconds
        self._text: str | None = None
        self._read_error: LicenseFileError | None = None
        self._loaded_at = -1e18

    # ---- reading ---------------------------------------------------------------------------------------------
    def _read(self) -> None:
        self._loaded_at = self._clock()
        self._text, self._read_error = None, None
        try:
            if self._inline.strip():
                self._text = decode_inline(self._inline)
                return
            path = Path(self._path)
            if not path.is_file():
                raise LicenseFileError(
                    LicenseState.NOT_ACTIVATED,
                    f"No license file found. Place the license file you received at '{self._path}' "
                    "(or set LICENSE_FILE / LICENSE_FILE_CONTENT), then reload this page.")
            if path.stat().st_size > MAX_LICENSE_BYTES:
                raise LicenseFileError(LicenseState.TAMPERED, "The license file is too large to be a license.")
            self._text = path.read_text(encoding="utf-8")
        except LicenseFileError as exc:
            self._read_error = exc
        except (OSError, UnicodeDecodeError):
            self._read_error = LicenseFileError(LicenseState.TAMPERED, "The license file could not be read.")

    def _maybe_reload(self) -> None:
        if self._clock() - self._loaded_at >= self._reload_seconds or self._loaded_at < 0:
            self._read()

    # ---- state -----------------------------------------------------------------------------------------------
    def _evaluate(self) -> tuple[LicenseStatus, OfflineLicense | None]:
        if not self._enforcement:
            return LicenseStatus(LicenseState.ACTIVE, "license enforcement disabled (development)"), None
        self._maybe_reload()
        if self._read_error is not None:
            e = self._read_error
            return LicenseStatus(e.state, e.message, e.license_id, e.expires_at), None
        try:
            lic = verify_license_text(self._text or "", public_key=self._public_key, product=self._product,
                                      host=self._host, now=int(self._clock()))
        except LicenseFileError as exc:
            return LicenseStatus(exc.state, exc.message, exc.license_id, exc.expires_at), None
        except Exception:  # noqa: BLE001 - fail CLOSED: an unexpected parser problem must lock the app, never crash it open
            log.exception("unexpected error while verifying the license file")
            return LicenseStatus(LicenseState.TAMPERED, "The license file could not be verified."), None
        return LicenseStatus(LicenseState.ACTIVE, "", lic.license_id, lic.expires_at), lic

    def status(self) -> LicenseStatus:
        return self._evaluate()[0]

    def summary(self) -> LicenseSummary:
        st, _ = self._evaluate()
        return LicenseSummary(state=st.state, detail=st.detail, enabled=st.enabled, license_id=st.license_id,
                              expires_at=st.expires_at, last_verified_at=None, last_attempt_failed=False)

    async def load(self) -> None:
        self._read()

    async def ensure_fresh(self) -> LicenseStatus:
        self._maybe_reload()
        return self.status()

    async def verify(self) -> LicenseStatus:  # "Check now" button: re-read the file immediately
        self._read()
        return self.status()

    async def activate(self, license_key: str) -> LicenseStatus:
        raise LicenseRejected("This installation uses a license file; there is nothing to activate.", code="offline_mode")
