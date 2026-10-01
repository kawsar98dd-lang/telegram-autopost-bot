"""Persistence for the local license activation (encrypted license key)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any, Protocol

from ..security.crypto import Cipher

_CONTEXT = "license_key"


@dataclass(frozen=True)
class LicenseRecord:
    license_key: str
    license_id: str
    product: str
    installation_id: str
    host: str
    status: str
    payload_json: str
    signature: str
    activated_at: int
    last_verified_at: int
    last_attempt_at: int | None
    last_error: str
    high_water_at: int

    def with_(self, **changes: Any) -> "LicenseRecord":
        return replace(self, **changes)


class LicenseStore(Protocol):
    async def load(self) -> LicenseRecord | None: ...
    async def save(self, record: LicenseRecord) -> None: ...


class MemoryLicenseStore:
    def __init__(self) -> None:
        self.record: LicenseRecord | None = None

    async def load(self) -> LicenseRecord | None:
        return self.record

    async def save(self, record: LicenseRecord) -> None:
        self.record = record


class DbLicenseStore:
    """Stores the record in ``license_activation`` (see migrations/0001_initial.sql)."""

    def __init__(self, db: Any, cipher: Cipher) -> None:
        self._db = db
        self._cipher = cipher

    async def load(self) -> LicenseRecord | None:
        row = await self._db.fetchrow("SELECT * FROM license_activation WHERE id = 1")
        if row is None:
            return None
        return LicenseRecord(
            license_key=self._cipher.decrypt(row["license_key_enc"], _CONTEXT),
            license_id=row["license_id"],
            product=row["product"],
            installation_id=row["installation_id"],
            host=row["host"],
            status=row["status"],
            payload_json=row["payload_json"],
            signature=row["signature"],
            activated_at=row["activated_at"],
            last_verified_at=row["last_verified_at"],
            last_attempt_at=row["last_attempt_at"],
            last_error=row["last_error"] or "",
            high_water_at=row["high_water_at"],
        )

    async def save(self, r: LicenseRecord) -> None:
        await self._db.execute(
            """
            INSERT INTO license_activation
              (id, license_key_enc, license_id, product, installation_id, host, status,
               payload_json, signature, activated_at, last_verified_at, last_attempt_at,
               last_error, high_water_at, updated_at)
            VALUES (1, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (id) DO UPDATE SET
              license_key_enc = EXCLUDED.license_key_enc, license_id = EXCLUDED.license_id,
              product = EXCLUDED.product, installation_id = EXCLUDED.installation_id,
              host = EXCLUDED.host, status = EXCLUDED.status,
              payload_json = EXCLUDED.payload_json, signature = EXCLUDED.signature,
              activated_at = EXCLUDED.activated_at, last_verified_at = EXCLUDED.last_verified_at,
              last_attempt_at = EXCLUDED.last_attempt_at, last_error = EXCLUDED.last_error,
              high_water_at = EXCLUDED.high_water_at, updated_at = EXCLUDED.updated_at
            """,
            self._cipher.encrypt(r.license_key, _CONTEXT),
            r.license_id,
            r.product,
            r.installation_id,
            r.host,
            r.status,
            r.payload_json,
            r.signature,
            r.activated_at,
            r.last_verified_at,
            r.last_attempt_at,
            r.last_error,
            r.high_water_at,
            max(r.last_verified_at, r.high_water_at),
        )


async def get_or_create_installation_id(db: Any) -> str:
    """Random per-installation id, generated once and kept in ``app_state``."""
    row = await db.fetchrow("SELECT value FROM app_state WHERE key = 'installation_id'")
    if row is not None:
        return row["value"]
    await db.execute(
        "INSERT INTO app_state (key, value) VALUES ('installation_id', $1) ON CONFLICT (key) DO NOTHING",
        str(uuid.uuid4()),
    )
    row = await db.fetchrow("SELECT value FROM app_state WHERE key = 'installation_id'")
    return row["value"]
