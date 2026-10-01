"""License state machine.

Design rules:
* The authority is the seller's license server. The client only trusts data
  carrying a valid Ed25519 signature made with the seller's private key.
* Freshness is measured from the signed ``issued_at`` timestamp, not from any
  locally editable column, so editing the database cannot extend the grace period.
* An unsigned refusal from the server never wipes a valid activation (a network
  attacker must not be able to switch a customer off); only SIGNED
  revoked/disabled/expired/deactivated payloads do.
* Reasonable tamper detection: signature re-check on every evaluation, product /
  installation / host binding, clock-rollback detection.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .client import (
    LicenseClient,
    LicenseError,
    LicenseRejected,
    LicenseResponseInvalid,
    LicenseUnreachable,
)
from .protocol import (
    DEFAULT_OFFLINE_GRACE,
    DEFAULT_VERIFY_INTERVAL,
    SIGNED_STATUSES,
    STATUS_ACTIVE,
    STATUS_DEACTIVATED,
    STATUS_DISABLED,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    canonical_json,
    new_nonce,
    normalize_license_key,
    verify_signature,
)
from .store import LicenseRecord, LicenseStore

log = logging.getLogger(__name__)

CLOCK_TOLERANCE = 15 * 60
RETRY_AFTER_FAILURE = 5 * 60


class LicenseState(str, Enum):
    NOT_ACTIVATED = "not_activated"
    ACTIVE = "active"
    GRACE = "grace"  # server unreachable, still inside the offline grace period
    VERIFY_REQUIRED = "verify_required"  # offline for too long: must reach the server again
    EXPIRED = "expired"
    REVOKED = "revoked"
    MISMATCH = "mismatch"  # activation belongs to another installation/host/product
    TAMPERED = "tampered"


@dataclass(frozen=True)
class LicenseStatus:
    state: LicenseState
    detail: str = ""
    license_id: str = ""
    expires_at: int | None = None

    @property
    def enabled(self) -> bool:
        return self.state in (LicenseState.ACTIVE, LicenseState.GRACE)


@dataclass(frozen=True)
class LicenseSummary:
    """Minimal, safe-to-display view of the local license state (never contains the key)."""

    state: LicenseState
    detail: str
    enabled: bool
    license_id: str
    expires_at: int | None
    last_verified_at: int | None
    last_attempt_failed: bool


class LicenseManager:
    def __init__(
        self,
        *,
        store: LicenseStore,
        client: LicenseClient | None,
        public_key: str,
        product: str,
        installation_id: str,
        host: str,
        app_version: str,
        enforcement: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._client = client
        self._public_key = public_key
        self._product = product
        self._installation_id = installation_id
        self._host = host
        self._app_version = app_version
        self._enforcement = enforcement
        self._clock = clock
        self._record: LicenseRecord | None = None
        self._max_seen = 0

    # ---- local evaluation (no network, no database) -------------------------------------
    def status(self) -> LicenseStatus:
        if not self._enforcement:
            return LicenseStatus(LicenseState.ACTIVE, "license enforcement disabled (development)")
        rec = self._record
        if rec is None:
            return LicenseStatus(LicenseState.NOT_ACTIVATED, "no license activated yet")
        now = int(self._clock())

        try:
            payload = json.loads(rec.payload_json)
        except ValueError:
            return LicenseStatus(LicenseState.TAMPERED, "stored license data is unreadable")
        if not isinstance(payload, dict) or not verify_signature(self._public_key, payload, rec.signature):
            return LicenseStatus(LicenseState.TAMPERED, "stored license signature is invalid")

        if payload.get("product") != self._product:
            return LicenseStatus(LicenseState.MISMATCH, "license is for a different product")
        if payload.get("installation_id") != self._installation_id:
            return LicenseStatus(LicenseState.MISMATCH, "license is bound to a different installation")
        if payload.get("host", "") != self._host:
            return LicenseStatus(LicenseState.MISMATCH, "license is bound to a different address (APP_URL)")

        license_id = str(payload.get("license_id", ""))
        expires_at = payload.get("expires_at")
        if now < max(rec.high_water_at, self._max_seen) - CLOCK_TOLERANCE:
            return LicenseStatus(LicenseState.TAMPERED, "system clock moved backwards", license_id, expires_at)
        issued_at = int(payload.get("issued_at", 0))
        if issued_at > now + CLOCK_TOLERANCE:
            return LicenseStatus(
                LicenseState.TAMPERED, "system clock is behind the license server time", license_id, expires_at
            )

        status = payload.get("status")
        if status in (STATUS_REVOKED, STATUS_DISABLED):
            return LicenseStatus(LicenseState.REVOKED, f"license {status}", license_id, expires_at)
        if status == STATUS_DEACTIVATED:
            return LicenseStatus(LicenseState.NOT_ACTIVATED, "activation was removed; activate again", license_id)
        if status == STATUS_EXPIRED or (expires_at is not None and now > int(expires_at)):
            return LicenseStatus(LicenseState.EXPIRED, "license expired", license_id, expires_at)
        if status != STATUS_ACTIVE:
            return LicenseStatus(LicenseState.TAMPERED, "unknown license status", license_id, expires_at)

        age = now - issued_at
        interval = int(payload.get("verify_interval_seconds", DEFAULT_VERIFY_INTERVAL))
        grace = int(payload.get("offline_grace_seconds", DEFAULT_OFFLINE_GRACE))
        if age <= interval:
            return LicenseStatus(LicenseState.ACTIVE, "", license_id, expires_at)
        if age <= grace:
            return LicenseStatus(LicenseState.GRACE, "license server not reached recently", license_id, expires_at)
        return LicenseStatus(
            LicenseState.VERIFY_REQUIRED, "license must be re-verified online", license_id, expires_at
        )

    def summary(self) -> LicenseSummary:
        st, rec = self.status(), self._record
        return LicenseSummary(
            state=st.state, detail=st.detail, enabled=st.enabled, license_id=st.license_id,
            expires_at=st.expires_at,
            last_verified_at=rec.last_verified_at if rec else None,
            last_attempt_failed=bool(rec and rec.last_error),
        )

    # ---- talking to the license server ---------------------------------------------------
    async def load(self) -> None:
        self._record = await self._store.load()

    def _require_client(self) -> LicenseClient:
        if self._client is None:
            raise LicenseError("license server is not configured", code="not_configured")
        return self._client

    async def _exchange(self, path: str, key: str) -> tuple[dict, str]:
        client = self._require_client()
        nonce = new_nonce()
        body = {
            "license_key": key,
            "product": self._product,
            "installation_id": self._installation_id,
            "host": self._host,
            "app_version": self._app_version,
            "nonce": nonce,
        }
        http_status, data = await client.call(path, body)
        if http_status == 200:
            payload, signature = data.get("payload"), data.get("signature")
            if not isinstance(payload, dict) or not isinstance(signature, str):
                raise LicenseResponseInvalid("malformed response")
            if not verify_signature(self._public_key, payload, signature):
                raise LicenseResponseInvalid("response signature is invalid")
            if payload.get("nonce") != nonce:
                raise LicenseResponseInvalid("response does not match the request")
            if (
                payload.get("product") != self._product
                or payload.get("installation_id") != self._installation_id
                or payload.get("host", "") != self._host
                or payload.get("status") not in SIGNED_STATUSES
            ):
                raise LicenseResponseInvalid("response is for a different installation")
            return payload, signature
        if 400 <= http_status < 500:
            raise LicenseRejected(str(data.get("message", "")), code=str(data.get("error", "rejected")))
        raise LicenseUnreachable(f"license server error (HTTP {http_status})")

    async def activate(self, license_key: str) -> LicenseStatus:
        try:
            key = normalize_license_key(license_key)
        except ValueError as exc:
            raise LicenseRejected("That license key is not in the expected format.", code="invalid_key") from exc
        payload, signature = await self._exchange("/v1/activate", key)
        if payload["status"] != STATUS_ACTIVE:
            raise LicenseRejected(f"license is {payload['status']}", code=payload["status"])
        now = int(self._clock())
        prev = self._record
        record = LicenseRecord(
            license_key=key,
            license_id=str(payload.get("license_id", "")),
            product=self._product,
            installation_id=self._installation_id,
            host=self._host,
            status=payload["status"],
            payload_json=canonical_json(payload).decode("ascii"),
            signature=signature,
            activated_at=now,
            last_verified_at=now,
            last_attempt_at=now,
            last_error="",
            high_water_at=max(now, prev.high_water_at if prev else 0),
        )
        await self._store.save(record)
        self._record = record
        self._max_seen = max(self._max_seen, now)
        log.info("license activated (license_id=%s)", record.license_id)
        return self.status()

    async def verify(self) -> LicenseStatus:
        rec = self._record
        if rec is None:
            raise LicenseError("no license activated", code="not_activated")
        now = int(self._clock())
        try:
            payload, signature = await self._exchange("/v1/verify", rec.license_key)
        except LicenseError as exc:
            # Unsigned/failed answers never invalidate an existing activation.
            log.warning("license verification did not complete: %s", exc.code)
            rec = rec.with_(last_attempt_at=now, last_error=exc.code)
            await self._store.save(rec)
            self._record = rec
            return self.status()

        try:
            old_issued = int(json.loads(rec.payload_json).get("issued_at", 0))
        except ValueError:
            old_issued = 0
        if int(payload.get("issued_at", 0)) < old_issued:
            log.warning("ignoring license response older than the stored one")
            return self.status()
        rec = rec.with_(
            status=payload["status"],
            payload_json=canonical_json(payload).decode("ascii"),
            signature=signature,
            last_verified_at=now,
            last_attempt_at=now,
            last_error="",
            high_water_at=max(now, rec.high_water_at),
        )
        await self._store.save(rec)
        self._record = rec
        self._max_seen = max(self._max_seen, now)
        return self.status()

    async def ensure_fresh(self) -> LicenseStatus:
        """Called at startup and periodically: reload, advance the clock mark, re-verify if due."""
        if not self._enforcement:
            return self.status()
        await self.load()
        rec = self._record
        if rec is None or self._client is None:
            return self.status()
        now = int(self._clock())
        if now > rec.high_water_at + 60 and now >= self._max_seen:
            rec = rec.with_(high_water_at=now)
            await self._store.save(rec)
            self._record = rec
        self._max_seen = max(self._max_seen, now)

        try:
            payload = json.loads(rec.payload_json)
            age = now - int(payload.get("issued_at", 0))
            interval = int(payload.get("verify_interval_seconds", DEFAULT_VERIFY_INTERVAL))
        except ValueError:
            return self.status()
        due = age >= interval // 2
        recently_tried = rec.last_attempt_at is not None and now - rec.last_attempt_at < RETRY_AFTER_FAILURE
        if due and not (recently_tried and rec.last_error):
            return await self.verify()
        return self.status()
