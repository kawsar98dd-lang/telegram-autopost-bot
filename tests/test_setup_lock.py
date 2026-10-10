"""A fresh PRODUCTION installation can never be claimed by an unauthorised first visitor: no SETUP_TOKEN, no setup page."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.config import ConfigError, SETUP_TOKEN_MIN_LENGTH, settings_from_env
from tests.web_support import BASE_ENV, PASSWORD, Env

GOOD = "a-proper-random-setup-token-123"
ROOT = Path(__file__).resolve().parent.parent


class ProductionWithoutTokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start()               # BASE_ENV is production and has no SETUP_TOKEN
        self.c = self.env.client()

    def users(self):
        return self.env.db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    async def test_the_setup_page_is_locked_and_explains_what_to_do(self):
        self.assertTrue(self.env.settings.setup_locked)
        r = await self.c.get("/setup")
        self.assertEqual(r.status, 403)
        self.assertIn("Setup is locked", r.body)
        self.assertIn("SETUP_TOKEN", r.body)
        self.assertNotIn("Create administrator", r.body)
        self.assertNotIn('name="password"', r.body)

    async def test_a_complete_valid_form_cannot_create_an_administrator(self):
        token = await self.c.token_from("/setup")
        for extra in ({}, {"setup_token": ""}, {"setup_token": "anything"}, {"setup_token": GOOD}):
            r = await self.c.post("/setup", {"csrf_token": token, "email": "attacker@example.com", "password": PASSWORD,
                                             "password_confirm": PASSWORD, **extra})
            self.assertEqual(r.status, 403, extra)
            self.assertFalse(self.c.session_cookie())
        self.assertEqual(self.users(), 0)

    async def test_post_without_csrf_cannot_claim_it_either(self):
        r = await self.c.post("/setup", {"email": "attacker@example.com", "password": PASSWORD, "password_confirm": PASSWORD})
        self.assertIn(r.status, (403,))
        self.assertEqual(self.users(), 0)

    async def test_other_pages_still_lead_to_the_locked_page_not_to_an_open_one(self):
        r = await self.c.get("/login")
        self.assertEqual(r.location, "/setup")
        self.assertEqual((await self.c.get("/setup")).status, 403)

    async def test_locked_attempts_do_not_use_up_the_rate_limit_and_never_write(self):
        for _ in range(30):
            self.assertEqual((await self.c.post("/setup", {"csrf_token": await self.c.token_from("/setup")})).status, 403)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)
        self.assertEqual(self.users(), 0)

    async def test_after_the_admin_exists_a_missing_token_is_harmless(self):
        env = await Env().start(with_admin=True, activated=True)    # the operator removed SETUP_TOKEN after setup
        c = env.client()
        self.assertEqual((await c.get("/setup")).status, 404)
        self.assertEqual((await c.get("/login")).status, 200)


class ProductionWithTokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start(env_overrides={"SETUP_TOKEN": GOOD})
        self.c = self.env.client()

    async def test_wrong_token_is_refused_and_right_token_works_exactly_once(self):
        self.assertFalse(self.env.settings.setup_locked)
        token = await self.c.token_from("/setup")
        form = {"csrf_token": token, "email": "owner@example.com", "password": PASSWORD, "password_confirm": PASSWORD}
        for wrong in ("", "wrong", GOOD[:-1], GOOD + "x", GOOD.upper()):
            r = await self.c.post("/setup", {**form, "setup_token": wrong})
            self.assertEqual(r.status, 400, wrong)
            self.assertIn("setup token is not correct", r.body)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        ok = await self.c.post("/setup", {**form, "setup_token": GOOD})
        self.assertEqual(ok.status, 303)
        again = await self.env.client().post("/setup", {**form, "setup_token": GOOD})
        self.assertIn(again.status, (403, 404, 409))
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)


class ConfigurationTests(unittest.TestCase):
    def env(self, **extra):
        return {**BASE_ENV, **extra}

    def test_short_or_placeholder_tokens_are_refused_in_production(self):
        for bad in ("a", "short-token", "x" * (SETUP_TOKEN_MIN_LENGTH - 1), "changeme", "change-this-token-please-1"):
            with self.assertRaises(ConfigError, msg=bad) as ctx:
                settings_from_env(self.env(SETUP_TOKEN=bad))
            self.assertIn("SETUP_TOKEN", str(ctx.exception))
            if len(bad) >= 8:
                self.assertNotIn(bad, str(ctx.exception))                              # the value is never echoed

    def test_empty_or_blank_token_means_locked_not_open(self):
        for blank in ("", "   "):
            s = settings_from_env(self.env(SETUP_TOKEN=blank))
            self.assertTrue(s.setup_locked)

    def test_valid_token_unlocks_and_development_may_run_without_one(self):
        self.assertFalse(settings_from_env(self.env(SETUP_TOKEN=GOOD)).setup_locked)
        dev = settings_from_env(self.env(APP_ENV="development", APP_URL="http://localhost:8000", LICENSE_ENFORCEMENT="false"))
        self.assertFalse(dev.setup_locked)   # development is explicit and is never production

    def test_the_token_is_not_part_of_the_settings_repr(self):
        self.assertNotIn(GOOD, repr(settings_from_env(self.env(SETUP_TOKEN=GOOD))))


class KeyScriptTests(unittest.TestCase):
    def test_generate_keys_prints_a_strong_setup_token_and_fills_env(self):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_keys.py")], capture_output=True, text=True, check=True).stdout
        values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        self.assertGreaterEqual(len(values["SETUP_TOKEN"]), SETUP_TOKEN_MIN_LENGTH)
        env_example = (ROOT / ".env.example").read_text()
        self.assertIn("REQUIRED in production", env_example)
        with tempfile.TemporaryDirectory() as tmp:
            from scripts import generate_keys as gk
            self.assertIn("SETUP_TOKEN", gk.new_values())


if __name__ == "__main__":
    unittest.main()
