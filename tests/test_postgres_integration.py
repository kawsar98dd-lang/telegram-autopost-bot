"""Runs against a REAL PostgreSQL. Skipped unless TEST_DATABASE_URL is set (the CI workflow sets it).

    TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/test python -m unittest tests.test_postgres_integration

WARNING: drops and recreates the public schema of that database.
"""

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import tests.support  # noqa: F401
from app.security.crypto import Cipher, generate_key

URL = os.environ.get("TEST_DATABASE_URL")
try:
    import asyncpg
except ImportError:  # the offline sandbox has no asyncpg
    asyncpg = None


REQUIRED = bool(os.environ.get("REQUIRE_POSTGRES_TESTS"))  # CI sets this: a skip becomes a failure
AVAILABLE = bool(URL and asyncpg)


class RequirePostgres(unittest.TestCase):
    def test_postgres_is_available_when_required(self):
        if REQUIRED and not AVAILABLE:
            self.fail("REQUIRE_POSTGRES_TESTS is set but TEST_DATABASE_URL / asyncpg is missing")


@unittest.skipUnless(AVAILABLE, "set TEST_DATABASE_URL and install asyncpg to run (environment limitation)")
class PostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db.migrate import ensure_schema

        conn = await asyncpg.connect(URL)
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await conn.close()
        self.first_run = await ensure_schema(URL)

    async def test_migrations_apply_once(self):
        from app.db.migrate import ensure_schema

        self.assertEqual(self.first_run, ["0001_initial", "0002_auth", "0003_telegram_connect"])
        self.assertEqual(await ensure_schema(URL), [])

    async def test_skip_locked_queue_claim_and_isolation_constraints(self):
        conn = await asyncpg.connect(URL)
        u1, u2, acc1, acc2, post = (uuid.uuid4() for _ in range(5))
        now = datetime.now(timezone.utc)
        for u, email in ((u1, "a@example.com"), (u2, "b@example.com")):
            await conn.execute("INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'h')", u, email)
        await conn.execute("INSERT INTO telegram_accounts (id, user_id, tg_user_id) VALUES ($1, $2, 1)", acc1, u1)
        await conn.execute("INSERT INTO telegram_accounts (id, user_id, tg_user_id) VALUES ($1, $2, 2)", acc2, u2)
        await conn.execute("INSERT INTO posts (id, user_id, body) VALUES ($1, $2, 'x')", post, u1)
        with self.assertRaises(asyncpg.ForeignKeyViolationError):  # user 1's post, user 2's account
            await conn.execute(
                "INSERT INTO schedules (id, user_id, post_id, account_id, kind, timezone) "
                "VALUES ($1, $2, $3, $4, 'once', 'UTC')", uuid.uuid4(), u1, post, acc2)
        await conn.execute(
            "INSERT INTO posting_jobs (id, user_id, account_id, group_title, text_snapshot, scheduled_for, "
            "next_attempt_at, idempotency_key) VALUES ($1, $2, $3, 'G', 't', $4, $4, 'k1')",
            uuid.uuid4(), u1, acc1, now - timedelta(minutes=1))

        claim = ("SELECT id FROM posting_jobs WHERE status IN ('scheduled','waiting') AND next_attempt_at <= $1 "
                 "ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 1")
        other = await asyncpg.connect(URL)
        tx1, tx2 = conn.transaction(), other.transaction()
        await tx1.start()
        await tx2.start()
        try:
            self.assertIsNotNone(await conn.fetchrow(claim, now))
            self.assertIsNone(await other.fetchrow(claim, now))  # already locked by the first worker
        finally:
            await tx1.rollback()
            await tx2.rollback()
            await other.close()
            await conn.close()

    async def test_first_run_claim_is_race_free_on_postgres(self):
        import asyncio

        from app.auth.users import SetupAlreadyDone, UserService
        from app.db.pool import PoolDb, create_pool

        pool = await create_pool(URL, max_size=8)
        try:
            users = UserService(PoolDb(pool))
            results = await asyncio.gather(
                *[users.create_initial_admin(f"admin{i}@example.com", "hash", "") for i in range(8)],
                return_exceptions=True)
            self.assertEqual(sum(isinstance(r, str) for r in results), 1)
            self.assertEqual(sum(isinstance(r, SetupAlreadyDone) for r in results), 7)
            self.assertEqual(await pool.fetchval("SELECT COUNT(*) FROM users"), 1)
        finally:
            await pool.close()

    async def test_sessions_and_rate_limits_on_postgres(self):
        import time

        from app.auth.ratelimit import RateLimiter
        from app.auth.sessions import SessionService
        from app.db.pool import PoolDb, create_pool

        pool = await create_pool(URL, max_size=2)
        try:
            db = PoolDb(pool)
            uid = uuid.uuid4()
            await pool.execute("INSERT INTO users (id, email, password_hash, is_admin) VALUES ($1, 'p@example.com', 'h', TRUE)", uid)
            svc = SessionService(db, time.time, 600, 3600)
            token = await svc.create(str(uid), "UA")
            info = await svc.lookup(token)
            self.assertEqual(info.principal.email, "p@example.com")
            await svc.destroy(token)
            self.assertIsNone(await svc.lookup(token))
            limiter, now = RateLimiter(db), int(time.time())
            for _ in range(3):
                await limiter.record("k", 100, now)
            self.assertEqual(await limiter.retry_after("k", 3, 100, now + 10), 90)
            await limiter.record("k", 100, now + 200)  # new window
            self.assertEqual(await limiter.retry_after("k", 3, 100, now + 201), 0)
        finally:
            await pool.close()

    async def test_license_store_and_installation_id_on_postgres(self):
        from app.db.pool import PoolDb, create_pool
        from app.licensing.store import DbLicenseStore, LicenseRecord, get_or_create_installation_id

        pool = await create_pool(URL, max_size=2)
        try:
            db = PoolDb(pool)
            store = DbLicenseStore(db, Cipher(generate_key()))
            self.assertIsNone(await store.load())
            record = LicenseRecord("TAP-AAAAA-BBBBB-CCCCC-DDDDD", "lic-1", "telegram-auto-poster", "inst",
                                   "h.example.com", "active", '{"a":1}', "sig", 100, 100, None, "", 100)
            await store.save(record)
            await store.save(record.with_(last_error="unreachable", last_attempt_at=200))
            loaded = await store.load()
            self.assertEqual((loaded.license_key, loaded.last_error, loaded.last_attempt_at),
                             ("TAP-AAAAA-BBBBB-CCCCC-DDDDD", "unreachable", 200))
            first = await get_or_create_installation_id(db)
            self.assertEqual(first, await get_or_create_installation_id(db))
        finally:
            await pool.close()


if __name__ == "__main__":
    unittest.main()
