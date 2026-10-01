import asyncio
import unittest

import tests.support  # noqa: F401
from tests.support import Clock, migrated_db
from app.auth import csrf
from app.auth.permissions import Principal
from app.auth.ratelimit import RateLimiter, key_part
from app.auth.redirects import safe_next
from app.auth.sessions import SessionService, hash_token
from app.auth.users import SetupAlreadyDone, UserService, is_valid_email, normalize_email
from app.security.password_policy import password_problems
from app.security.passwords import hash_password, verify_password


class PasswordPolicyTests(unittest.TestCase):
    def test_accepts_strong_passwords(self):
        for pw in ("correct-horse-battery", "Tr0ub4dor&3-long-enough", "পাসওয়ার্ড-বাংলা-১২৩৪৫"):
            self.assertEqual(password_problems(pw, "me@example.com"), [], pw)

    def test_rejects_weak_passwords(self):
        cases = {
            "short1!": "at least 12",
            "x" * 129: "at most",
            "password123": "at least 12",  # too short
            "aaaaaaaaaaaaaa": "variety",
            "123456789012": "too common",
            "1029384756473": "digits",
            "johnsmith-secret": "email name",
        }
        for pw, needle in cases.items():
            problems = password_problems(pw, "johnsmith@example.com")
            if needle:
                self.assertTrue(any(needle in p for p in problems), (pw, problems))

    def test_hashing_is_modern_and_salted(self):
        h = hash_password("correct-horse-battery")
        self.assertTrue(h.startswith("scrypt$32768$8$1$"))  # memory-hard KDF, N=2^15
        self.assertTrue(verify_password("correct-horse-battery", h))
        self.assertFalse(verify_password("correct-horse-batterx", h))


class RedirectTests(unittest.TestCase):
    def test_safe_targets_pass(self):
        for value in ("/", "/account", "/groups?x=1", "/a/b#frag"):
            self.assertEqual(safe_next(value), value)

    def test_dangerous_targets_fall_back(self):
        for value in ("https://evil.com", "//evil.com", "/\\evil.com", "javascript:alert(1)", "http://x", "evil.com",
                      "/ok\r\nSet-Cookie: a=b", "/login", "/logout", "/setup?x", "", None, "/" + "a" * 400,
                      "///evil.com", "/\t/evil.com", "data:text/html,x"):
            self.assertEqual(safe_next(value), "/", repr(value))


class CsrfUnitTests(unittest.TestCase):
    def test_token_binding(self):
        secret = "s" * 40
        binding = csrf.new_binding()
        token = csrf.token_for(secret, binding)
        self.assertTrue(csrf.verify(secret, binding, token))
        self.assertFalse(csrf.verify(secret, csrf.new_binding(), token))
        self.assertFalse(csrf.verify("t" * 40, binding, token))
        self.assertFalse(csrf.verify(secret, binding, ""))
        self.assertFalse(csrf.verify(secret, "", token))

    def test_origin_rules(self):
        app = "https://poster.example.com"
        self.assertTrue(csrf.origin_ok(app, "https://poster.example.com", None))
        self.assertTrue(csrf.origin_ok(app, None, "https://poster.example.com/login"))
        self.assertTrue(csrf.origin_ok(app, None, None))
        for origin in ("https://evil.example.com", "null", "http://poster.example.com", "https://poster.example.com.evil.io"):
            self.assertFalse(csrf.origin_ok(app, origin, None), origin)
        self.assertFalse(csrf.origin_ok(app, None, "https://poster.example.com.evil.io/x"))


class PermissionTests(unittest.TestCase):
    def test_roles(self):
        admin = Principal("1", "a@b.co", "", True)
        user = Principal("2", "c@d.co", "", False)
        self.assertTrue(admin.can("anything.at.all"))
        self.assertTrue(user.can("dashboard.view"))
        self.assertFalse(user.can("license.manage"))
        self.assertEqual((admin.role, user.role), ("admin", "user"))


class EmailTests(unittest.TestCase):
    def test_email(self):
        self.assertEqual(normalize_email("  A@Example.COM "), "a@example.com")
        for good in ("a@example.com", "first.last+tag@sub.example.org"):
            self.assertTrue(is_valid_email(good))
        for bad in ("", "a", "a@b", "a b@example.com", "@example.com", "a@@example.com", "a@example.c", "x" * 250 + "@e.com"):
            self.assertFalse(is_valid_email(bad), bad)


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_window_and_reset(self):
        db = await migrated_db()
        rl = RateLimiter(db)
        for _ in range(3):
            self.assertEqual(await rl.retry_after("k", 3, 100, 1000), 0)
            await rl.record("k", 100, 1000)
        self.assertEqual(await rl.retry_after("k", 3, 100, 1010), 90)
        self.assertEqual(await rl.retry_after("k", 3, 100, 1100), 0)  # window over
        await rl.record("k", 100, 1101)  # new window starts, counter restarts
        self.assertEqual(db.conn.execute("SELECT count FROM rate_limits").fetchone()[0], 1)
        await rl.reset("k")
        self.assertEqual(await rl.retry_after("k", 1, 100, 1102), 0)

    async def test_keys_do_not_contain_personal_data(self):
        self.assertNotIn("example.com", key_part("someone@example.com"))
        self.assertNotEqual(key_part("a"), key_part("b"))


class SessionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await migrated_db()
        self.clock = Clock()
        self.svc = SessionService(self.db, self.clock, idle_seconds=600, absolute_seconds=3600)
        self.db.conn.execute("INSERT INTO users (id, email, password_hash, is_admin) VALUES ('u1','a@example.com','h',1)")
        self.db.conn.execute("INSERT INTO users (id, email, password_hash) VALUES ('u2','b@example.com','h')")
        self.db.conn.commit()

    async def test_only_hash_is_stored(self):
        token = await self.svc.create("u1", "UA")
        stored = self.db.conn.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
        self.assertEqual(stored, hash_token(token))
        self.assertNotIn(token, stored)
        self.assertGreaterEqual(len(token), 40)

    async def test_lookup_idle_and_absolute_expiry(self):
        token = await self.svc.create("u1")
        self.assertIsNotNone(await self.svc.lookup(token))
        for _ in range(7):  # activity every 500s keeps the idle timer (600s) from firing
            self.clock.advance(500)
            self.assertIsNotNone(await self.svc.lookup(token))
        # 3500s since creation: still inside the absolute lifetime (3600s)
        self.clock.advance(200)  # 3700s since creation, but only 200s idle
        self.assertIsNone(await self.svc.lookup(token))  # absolute lifetime wins over activity
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)

    async def test_idle_expiry(self):
        token = await self.svc.create("u1")
        self.clock.advance(601)
        self.assertIsNone(await self.svc.lookup(token))

    async def test_garbage_tokens(self):
        for bad in ("", "x", "a" * 500, "' OR 1=1 --"):
            self.assertIsNone(await self.svc.lookup(bad))

    async def test_destroy_others_only_touches_own_user(self):
        a1, a2, b1 = await self.svc.create("u1"), await self.svc.create("u1"), await self.svc.create("u2")
        await self.svc.destroy_others("u1", a1)
        self.assertIsNotNone(await self.svc.lookup(a1))
        self.assertIsNone(await self.svc.lookup(a2))
        self.assertIsNotNone(await self.svc.lookup(b1))
        self.assertEqual(len(await self.svc.list_for_user("u2")), 1)

    async def test_inactive_user_session_dies(self):
        token = await self.svc.create("u2")
        self.db.conn.execute("UPDATE users SET is_active = 0 WHERE id = 'u2'")
        self.db.conn.commit()
        self.assertIsNone(await self.svc.lookup(token))

    async def test_purge(self):
        await self.svc.create("u1")
        self.clock.advance(4000)
        await self.svc.purge_expired()
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)


class FirstRunClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_database_is_not_set_up(self):
        self.assertFalse(await UserService(await migrated_db()).is_setup_complete())

    async def test_exactly_one_of_many_simultaneous_claims_wins(self):
        db = await migrated_db()
        users = UserService(db)
        results = await asyncio.gather(
            *[users.create_initial_admin(f"admin{i}@example.com", "hash", "") for i in range(8)], return_exceptions=True)
        wins = [r for r in results if isinstance(r, str)]
        losses = [r for r in results if isinstance(r, SetupAlreadyDone)]
        self.assertEqual((len(wins), len(losses)), (1, 7))
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        self.assertEqual(db.conn.execute("SELECT is_admin FROM users").fetchone()[0], 1)
        self.assertTrue(await users.is_setup_complete())

    async def test_failed_creation_releases_the_claim(self):
        db = await migrated_db()
        users = UserService(db)
        with self.assertRaises(Exception):
            await users.create_initial_admin("a@example.com", None, "")  # NOT NULL violation -> rollback
        self.assertFalse(await users.is_setup_complete())
        await users.create_initial_admin("a@example.com", "hash", "")  # the slot is free again

    async def test_existing_user_without_claim_row_blocks_setup(self):
        db = await migrated_db()
        db.conn.execute("INSERT INTO users (id, email, password_hash) VALUES ('u','a@example.com','h')")
        db.conn.commit()
        users = UserService(db)
        self.assertTrue(await users.is_setup_complete())
        with self.assertRaises(SetupAlreadyDone):
            await users.create_initial_admin("b@example.com", "hash", "")
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)


class TimeFilterTests(unittest.TestCase):
    def test_accepts_every_representation_the_databases_return(self):
        from datetime import datetime, timezone

        from app.web.context import _fmt_time

        self.assertEqual(_fmt_time(1_800_000_000), "2027-01-15 08:00 UTC")
        self.assertEqual(_fmt_time("1800000000"), "2027-01-15 08:00 UTC")
        self.assertEqual(_fmt_time(datetime(2026, 9, 30, 14, 51, tzinfo=timezone.utc)), "2026-09-30 14:51 UTC")  # asyncpg
        self.assertEqual(_fmt_time("2026-09-30 14:51:49"), "2026-09-30 14:51 UTC")  # SQLite
        for empty in (None, "", 0, "garbage"):
            self.assertEqual(_fmt_time(empty), "-")


if __name__ == "__main__":
    unittest.main()
