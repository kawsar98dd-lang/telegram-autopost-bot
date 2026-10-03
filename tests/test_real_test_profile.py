"""The exact configuration documented in docs/STEP3_REAL_TELEGRAM_VERIFICATION.md must really work.

It walks through the documented real-test journey (development mode, plain http on a LAN address, license
enforcement off) against the real web application with a simulated Telegram. It does NOT prove anything about
the real Telegram network: that is what the manual test is for.
"""

import re
import unittest
from pathlib import Path

from app.auth import csrf
from app.config import ConfigError, settings_from_env
from scripts import generate_keys
from tests.fake_telegram import GOOD_API_HASH, GOOD_API_ID
from tests.web_support import PASSWORD, Env

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "STEP3_REAL_TELEGRAM_VERIFICATION.md").read_text(encoding="utf-8")
LAN = "http://192.168.1.50:8000"
PHONE, CODE = "+8801712345678", "48213"


def documented_env(**extra):
    """What a user ends up with after `generate_keys.py` + the four edits listed in the document."""
    values = generate_keys.new_values()
    env = {"APP_ENV": "development", "APP_URL": LAN, "SETUP_TOKEN": "my-test-word", "LICENSE_ENFORCEMENT": "false",
           "DATABASE_URL": f"postgresql://poster:{values['POSTGRES_PASSWORD']}@db:5432/poster",
           "APP_SECRET": values["APP_SECRET"], "SESSION_ENCRYPTION_KEY": values["SESSION_ENCRYPTION_KEY"]}
    env.update(extra)
    return env


