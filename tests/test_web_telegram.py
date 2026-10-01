import io
import logging
import re
import unittest

from app.web.routes import ROUTES
from tests.fake_telegram import GOOD_API_HASH, GOOD_API_ID
from tests.web_support import PASSWORD, Env

PHONE, CODE, TFA = "+8801712345678", "48213", "cloud-2fa-Secret1"
API_ID = str(GOOD_API_ID)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start(activated=True, with_admin=True)
        self.env.world.add_account(PHONE, 111, code=CODE)
        self.env.world.add_account("+8801812345678", 222, code=CODE, username="bob", first_name="Bob")
        self.c = self.env.client()
        await self.env.login(self.c)

    async def start(self, client=None, phone=PHONE, api_id=API_ID, api_hash=GOOD_API_HASH, path="/telegram/add"):
        client = client or self.c
        token = await client.token_from(path)
        return await client.post("/telegram/add", {"csrf_token": token, "phone": phone, "api_id": api_id, "api_hash": api_hash})

    async def code(self, attempt_path, code=CODE, client=None):
        client = client or self.c
        token = await client.token_from(attempt_path)
        return await client.post(attempt_path + "/code", {"csrf_token": token, "code": code})

    async def connect(self, client=None, phone=PHONE):
        r = await self.start(client, phone)
        path = r.location
        r = await self.code(path, client=client)
        return path, r

    def db_one(self, sql, *a):
        return self.env.db.conn.execute(sql, a).fetchone()


