"""PostgreSQL access (asyncpg). Queries use $1, $2 ... placeholders."""

from __future__ import annotations

import contextlib


async def create_pool(database_url: str, max_size: int = 10):
    import asyncpg

    # statement_cache_size=0 keeps this compatible with connection poolers
    # (Supabase / PgBouncer transaction mode).
    return await asyncpg.create_pool(
        database_url, min_size=1, max_size=max_size, statement_cache_size=0, command_timeout=30
    )


class PoolDb:
    """Minimal database facade used by the license store (and later services)."""

    dialect = "postgres"  # lets the job queue add FOR UPDATE SKIP LOCKED (the offline SQLite test database has none)

    def __init__(self, pool) -> None:
        self._pool = pool

    async def fetchrow(self, sql: str, *args):
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args):
        async with self._pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def execute(self, sql: str, *args) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *args)

    @contextlib.asynccontextmanager
    async def transaction(self):
        """All statements on the yielded object run in one transaction."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield _Tx(conn)

    async def close(self) -> None:
        await self._pool.close()


class _Tx:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def fetchrow(self, sql: str, *args):
        return await self._conn.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args):
        return await self._conn.fetch(sql, *args)

    async def execute(self, sql: str, *args) -> None:
        await self._conn.execute(sql, *args)
