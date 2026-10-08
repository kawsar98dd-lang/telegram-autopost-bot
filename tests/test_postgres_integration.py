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

        self.assertEqual(self.first_run, ["0001_initial", "0002_auth", "0003_telegram_connect", "0004_telegram_groups", "0005_posts"])
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

    async def _group_stack(self, pool):
        """Real PostgreSQL + the real services + the fake Telegram network (no Telegram account involved)."""
        import time

        from app.config import settings_from_env
        from app.db.pool import PoolDb
        from app.security.crypto import Cipher, generate_key
        from app.telegram.connect import TelegramConnectionService
        from app.telegram.group_sync import GroupService
        from app.telegram.groups import RawChat
        from tests.fake_telegram import GOOD_API_HASH, GOOD_API_ID, FakeTelegram
        from tests.web_support import BASE_ENV

        world = FakeTelegram()
        world.add_account("+8801712345678", 111, code="48213")
        world.add_account("+8801812345678", 222, code="48213")
        world.chats[111] = [RawChat(kind="megagroup", chat_id=-1001, title="Open Group", username="open_group"),
                            RawChat(kind="megagroup", chat_id=-1002, title="Announcements", default_send_banned=True),
                            RawChat(kind="basic", chat_id=-2001, title="Basic Group")]
        world.chats[222] = [RawChat(kind="megagroup", chat_id=-1001, title="Same Chat Other Account")]
        db = PoolDb(pool)
        tg = world.service()
        connect = TelegramConnectionService(db, Cipher(generate_key()), tg, settings_from_env(BASE_ENV), time.time)
        users = {}
        for name in ("one", "two"):
            uid = uuid.uuid4()
            await pool.execute("INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'h')", uid, f"{name}@example.com")
            users[name] = str(uid)

        async def link(user, phone):
            attempt = await connect.start(user, phone, str(GOOD_API_ID), GOOD_API_HASH)
            return (await connect.submit_code(user, attempt, "48213")).account_id

        return world, db, GroupService(db, connect, tg, time.time), users, link

    async def test_groups_sync_selection_and_isolation_on_postgres(self):
        from app.db.pool import create_pool
        from app.telegram.connect import AccountNotFound
        from app.telegram.group_sync import GroupNotFound, NotSelectable

        pool = await create_pool(URL, max_size=8)
        try:
            world, db, groups, users, link = await self._group_stack(pool)
            acc1, acc2 = await link(users["one"], "+8801712345678"), await link(users["two"], "+8801812345678")
            result = await groups.sync(users["one"], acc1)
            self.assertEqual((result.total, result.postable), (3, 2))
            await groups.sync(users["one"], acc1)  # idempotent: the upsert must not duplicate anything
            await groups.sync(users["two"], acc2)
            self.assertEqual(await pool.fetchval("SELECT COUNT(*) FROM telegram_groups"), 4)
            mine = {g["title"]: g for g in await groups.groups(users["one"], acc1)}
            self.assertEqual(mine["Announcements"]["permission_status"], "no_permission")
            self.assertEqual(mine["Open Group"]["username"], "open_group")
            # selection persists across a refresh while valid, and is refused for an unpostable group
            await groups.save_selection(users["one"], acc1, {mine["Open Group"]["id"]})
            await groups.sync(users["one"], acc1)
            self.assertEqual([g["title"] for g in await groups.groups(users["one"], acc1) if g["is_enabled"]], ["Open Group"])
            with self.assertRaises(NotSelectable):
                await groups.save_selection(users["one"], acc1, {mine["Announcements"]["id"]})
            # cross-user manipulation
            theirs = (await groups.groups(users["two"], acc2))[0]["id"]
            with self.assertRaises(GroupNotFound):
                await groups.save_selection(users["one"], acc1, {theirs})
            with self.assertRaises(AccountNotFound):
                await groups.save_selection(users["two"], acc1, {mine["Open Group"]["id"]})
            # a group that disappears is flagged and unselected
            world.chats[111] = [world.chats[111][1]]
            await groups.sync(users["one"], acc1)
            gone = {g["title"]: g for g in await groups.groups(users["one"], acc1)}["Open Group"]
            self.assertEqual((gone["is_present"], gone["is_enabled"], gone["permission_status"]), (False, False, "unavailable"))
            self.assertTrue(world.assert_all_clients_closed())
        finally:
            await pool.close()

    async def test_group_constraints_indexes_and_concurrent_refresh_on_postgres(self):
        import asyncio

        from app.db.pool import create_pool

        pool = await create_pool(URL, max_size=8)
        try:
            world, db, groups, users, link = await self._group_stack(pool)
            acc1 = await link(users["one"], "+8801712345678")
            with self.assertRaises(asyncpg.ForeignKeyViolationError):  # another user + this user's account
                await pool.execute("INSERT INTO telegram_groups (id, user_id, account_id, tg_chat_id, title, chat_type) "
                                   "VALUES ($1, $2, $3, 5, 't', 'group')", uuid.uuid4(), uuid.UUID(users["two"]), uuid.UUID(acc1))
            await pool.execute("INSERT INTO telegram_groups (id, user_id, account_id, tg_chat_id, title, chat_type) "
                               "VALUES ($1, $2, $3, 5, 't', 'group')", uuid.uuid4(), uuid.UUID(users["one"]), uuid.UUID(acc1))
            with self.assertRaises(asyncpg.UniqueViolationError):
                await pool.execute("INSERT INTO telegram_groups (id, user_id, account_id, tg_chat_id, title, chat_type) "
                                   "VALUES ($1, $2, $3, 5, 't2', 'group')", uuid.uuid4(), uuid.UUID(users["one"]), uuid.UUID(acc1))
            with self.assertRaises(asyncpg.CheckViolationError):
                await pool.execute("UPDATE telegram_groups SET permission_status = 'whatever'")
            names = {r["indexname"] for r in await pool.fetch("SELECT indexname FROM pg_indexes WHERE tablename = 'telegram_groups'")}
            self.assertLessEqual({"telegram_groups_selected_idx", "telegram_groups_account_idx"}, names)
            await pool.execute("DELETE FROM telegram_groups")
            outcomes = await asyncio.gather(groups.sync(users["one"], acc1), groups.sync(users["one"], acc1), return_exceptions=True)
            self.assertTrue(all(not isinstance(o, Exception) for o in outcomes), outcomes)
            self.assertEqual(await pool.fetchval("SELECT COUNT(*) FROM telegram_groups"), 3)  # two simultaneous refreshes, no duplicates
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

    async def test_posts_media_targets_and_isolation_on_postgres(self):
        from app.config import settings_from_env
        from app.db.pool import PoolDb, create_pool
        from app.posts.service import PostNotFound, PostService, Upload
        from app.posts.storage import DatabaseMediaStorage, new_key
        from app.telegram.group_sync import GroupNotFound, NotSelectable
        from tests.posts_support import png
        from tests.web_support import BASE_ENV

        u1, u2, a1, a2, g1, g2, g_bad = (str(uuid.uuid4()) for _ in range(7))
        conn = await asyncpg.connect(URL)
        try:
            for u, email in ((u1, "p1@example.com"), (u2, "p2@example.com")):
                await conn.execute("INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'h')", u, email)
            for a, u, n in ((a1, u1, 1), (a2, u2, 2)):
                await conn.execute("INSERT INTO telegram_accounts (id, user_id, tg_user_id) VALUES ($1, $2, $3)", a, u, n)
            for g, u, a, chat, status in ((g1, u1, a1, 10, "ok"), (g2, u2, a2, 20, "ok"), (g_bad, u1, a1, 11, "no_permission")):
                await conn.execute("INSERT INTO telegram_groups (id, user_id, account_id, tg_chat_id, title, chat_type, permission_status) "
                                   "VALUES ($1, $2, $3, $4, 'G', 'supergroup', $5)", g, u, a, chat, status)

            class Connect:  # the real connection service is not needed here: only the account listing is used
                async def list_accounts(self, user_id):
                    rows = await conn.fetch("SELECT id, status FROM telegram_accounts WHERE user_id = $1", user_id)
                    return [{"id": str(r["id"]), "status": r["status"]} for r in rows]

            pool = await create_pool(URL, max_size=3)
            try:
                db = PoolDb(pool)
                settings = settings_from_env(BASE_ENV)
                svc = PostService(db, Connect(), DatabaseMediaStorage(db, lambda: 1_800_000_000.0), settings, lambda: 1_800_000_000.0)
                post_id = await svc.create(u1, a1, "Title", "Hello\nworld", {g1}, Upload("a.png", png()))
                post = await svc.get(u1, post_id)
                self.assertEqual((post["status"], post["account_id"], len(post["targets"])), ("draft", a1, 1))
                self.assertEqual(await svc.image(u1, post_id), ("image/png", png()))  # BYTEA round trip
                data = await svc.preview(u1, post_id)
                self.assertEqual(data["message"].text.count("Auto Posted by"), 1)
                with self.assertRaises(PostNotFound):
                    await svc.get(u2, post_id)
                with self.assertRaises(PostNotFound):
                    await svc.image(u2, post_id)
                with self.assertRaises(PostNotFound):
                    await svc.create(u2, a1, "", "x", set(), None)
                with self.assertRaises(GroupNotFound):
                    await svc.create(u1, a1, "", "x", {g2}, None)
                with self.assertRaises(NotSelectable):
                    await svc.create(u1, a1, "", "x", {g_bad}, None)
                await svc.update(u1, post_id, "T2", "changed", set(), None, True)
                self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM post_media"), 0)
                self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM media_blobs"), 0)
                # database-level isolation
                with self.assertRaises(asyncpg.ForeignKeyViolationError):
                    await conn.execute("INSERT INTO post_targets (post_id, group_id, account_id, user_id) VALUES ($1, $2, $3, $4)",
                                       post_id, g2, a1, u1)
                with self.assertRaises(asyncpg.ForeignKeyViolationError):
                    await conn.execute("INSERT INTO post_targets (post_id, group_id, account_id, user_id) VALUES ($1, $2, $3, $4)",
                                       post_id, g2, a2, u1)
                with self.assertRaises(asyncpg.CheckViolationError):
                    await conn.execute("UPDATE posts SET status = 'banana' WHERE id = $1", post_id)
                key = new_key()
                await DatabaseMediaStorage(db, lambda: 5.0).put(key, b"orphan")
                self.assertEqual(await svc.purge_orphans(), 1)
                await svc.delete(u1, post_id)
                self.assertEqual(await conn.fetchval("SELECT COUNT(*) FROM posts"), 0)
            finally:
                await pool.close()
        finally:
            await conn.close()


if __name__ == "__main__":
    unittest.main()