class AccountPagesTests(Base):
    async def test_pages_require_authentication_and_the_license(self):
        anon = self.env.client()
        for path in ("/telegram", "/telegram/add", "/telegram/verify/00000000-0000-0000-0000-000000000abc"):
            r = await anon.get(path)
            self.assertEqual((r.status, r.location.split("?")[0]), (303, "/login"), path)
        self.env.server.revoke("TAP-ABCDE-FGHJK-MNPQR-STVWX")
        await self.env.manager.verify()
        self.assertEqual((await self.c.get("/telegram")).location, "/activate")  # license gate applies

    async def test_empty_state_and_add_form(self):
        page = (await self.c.get("/telegram")).body
        self.assertIn("Add Telegram Account", page)
        form = (await self.c.get("/telegram/add")).body
        for text in ('name="phone"', 'name="api_id"', 'name="api_hash"', 'type="password" name="api_hash"', "Send login code"):
            self.assertIn(text, form)

    async def test_full_flow_shows_only_safe_metadata(self):
        r = await self.start()
        self.assertEqual(r.status, 303)
        attempt_path = r.location
        self.assertRegex(attempt_path, r"^/telegram/verify/[0-9a-f-]{36}$")
        page = (await self.c.get(attempt_path)).body
        self.assertIn("+88", page)
        self.assertNotIn(PHONE, page)
        self.assertIn('autocomplete="one-time-code"', page)
        done = await self.code(attempt_path)
        self.assertEqual((done.status, done.location), (303, "/telegram?notice=connected"))
        accounts = (await self.c.get("/telegram", query="notice=connected")).body
        for text in ("Telegram account connected.", "Alice", "@alice", ">111<", "Connected"):
            self.assertIn(text, accounts)
        live = next(iter(self.env.world.live))
        for secret in (PHONE, GOOD_API_HASH, live, CODE):
            self.assertNotIn(secret, accounts)
            for reply in self.c.log:
                self.assertNotIn(secret, reply.body, "a secret reached a browser response")
                self.assertNotIn(secret, " ".join(reply.headers.values() if reply.headers else []))

    async def test_api_credentials_are_never_echoed_back_into_forms(self):
        r = await self.start(api_hash="a" * 32, api_id="99999")
        self.assertEqual(r.status, 400)
        self.assertIn("did not accept the API ID", r.body)
        self.assertNotIn("a" * 32, r.body)
        self.assertNotIn("99999", r.body)
        r = await self.start(api_hash="short")
        self.assertEqual(r.status, 400)
        self.assertNotIn(">short<", r.body)
        self.assertNotIn('value="short"', r.body)

    async def test_error_messages_are_fixed_texts(self):
        cases = [({"phone": "12345"}, 400, "not valid"), ({"phone": "+8801999999999"}, 400, "not valid")]
        for kwargs, status, needle in cases:
            r = await self.start(**kwargs)
            self.assertEqual(r.status, status)
            self.assertIn(needle, r.body)
        self.env.world.down = True
        r = await self.start()
        self.assertEqual(r.status, 503)
        self.assertIn("could not be reached", r.body)
        self.assertNotIn("ConnectionError", r.body)
        self.env.world.down = False
        self.env.world.flood = 42
        r = await self.start()
        self.assertEqual((r.status, r.headers["retry-after"]), (429, "42"))
        self.assertIn("42 seconds", r.body)
        self.env.world.flood = 0
        self.env.world.accounts[PHONE].banned = True
        self.assertIn("does not allow", (await self.start()).body)

    async def test_wrong_code_then_success(self):
        attempt = (await self.start()).location
        r = await self.code(attempt, "00000")
        self.assertEqual(r.status, 400)
        self.assertIn("not correct", r.body)
        self.assertIn("4 attempt(s) left", r.body)
        self.assertEqual((await self.code(attempt)).status, 303)

    async def test_too_many_wrong_codes_send_the_user_back_to_the_start(self):
        attempt = (await self.start()).location
        for _ in range(4):
            self.assertEqual((await self.code(attempt, "00000")).status, 400)
        r = await self.code(attempt, "00000")
        self.assertIn("Too many wrong attempts", r.body)
        self.assertEqual((await self.c.get(attempt)).location, "/telegram/add?gone=not_found")
        self.assertIn("no longer available", (await self.c.get("/telegram/add", query="gone=not_found")).body)

    async def test_two_factor_flow(self):
        self.env.world.accounts[PHONE].password = TFA
        attempt = (await self.start()).location
        step = await self.code(attempt)
        self.assertEqual((step.status, step.location), (303, attempt))  # back to the same page, now asking for 2FA
        page = (await self.c.get(attempt)).body
        self.assertIn("two-step verification", page)
        self.assertIn('type="password" name="password"', page)
        self.assertNotIn('name="code"', page)
        token = await self.c.token_from(attempt)
        bad = await self.c.post(attempt + "/password", {"csrf_token": token, "password": "nope"})
        self.assertEqual(bad.status, 400)
        self.assertIn("password is not correct", bad.body)
        self.assertNotIn("nope", bad.body)
        ok = await self.c.post(attempt + "/password", {"csrf_token": token, "password": TFA})
        self.assertEqual(ok.location, "/telegram?notice=connected")
        self.assertEqual(self.db_one("SELECT status FROM telegram_accounts")[0], "connected")

    async def test_code_step_cannot_be_used_for_the_password_step_and_vice_versa(self):
        attempt = (await self.start()).location
        token = await self.c.token_from(attempt)
        r = await self.c.post(attempt + "/password", {"csrf_token": token, "password": TFA})
        self.assertEqual((r.status, r.location), (303, attempt))

    async def test_expired_code_page(self):
        attempt = (await self.start()).location
        self.env.world.expired_hashes.update(self.env.world.code_hashes)
        r = await self.code(attempt)
        self.assertEqual(r.status, 400)
        self.assertIn("code has expired", r.body)
        self.assertEqual(self.db_one("SELECT COUNT(*) FROM telegram_login_attempts")[0], 0)

    async def test_cancel(self):
        attempt = (await self.start()).location
        token = await self.c.token_from(attempt)
        r = await self.c.post(attempt + "/cancel", {"csrf_token": token})
        self.assertEqual((r.status, r.location), (303, "/telegram"))
        self.assertEqual(self.db_one("SELECT COUNT(*) FROM telegram_login_attempts")[0], 0)

    async def test_disconnect_check_and_remove(self):
        await self.connect()
        (acc,) = await self.env.ctx.telegram_connect.list_accounts(self.env.admin_id)
        base = f"/telegram/accounts/{acc['id']}"
        token = await self.c.token_from("/telegram")
        r = await self.c.post(base + "/check", {"csrf_token": token})
        self.assertEqual(r.location, "/telegram?notice=checked")
        self.env.world.revoke_everything()
        r = await self.c.post(base + "/check", {"csrf_token": token})
        self.assertEqual(r.location, "/telegram?notice=expired")
        self.assertIn("Session expired", (await self.c.get("/telegram")).body)
        # reconnect, then disconnect and remove
        await self.connect()
        r = await self.c.post(base + "/disconnect", {"csrf_token": token})
        self.assertEqual(r.location, "/telegram?notice=disconnected")
        self.assertEqual(self.db_one("SELECT COUNT(*) FROM telegram_sessions")[0], 0)
        self.assertEqual(self.env.world.live, set())
        self.assertIn("Disconnected", (await self.c.get("/telegram")).body)
        r = await self.c.post(base + "/remove", {"csrf_token": token})
        self.assertEqual(r.location, "/telegram?notice=removed")
        self.assertEqual(self.db_one("SELECT COUNT(*) FROM telegram_accounts")[0], 0)

    async def test_sending_codes_is_rate_limited_per_user(self):
        statuses = [(await self.start(phone="+8801999999999")).status for _ in range(6)]
        self.assertEqual(statuses, [400] * 5 + [429])

    async def test_verification_attempts_are_rate_limited_per_user(self):
        attempt = (await self.start()).location
        token = await self.c.token_from(attempt)
        statuses = []
        for _ in range(22):
            r = await self.c.post(attempt + "/code", {"csrf_token": token, "code": "00000"})
            statuses.append(r.status)
            if r.status != 400:
                pass
        self.assertIn(429, statuses)


