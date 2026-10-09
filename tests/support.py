"""Test helpers: SQLite adapters (so the SQL runs offline) and a fake license server."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "license_server"))

import signing  # noqa: E402  (license_server/signing.py)
from app.db.migrate import Migration  # noqa: E402
from app.licensing.protocol import STATUS_ACTIVE, STATUS_DEACTIVATED, STATUS_DISABLED, STATUS_EXPIRED, STATUS_REVOKED  # noqa: E402

_PLACEHOLDER = re.compile(r"\$(\d+)")

# Step 6: datetimes are bound as fixed-width UTC text so that SQLite compares them chronologically (asyncpg binds real
# timestamptz values on PostgreSQL).
sqlite3.register_adapter(datetime, lambda d: (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
                         .strftime("%Y-%m-%d %H:%M:%S.%f"))


class SqliteDb:
    """Async facade with asyncpg-style $n placeholders over an in-memory SQLite DB."""

    dialect = "sqlite"

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._lock = asyncio.Lock()
        self._in_tx = False

    def _prep(self, sql: str, args: tuple):
        order = [int(n) - 1 for n in _PLACEHOLDER.findall(sql)]
        return _PLACEHOLDER.sub("?", sql), tuple(args[i] for i in order)

    def _run(self, sql: str, args: tuple, fetch: str | None):
        sql, params = self._prep(sql, args)
        rows = self.conn.execute(sql, params).fetchall()
        if not self._in_tx:
            self.conn.commit()
        if fetch == "one":
            return rows[0] if rows else None
        return rows

    async def fetchrow(self, sql: str, *args):
        return self._run(sql, args, "one")

    async def fetch(self, sql: str, *args):
        return self._run(sql, args, "all")

    async def execute(self, sql: str, *args) -> None:
        self._run(sql, args, None)

    @contextlib.asynccontextmanager
    async def transaction(self):
        async with self._lock:  # one transaction at a time on the single connection
            self.conn.execute("BEGIN")
            self._in_tx = True
            try:
                yield self
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()
            finally:
                self._in_tx = False


class SqliteMigrationDriver:
    def __init__(self, db: SqliteDb) -> None:
        self.db = db

    async def prepare(self) -> None:
        self.db.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, "
            "name TEXT NOT NULL, checksum TEXT NOT NULL)"
        )

    async def applied(self) -> dict[str, str]:
        return {r["version"]: r["checksum"] for r in self.db.conn.execute("SELECT * FROM schema_migrations")}

    async def apply(self, m: Migration) -> None:
        try:
            self.db.conn.executescript("BEGIN;\n" + m.sql)
            self.db.conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)", (m.version, m.name, m.checksum)
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise


async def migrated_db() -> SqliteDb:
    from app.db.migrate import discover, run_migrations

    db = SqliteDb()
    await run_migrations(SqliteMigrationDriver(db), discover())
    return db


class Clock:
    def __init__(self, start: float = 1_800_000_000) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeLicenseServer:
    """Reference implementation of the seller-side rules, used to exercise the client."""

    def __init__(self, private_key: str, clock: Clock, product: str = "telegram-auto-poster") -> None:
        self.private_key = private_key
        self.clock = clock
        self.product = product
        self.licenses: dict[str, dict] = {}
        self.down = False
        self.unsigned_error: tuple[int, dict] | None = None
        self.calls: list[tuple[str, dict]] = []

    def add_license(self, key: str, *, max_activations: int = 1, expires_at=None, product=None,
                    verify_interval: int = 86400, grace: int = 7 * 86400) -> None:
        self.licenses[key] = {
            "license_id": "lic-" + hashlib.sha1(key.encode()).hexdigest()[:8],
            "status": STATUS_ACTIVE, "max": max_activations, "expires_at": expires_at,
            "product": product or self.product, "installations": {},
            "verify_interval": verify_interval, "grace": grace,
        }

    def revoke(self, key: str) -> None:
        self.licenses[key]["status"] = STATUS_REVOKED

    def reactivate(self, key: str) -> None:
        self.licenses[key]["status"] = STATUS_ACTIVE

    def release(self, key: str, installation_id: str) -> None:
        self.licenses[key]["installations"].pop(installation_id, None)

    def _payload(self, lic: dict, body: dict, status: str) -> dict:
        return {
            "v": 1, "license_id": lic["license_id"], "product": lic["product"],
            "installation_id": body["installation_id"], "host": body.get("host", ""),
            "status": status, "nonce": body["nonce"], "issued_at": int(self.clock()),
            "expires_at": lic["expires_at"], "verify_interval_seconds": lic["verify_interval"],
            "offline_grace_seconds": lic["grace"],
        }

    def handle(self, path: str, body: dict) -> tuple[int, dict]:
        self.calls.append((path, body))
        if self.down:
            raise ConnectionError("server down")
        if self.unsigned_error:
            return self.unsigned_error
        lic = self.licenses.get(body.get("license_key", ""))
        if lic is None:
            return 404, {"error": "invalid_key", "message": "Unknown license key."}
        if lic["product"] != body.get("product"):
            return 403, {"error": "product_mismatch", "message": "Key is for another product."}
        inst = body["installation_id"]
        if path == "/v1/activate":
            if lic["status"] != STATUS_ACTIVE:
                return 403, {"error": lic["status"], "message": "License disabled."}
            if inst not in lic["installations"] and len(lic["installations"]) >= lic["max"]:
                return 409, {"error": "activation_limit", "message": "Activation limit reached."}
            lic["installations"][inst] = body.get("host", "")
        elif inst not in lic["installations"]:
            return 200, signing.sign_payload(self.private_key, self._payload(lic, body, STATUS_DEACTIVATED))
        status = lic["status"]
        if lic["expires_at"] is not None and self.clock() > lic["expires_at"]:
            status = STATUS_EXPIRED
        return 200, signing.sign_payload(self.private_key, self._payload(lic, body, status))

    async def transport(self, path: str, body: dict):
        return self.handle(path, body)
