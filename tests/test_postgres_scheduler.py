"""Step 6 queue behaviour against a REAL PostgreSQL (real FOR UPDATE SKIP LOCKED contention, real constraints).

Skipped unless TEST_DATABASE_URL is set and asyncpg is installed; CI sets both and fails on a skip (scripts/ci_verify.py).
WARNING: drops and recreates the public schema of that database.
"""

import asyncio
import json
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import tests.support  # noqa: F401
from tests.test_postgres_integration import AVAILABLE, URL, asyncpg


@unittest.skipUnless(AVAILABLE, "set TEST_DATABASE_URL and install asyncpg to run (environment limitation)")
class SchedulerPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db.migrate import AsyncpgDriver, MigrationError, discover, run_migrations  # noqa: F401
        from app.db.pool import PoolDb, create_pool

        conn = await asyncpg.connect(URL)
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await conn.close()
        self.old_migrations = [m for m in discover() if m.version <= "0005"]
        conn = await asyncpg.connect(URL, statement_cache_size=0)
        await run_migrations(AsyncpgDriver(conn), self.old_migrations)   # a database exactly as Step 5 left it
        await conn.close()
        self.pool = await create_pool(URL, max_size=12)
        self.db = PoolDb(self.pool)

    async def asyncTearDown(self):
        await self.pool.close()

    async def seed(self, accounts=1, groups=2):
        """Users, accounts, groups, a post and a due once-schedule per account, written BEFORE migration 0006 existed."""
        out = []
        for i in range(accounts):
            u, acc, post, sid = (uuid.uuid4() for _ in range(4))
            await self.pool.execute("INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'h')", u, f"u{i}@example.com")
            await self.pool.execute("INSERT INTO telegram_accounts (id, user_id, tg_user_id) VALUES ($1, $2, $3)", acc, u, 100 + i)
            await self.pool.execute("INSERT INTO posts (id, user_id, account_id, body, status) VALUES ($1, $2, $3, 'hello', 'scheduled')",
                                    post, u, acc)
            await self.pool.execute(
                "INSERT INTO schedules (id, user_id, post_id, account_id, kind, timezone, run_at, next_run_at) "
                "VALUES ($1, $2, $3, $4, 'once', 'UTC', $5, $5)", sid, u, post, acc, datetime.now(timezone.utc) - timedelta(minutes=2))
            gids = []
            for g in range(groups):
                gid = uuid.uuid4()
                gids.append(gid)
                await self.pool.execute(
                    "INSERT INTO telegram_groups (id, user_id, account_id, tg_chat_id, title, chat_type, permission_status) "
                    "VALUES ($1, $2, $3, $4, $5, 'supergroup', 'ok')", gid, u, acc, -1000 - g, f"G{i}-{g}")
                await self.pool.execute("INSERT INTO schedule_targets (schedule_id, group_id, user_id) VALUES ($1, $2, $3)", sid, gid, u)
            out.append((u, acc, post, sid, gids))
        return out

    async def migrate_to_latest(self):
        from app.db.migrate import ensure_schema
        return await ensure_schema(URL)

    async def test_migration_0006_applies_to_an_existing_database_and_keeps_data(self):
        seeded = await self.seed()
        applied = await self.migrate_to_latest()
        self.assertEqual(applied, ["0006_scheduler"])
        self.assertEqual(await self.migrate_to_latest(), [])
        u, acc, post, sid, gids = seeded[0]
        row = await self.pool.fetchrow("SELECT status, cancelled_at, kind FROM schedules WHERE id = $1", sid)
        self.assertEqual((row["status"], row["cancelled_at"], row["kind"]), ("active", None, "once"))
        self.assertEqual(await self.pool.fetchval("SELECT COUNT(*) FROM telegram_groups WHERE account_id = $1", acc), 2)
        self.assertIsNone(await self.pool.fetchval("SELECT send_locked_by FROM telegram_accounts WHERE id = $1", acc))
        self.assertEqual(await self.pool.fetchval("SELECT status FROM posts WHERE id = $1", post), "scheduled")

    async def test_constraints_and_isolation_of_the_new_columns(self):
        (u, acc, post, sid, gids), = await self.seed()
        await self.migrate_to_latest()
        base = ("INSERT INTO posting_jobs (id, user_id, account_id, group_title, text_snapshot, scheduled_for, next_attempt_at, "
                "idempotency_key, delivery_state) VALUES ($1, $2, $3, 'G', 't', now(), now(), $4, $5)")
        await self.pool.execute(base, uuid.uuid4(), u, acc, "k1", "uncertain")
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.pool.execute(base, uuid.uuid4(), u, acc, "k2", "bogus")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.pool.execute(base, uuid.uuid4(), u, acc, "k1", "not_sent")      # duplicate occurrence
        with self.assertRaises(asyncpg.ForeignKeyViolationError):                       # job for a post that does not exist
            await self.pool.execute("UPDATE posting_jobs SET post_id = $1 WHERE idempotency_key = 'k1'", uuid.uuid4())

    async def test_concurrent_materialisation_creates_each_job_exactly_once(self):
        (u, acc, post, sid, gids), = await self.seed(groups=3)
        await self.migrate_to_latest()
        from app.scheduler.queue import JobQueue

        queues = [JobQueue(self.db, time.time) for _ in range(8)]
        results = await asyncio.gather(*[q.materialize_due() for q in queues])
        self.assertEqual(sum(results), 1)                                               # one worker created the occurrence
        self.assertEqual(await self.pool.fetchval("SELECT COUNT(*) FROM posting_jobs WHERE schedule_id = $1", sid), 3)
        self.assertEqual(await self.pool.fetchval("SELECT COUNT(DISTINCT idempotency_key) FROM posting_jobs"), 3)
        row = await self.pool.fetchrow("SELECT status, next_run_at FROM schedules WHERE id = $1", sid)
        self.assertEqual((row["status"], row["next_run_at"]), ("completed", None))
        self.assertEqual(await self.pool.fetchval("SELECT status FROM posts WHERE id = $1", post), "sending")

    async def test_concurrent_claims_never_hand_out_a_job_twice_and_serialise_per_account(self):
        seeded = await self.seed(accounts=3, groups=4)
        await self.migrate_to_latest()
        from app.scheduler.queue import JobQueue

        await JobQueue(self.db, time.time).materialize_due()
        self.assertEqual(await self.pool.fetchval("SELECT COUNT(*) FROM posting_jobs"), 12)
        queues = [JobQueue(self.db, time.time) for _ in range(10)]
        claims = await asyncio.gather(*[q.claim_account_batch(f"w{i}", batch=3) for i, q in enumerate(queues)])
        claims = [c for c in claims if c is not None]
        ids = [j.id for _, jobs in claims for j in jobs]
        self.assertEqual(len(ids), len(set(ids)))                                       # no job claimed twice
        self.assertEqual(len({acc for acc, _ in claims}), len(claims))                  # one worker per account at a time
        self.assertLessEqual(len(claims), 3)
        self.assertEqual(await self.pool.fetchval("SELECT COUNT(*) FROM posting_jobs WHERE status = 'processing'"), len(ids))
        self.assertEqual(await self.pool.fetchval("SELECT COUNT(*) FROM posting_jobs WHERE attempts = 1"), len(ids))

    async def test_recovery_of_expired_leases_on_postgres(self):
        (u, acc, post, sid, gids), = await self.seed(groups=2)
        await self.migrate_to_latest()
        from app.scheduler.queue import JobQueue

        q = JobQueue(self.db, time.time)
        await q.materialize_due()
        _, jobs = await q.claim_account_batch("dead", lease=1)
        self.assertTrue(await q.begin_send(jobs[0], "dead"))       # first job: send started, second: only claimed
        await self.pool.execute("UPDATE posting_jobs SET lease_expires_at = now() - interval '1 second'")
        stats = await q.recover_stale()
        self.assertEqual((stats["uncertain"], stats["requeued"]), (1, 1))
        states = sorted(r["delivery_state"] + ":" + r["status"] for r in await self.pool.fetch("SELECT * FROM posting_jobs"))
        self.assertEqual(states, ["not_sent:scheduled", "uncertain:failed"])
        self.assertIsNone(await self.pool.fetchval("SELECT send_locked_by FROM telegram_accounts WHERE id = $1", acc))
        log = await self.pool.fetch("SELECT details FROM posting_logs WHERE event = 'uncertain'")
        self.assertEqual(len(log), 1)
        self.assertNotIn("text", json.loads(log[0]["details"]))                          # audit rows hold ids and codes only


if __name__ == "__main__":
    unittest.main()
