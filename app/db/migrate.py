"""Tiny forward-only SQL migration runner.

Migrations are plain ``migrations/NNNN_name.sql`` files. Applied versions and
their checksums are recorded in ``schema_migrations``; editing an already
applied file is refused. On PostgreSQL an advisory lock makes concurrent
start-up of the web and worker processes safe.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LOCK_ID = 727_274_001


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise MigrationError(f"bad migration file name: {path.name}")
        found.append(Migration(match.group(1), match.group(2), path.read_text(encoding="utf-8")))
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise MigrationError("duplicate migration version numbers")
    return found


class MigrationDriver(Protocol):
    async def prepare(self) -> None: ...
    async def applied(self) -> dict[str, str]: ...
    async def apply(self, migration: Migration) -> None: ...


async def run_migrations(driver: MigrationDriver, migrations: list[Migration]) -> list[str]:
    await driver.prepare()
    applied = await driver.applied()
    done: list[str] = []
    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is not None:
            if recorded != migration.checksum:
                raise MigrationError(
                    f"migration {migration.version}_{migration.name} was modified after it was applied"
                )
            continue
        await driver.apply(migration)
        done.append(f"{migration.version}_{migration.name}")
    return done


class AsyncpgDriver:
    """PostgreSQL driver (asyncpg is imported lazily)."""

    def __init__(self, conn) -> None:
        self._conn = conn

    async def prepare(self) -> None:
        await self._conn.execute("SELECT pg_advisory_lock($1)", _LOCK_ID)
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )

    async def applied(self) -> dict[str, str]:
        rows = await self._conn.fetch("SELECT version, checksum FROM schema_migrations")
        return {r["version"]: r["checksum"] for r in rows}

    async def apply(self, migration: Migration) -> None:
        async with self._conn.transaction():
            await self._conn.execute(migration.sql)
            await self._conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
                migration.version,
                migration.name,
                migration.checksum,
            )

    async def release(self) -> None:
        await self._conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_ID)


async def ensure_schema(database_url: str) -> list[str]:
    import asyncpg

    conn = await asyncpg.connect(database_url, statement_cache_size=0)
    driver = AsyncpgDriver(conn)
    try:
        return await run_migrations(driver, discover())
    finally:
        try:
            await driver.release()
        finally:
            await conn.close()


def main() -> int:
    from ..config import ConfigError, load_settings

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    applied = asyncio.run(ensure_schema(settings.database_url))
    print("Applied: " + (", ".join(applied) if applied else "nothing (already up to date)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