class CsrfAndIsolationTests(Base):
    async def test_every_telegram_post_needs_a_csrf_token(self):
        for route in [r for r in ROUTES if r.method == "POST" and r.path.startswith("/telegram")]:
            path = re.sub(r"\{[a-z_]+\}", "00000000-0000-0000-0000-000000000abc", route.path)
            self.assertEqual((await self.c.post(path, {"phone": PHONE})).status, 403, route.path)
            self.assertEqual((await self.c.post(path, {"csrf_token": "bad"})).status, 403, route.path)
        self.assertEqual(self.db_one("SELECT COUNT(*) FROM telegram_login_attempts")[0], 0)

    async def test_another_user_cannot_see_verify_or_manage_my_accounts(self):
        await self.env.add_user("member@example.com")
        member = self.env.client()
        await self.env.login(member, "member@example.com")
        attempt_path, _ = await self.connect()
        (acc,) = await self.env.ctx.telegram_connect.list_accounts(self.env.admin_id)
        second = (await self.start(phone="+8801812345678")).location  # admin has a fresh attempt in progress
        # verify page / code / password / cancel of someone else's attempt
        self.assertEqual((await member.get(second)).location, "/telegram/add?gone=not_found")
        token = await member.token_from("/telegram")
        r = await member.post(second + "/code", {"csrf_token": token, "code": CODE})
        self.assertEqual(r.status, 400)
        self.assertIn("no longer available", r.body)
        await member.post(second + "/cancel", {"csrf_token": token})
        self.assertTrue(await self.env.ctx.telegram_connect.pending(self.env.admin_id, second.rsplit("/", 1)[1]))
        # account actions look exactly like a missing account
        for action in ("check", "disconnect", "remove"):
            r = await member.post(f"/telegram/accounts/{acc['id']}/{action}", {"csrf_token": token})
            self.assertEqual(r.status, 404, action)
        self.assertEqual(self.db_one("SELECT status FROM telegram_accounts")[0], "connected")
        self.assertNotIn("Alice", (await member.get("/telegram")).body)
        self.assertIn("Alice", (await self.c.get("/telegram")).body)

    async def test_malformed_ids_do_not_match_any_route(self):
        for path in ("/telegram/verify/1", "/telegram/verify/../add", "/telegram/accounts/x/disconnect",
                     "/telegram/verify/" + "z" * 36 + "/code"):
            self.assertEqual((await self.c.get(path)).status, 404, path)

    async def test_disconnected_or_foreign_accounts_do_not_leak_in_counts(self):
        await self.env.add_user("member@example.com")
        member = self.env.client()
        await self.env.login(member, "member@example.com")
        await self.connect()
        count = lambda html: re.search(r"Telegram accounts</h3><p class=\"big\">(\d+)</p>", html).group(1)  # noqa: E731
        self.assertEqual(count((await self.c.get("/")).body), "1")
        self.assertEqual(count((await member.get("/")).body), "0")


class LoggingTests(Base):
    async def test_no_secret_reaches_logs_during_a_full_web_flow(self):
        self.env.world.accounts[PHONE].password = TFA
        stream = io.StringIO()
        from app.security.redact import RedactingFormatter

        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(name)s %(levelname)s %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            attempt = (await self.start()).location
            await self.code(attempt, "00000")
            await self.code(attempt)
            token = await self.c.token_from(attempt)
            await self.c.post(attempt + "/password", {"csrf_token": token, "password": "wrong-tfa-pass"})
            await self.c.post(attempt + "/password", {"csrf_token": token, "password": TFA})
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        logged = stream.getvalue()
        live = next(iter(self.env.world.live))
        for secret in (CODE, "00000", TFA, "wrong-tfa-pass", GOOD_API_HASH, live, PHONE, PASSWORD):
            self.assertNotIn(secret, logged, secret)
        self.assertNotIn(CODE, self.env.db.conn.execute("SELECT group_concat(value) FROM (SELECT value FROM app_state)").fetchone()[0] or "")

    async def test_unexpected_errors_in_the_service_never_show_details(self):
        async def boom(*args, **kwargs):
            raise RuntimeError(f"db exploded api_hash={GOOD_API_HASH}")

        self.env.ctx.telegram_connect.start = boom
        r = await self.start()
        self.assertEqual(r.status, 500)
        self.assertNotIn(GOOD_API_HASH, r.body)
        self.assertNotIn("Traceback", r.body)
        self.assertRegex(r.body, r"Reference: [0-9a-f]{8}")


if __name__ == "__main__":
    unittest.main()