class DocumentedConfigurationTests(unittest.TestCase):
    def test_documented_values_form_a_valid_configuration(self):
        settings = settings_from_env(documented_env())
        self.assertFalse(settings.is_production)
        self.assertFalse(settings.license_enforcement)
        self.assertFalse(settings.cookie_secure)  # plain http: cookies must not be 'Secure' or the browser drops them
        self.assertEqual(settings.app_host, "192.168.1.50:8000")
        self.assertFalse(settings.telegram_configured)  # the test types the credentials into the browser

    def test_the_same_edits_are_refused_in_production_mode(self):
        with self.assertRaises(ConfigError) as ctx:
            settings_from_env(documented_env(APP_ENV="production"))
        text = str(ctx.exception)
        self.assertIn("https://", text)
        self.assertIn("LICENSE_ENFORCEMENT", text)

    def test_origin_of_the_phone_browser_matches_app_url(self):
        self.assertTrue(csrf.origin_ok(LAN, LAN, None))
        self.assertFalse(csrf.origin_ok(LAN, "http://localhost:8000", None))  # why the document says "use exactly APP_URL"

    def test_the_document_covers_what_it_promises(self):
        for needle in ("NEVER share secrets", "PASS", "FAIL", "Android", "Session expired", "Disconnect", "gAAAA",
                       "docker compose down -v", "view-source:", "LICENSE_ENFORCEMENT", "http://<PC-IP>:8000",
                       "OTP", "2FA", "pg_dump", "docker compose logs"):
            self.assertIn(needle, DOC, needle)
        steps = re.findall(r"^\| (\d+) \|", DOC, re.M)
        self.assertEqual(steps, [str(i) for i in range(1, 17)])  # steps 1..16, in order

    def test_the_document_never_asks_for_secrets_and_contains_none(self):
        self.assertNotRegex(DOC, r"(?i)\b(send|paste|share|give|post) (me|us|it|them)? ?(your |the )?(otp|code|password|api hash|session|\.env)\b(?! (to anyone|anywhere))")
        self.assertNotRegex(DOC, r"[0-9a-f]{32}")  # no hash-like value
        self.assertNotRegex(DOC, r"gAAAA[A-Za-z0-9_-]{20,}")
        self.assertNotRegex(DOC, r"\+880\d{10}")  # no real phone number (only the +8801... placeholder)

    def test_env_example_explains_the_development_profile_without_enabling_it(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("LICENSE_ENFORCEMENT=false", text)
        self.assertRegex(text, r"(?m)^#\s+LICENSE_ENFORCEMENT=false")  # only inside a comment
        self.assertNotRegex(text, r"(?m)^LICENSE_ENFORCEMENT=")
        self.assertRegex(text, r"(?m)^APP_ENV=production$")


class DocumentedJourneyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start(env_overrides=documented_env(), enforcement=False)
        self.env.world.add_account(PHONE, 111, code=CODE, username="alice", first_name="Alice")
        self.phone_browser = self.env.client(origin=LAN, ip="192.168.1.77")

    async def test_whole_documented_journey_over_plain_http(self):
        c = self.phone_browser
        # 1-2: first-run page asks for the setup token, admin is created, dashboard opens
        page = await c.get("/setup")
        self.assertIn("Setup token", page.body)
        token = c.csrf(page.body)
        wrong = await c.post("/setup", {"csrf_token": token, "setup_token": "nope", "email": "me@example.com",
                                        "password": PASSWORD, "password_confirm": PASSWORD})
        self.assertEqual(wrong.status, 400)
        made = await c.post("/setup", {"csrf_token": token, "setup_token": "my-test-word", "email": "me@example.com",
                                       "password": PASSWORD, "password_confirm": PASSWORD})
        self.assertEqual((made.status, made.location), (303, "/"))  # no license detour: enforcement is off
        self.assertIn("tap_session", c.cookies)  # plain (non '__Host-') cookie name on http
        self.assertTrue(all("Secure" not in x for r in c.log for x in r.set_cookies))
        self.assertIn("Dashboard", (await c.get("/")).body)
        # 3: logout and login again
        await c.post("/logout", {"csrf_token": await c.token_from("/")})
        self.assertEqual((await self.env.login(c, "me@example.com")).location, "/")
        # 4-9: add the account (credentials typed into the form), code, connected
        self.assertIn('type="password" name="api_hash"', (await c.get("/telegram/add")).body)
        started = await c.post("/telegram/add", {"csrf_token": await c.token_from("/telegram/add"), "phone": PHONE,
                                                 "api_id": str(GOOD_API_ID), "api_hash": GOOD_API_HASH})
        self.assertEqual(started.status, 303)
        verify = started.location
        self.assertNotIn(PHONE, (await c.get(verify)).body)
        done = await c.post(verify + "/code", {"csrf_token": await c.token_from(verify), "code": CODE})
        self.assertEqual(done.location, "/telegram?notice=connected")
        listing = (await c.get("/telegram")).body
        self.assertIn("Alice", listing)
        self.assertIn("Connected", listing)
        # 12: session revoked in the Telegram app -> Check -> expired
        account_id = (await self.env.ctx.telegram_connect.list_accounts(self._user_id()))[0]["id"]
        token = await c.token_from("/telegram")
        self.assertEqual((await c.post(f"/telegram/accounts/{account_id}/check", {"csrf_token": token})).location,
                         "/telegram?notice=checked")
        self.env.world.revoke_everything()
        self.assertEqual((await c.post(f"/telegram/accounts/{account_id}/check", {"csrf_token": token})).location,
                         "/telegram?notice=expired")
        self.assertIn("Session expired", (await c.get("/telegram")).body)
        # 13: reconnect reuses the same account row
        again = await c.post("/telegram/add", {"csrf_token": await c.token_from("/telegram/add"), "phone": PHONE,
                                               "api_id": str(GOOD_API_ID), "api_hash": GOOD_API_HASH})
        await c.post(again.location + "/code", {"csrf_token": await c.token_from(again.location), "code": CODE})
        accounts = await self.env.ctx.telegram_connect.list_accounts(self._user_id())
        self.assertEqual((len(accounts), accounts[0]["status"], accounts[0]["id"]), (1, "connected", account_id))
        # 14: disconnect removes the local session and the stored API credentials
        token = await c.token_from("/telegram")
        self.assertEqual((await c.post(f"/telegram/accounts/{account_id}/disconnect", {"csrf_token": token})).location,
                         "/telegram?notice=disconnected")
        db = self.env.db.conn
        self.assertEqual(db.execute("SELECT COUNT(*) FROM telegram_sessions").fetchone()[0], 0)
        row = db.execute("SELECT status, api_id_enc, api_hash_enc FROM telegram_accounts").fetchone()
        self.assertEqual(tuple(row), ("disconnected", None, None))
        self.assertEqual(self.env.world.live, set())
        # 15: a wrong code on purpose
        third = await c.post("/telegram/add", {"csrf_token": await c.token_from("/telegram/add"), "phone": PHONE,
                                               "api_id": str(GOOD_API_ID), "api_hash": GOOD_API_HASH})
        bad = await c.post(third.location + "/code", {"csrf_token": await c.token_from(third.location), "code": "00000"})
        self.assertIn("4 attempt(s) left", bad.body)
        # 16: remove
        token = await c.token_from("/telegram")
        await c.post(f"/telegram/accounts/{account_id}/remove", {"csrf_token": token})
        self.assertEqual(db.execute("SELECT COUNT(*) FROM telegram_accounts").fetchone()[0], 0)
        # nothing the user typed is stored in clear anywhere, and no response ever contained it
        dump = "\n".join(str(v) for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                         for r in db.execute(f'SELECT * FROM "{t}"').fetchall() for v in tuple(r))
        for secret in (GOOD_API_HASH, CODE, PHONE):
            self.assertNotIn(secret, dump)
            for reply in c.log:
                self.assertNotIn(secret, reply.body)

    def _user_id(self):
        return self.env.db.conn.execute("SELECT id FROM users").fetchone()[0]

    async def test_the_page_served_on_the_lan_address_rejects_other_origins(self):
        c = self.env.client(origin="http://localhost:8000")
        token = c.csrf((await c.get("/setup")).body)
        r = await c.post("/setup", {"csrf_token": token, "setup_token": "my-test-word", "email": "a@example.com",
                                    "password": PASSWORD, "password_confirm": PASSWORD})
        self.assertEqual(r.status, 403)


if __name__ == "__main__":
    unittest.main()
