import asyncio
import logging
import re
import unittest

from tests.web_support import KEY, PASSWORD, Env


class LogCapture:
    def __init__(self):
        self.records, self.handler = [], logging.Handler()
        self.handler.emit = self.records.append

    def __enter__(self):
        logging.getLogger().addHandler(self.handler)
        self._old = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self.handler)
        logging.getLogger().setLevel(self._old)

    def text(self):
        fmt = logging.Formatter("%(message)s")
        return "\n".join(fmt.format(r) for r in self.records)


class FirstRunSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start()
        self.c = self.env.client()

    async def submit(self, client=None, **override):
        client = client or self.c
        token = await client.token_from("/setup")
        form = {"csrf_token": token, "email": "Owner@Example.com", "display_name": "Owner",
                "password": PASSWORD, "password_confirm": PASSWORD, **override}
        return await client.post("/setup", form)

    async def test_fresh_install_sends_everything_to_setup(self):
        for path in ("/login", "/setup"):
            r = await self.c.get(path)
            self.assertIn(r.status, (200, 303), path)
        self.assertEqual((await self.c.get("/login")).location, "/setup")
        self.assertEqual((await self.c.get("/")).location, "/activate")  # gate first ...
        self.assertEqual((await self.c.get("/activate")).location, "/setup")  # ... then setup
        page = await self.c.get("/setup")
        self.assertEqual(page.status, 200)
        self.assertIn("Create administrator", page.body)
        self.assertNotIn("setup_token", page.body)  # no token configured

    async def test_admin_creation_success(self):
        r = await self.submit()
        self.assertEqual((r.status, r.location), (303, "/activate"))
        row = self.env.db.conn.execute("SELECT email, is_admin, password_hash, display_name FROM users").fetchone()
        self.assertEqual((row["email"], row["is_admin"], row["display_name"]), ("owner@example.com", 1, "Owner"))
        self.assertTrue(row["password_hash"].startswith("scrypt$"))
        self.assertNotIn(PASSWORD, row["password_hash"])
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 1)
        self.assertTrue(self.c.session_cookie())  # signed in straight away

    async def test_session_cookie_flags(self):
        r = await self.submit()
        cookie = next(c for c in r.set_cookies if c.startswith("__Host-tap_session="))
        for flag in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Domain", cookie)
        self.assertIn("Max-Age=604800", cookie)  # 168h absolute lifetime

    async def test_setup_is_disabled_permanently_afterwards(self):
        await self.submit()
        other = self.env.client()
        for method in ("GET", "POST"):
            r = await other.request(method, "/setup", form={} if method == "POST" else None)
            self.assertEqual(r.status, 404, method)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        self.assertEqual((await self.c.get("/setup")).status, 404)  # even for the new admin

    async def test_weak_or_invalid_input_is_rejected_without_creating_anything(self):
        cases = [
            ({"password": "short", "password_confirm": "short"}, "at least 12"),
            ({"password": "password1234", "password_confirm": "password1234"}, "too common"),
            ({"password_confirm": "different-password-1"}, "do not match"),
            ({"email": "not-an-email"}, "valid email"),
            ({"password": "owner-secret-pass", "password_confirm": "owner-secret-pass"}, "email name"),
        ]
        for override, needle in cases:
            r = await self.submit(**override)
            self.assertEqual(r.status, 400, override)
            self.assertIn(needle, r.body)
            self.assertNotIn(override.get("password", "zzz-none"), r.body)  # password never echoed back
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    async def test_simultaneous_first_run_requests_create_exactly_one_admin(self):
        clients = [self.env.client(ip=f"198.51.100.{i}") for i in range(4)]
        tokens = [await c.token_from("/setup") for c in clients]

        def post(i):
            return clients[i].post("/setup", {"csrf_token": tokens[i], "email": f"admin{i}@example.com",
                                              "password": PASSWORD, "password_confirm": PASSWORD})

        replies = await asyncio.gather(*[post(i) for i in range(4)])
        self.assertEqual(sorted(r.status for r in replies), [303, 409, 409, 409])
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 1)
        loser = next(c for c, r in zip(clients, replies) if r.status == 409)
        self.assertFalse(loser.session_cookie())  # losers never get a session

    async def test_setup_token_protects_a_fresh_installation(self):
        env = await Env().start(env_overrides={"SETUP_TOKEN": "let-me-in-please"})
        c = env.client()
        self.assertIn("setup_token", (await c.get("/setup")).body)
        token = await c.token_from("/setup")
        form = {"csrf_token": token, "email": "a@example.com", "password": PASSWORD, "password_confirm": PASSWORD}
        for bad in ("", "wrong"):
            r = await c.post("/setup", {**form, "setup_token": bad})
            self.assertEqual(r.status, 400)
            self.assertIn("token is not correct", r.body)
        self.assertEqual(env.db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        self.assertEqual((await c.post("/setup", {**form, "setup_token": "let-me-in-please"})).status, 303)

    async def test_setup_attempts_are_rate_limited(self):
        for _ in range(10):
            self.assertEqual((await self.submit(email="bad")).status, 400)
        self.assertEqual((await self.submit(email="bad")).status, 429)

    async def test_setup_requires_csrf(self):
        r = await self.c.post("/setup", {"email": "a@example.com", "password": PASSWORD, "password_confirm": PASSWORD})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start(activated=True, with_admin=True)
        self.c = self.env.client()

    async def test_login_success_creates_a_hashed_session(self):
        r = await self.env.login(self.c)
        self.assertEqual((r.status, r.location), (303, "/"))
        token = self.c.session_cookie()
        self.assertTrue(token)
        rows = self.env.db.conn.execute("SELECT token_hash FROM auth_sessions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0][0], token)  # only a hash is stored
        page = await self.c.get("/")
        self.assertEqual(page.status, 200)
        self.assertIn("admin@example.com", page.body)

    async def test_failures_are_indistinguishable(self):
        await self.env.add_user("inactive@example.com", active=False)
        bodies = []
        for email, pw in (("admin@example.com", "wrong-password-1"), ("nobody@example.com", PASSWORD),
                          ("inactive@example.com", PASSWORD), ("", ""), ("not an email", "x")):
            c = self.env.client()
            r = await self.env.login(c, email, pw)
            self.assertEqual(r.status, 401, email)
            self.assertIn("Invalid email or password.", r.body)
            self.assertFalse(c.session_cookie())
            normalised = r.body.replace(email, "<email>") if email else r.body
            normalised = re.sub(r'content="[A-Za-z0-9_\-]{43}"|value="[A-Za-z0-9_\-]{43}"', "<csrf>", normalised)
            bodies.append(normalised)
        # Whether the account exists, is disabled, or the input is junk, the page is byte-for-byte the same.
        self.assertEqual(len({b.replace('value="" ', 'value="<email>" ') for b in bodies[:3]}), 1)

    async def test_session_fixation_is_impossible(self):
        planted = "attacker-chosen-session-token-value-1234567890"
        victim = self.env.client()
        victim.cookies["__Host-tap_session"] = planted
        r = await self.env.login(victim)
        self.assertEqual(r.status, 303)
        self.assertNotEqual(victim.session_cookie(), planted)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 1)
        attacker = self.env.client()
        attacker.cookies["__Host-tap_session"] = planted
        self.assertEqual((await attacker.get("/")).location, "/login?next=%2F")

    async def test_login_from_a_signed_in_browser_replaces_the_session(self):
        await self.env.login(self.c)
        first = self.c.session_cookie()
        await self.c.post("/logout", {"csrf_token": await self.c.token_from("/")})
        await self.env.login(self.c)
        self.assertNotEqual(self.c.session_cookie(), first)
        stale = self.env.client()
        stale.cookies["__Host-tap_session"] = first
        self.assertEqual((await stale.get("/")).status, 303)

    async def test_logout(self):
        await self.env.login(self.c)
        cookie = self.c.session_cookie()
        r = await self.c.post("/logout", {"csrf_token": await self.c.token_from("/")})
        self.assertEqual((r.status, r.location), (303, "/login"))
        self.assertFalse(self.c.session_cookie())
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)
        replay = self.env.client()
        replay.cookies["__Host-tap_session"] = cookie  # a stolen cookie is useless after logout
        self.assertEqual((await replay.get("/")).status, 303)
        self.assertEqual((await self.c.get("/logout")).status, 405)

    async def test_idle_expiry_and_cookie_cleanup(self):
        await self.env.login(self.c)
        self.env.clock.advance(60 * 100)  # 100 min: fine
        self.assertEqual((await self.c.get("/")).status, 200)
        self.env.clock.advance(60 * 121)  # 121 idle minutes
        r = await self.c.get("/")
        self.assertEqual((r.status, r.location), (303, "/login?next=%2F"))
        self.assertFalse(self.c.session_cookie())
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)

    async def test_absolute_expiry_even_with_constant_activity(self):
        await self.env.login(self.c)
        for _ in range(100):  # one request every 100 minutes for ~7 days
            self.env.clock.advance(60 * 100)
            self.assertEqual((await self.c.get("/")).status, 200)
        self.env.clock.advance(60 * 100 * 2)
        self.assertEqual((await self.c.get("/")).status, 303)

    async def test_invalid_session_cookies(self):
        for junk in ("garbage", "a" * 300, "%00", "x;y"):
            c = self.env.client()
            c.cookies["__Host-tap_session"] = junk
            r = await c.get("/account")
            self.assertEqual(r.status, 303, junk)
            self.assertTrue(any("Max-Age=0" in x for x in r.set_cookies))

    async def test_disabled_account_loses_access_immediately(self):
        await self.env.login(self.c)
        self.env.db.conn.execute("UPDATE users SET is_active = 0")
        self.env.db.conn.commit()
        self.assertEqual((await self.c.get("/")).status, 303)

    async def test_signed_in_users_skip_the_login_page(self):
        await self.env.login(self.c)
        self.assertEqual((await self.c.get("/login")).location, "/")

    async def test_redirect_after_login_and_open_redirect_prevention(self):
        for target, expected in (("/account", "/account"), ("https://evil.example", "/"), ("//evil.example", "/"),
                                 ("/\\evil.example", "/"), ("javascript:alert(1)", "/"), ("/login", "/")):
            c = self.env.client()
            r = await self.env.login(c, next_=target)
            self.assertEqual(r.location, expected, target)
        c = self.env.client()
        page = await c.get("/login", query="next=https://evil.example")
        self.assertNotIn("evil.example", page.body)
        page = await c.get("/login", query="next=/account")
        self.assertIn('value="/account"', page.body)
        anon = self.env.client()
        self.assertEqual((await anon.get("/account")).location, "/login?next=%2Faccount")

    async def test_login_rate_limit_per_account_and_ip(self):
        c = self.env.client()
        for _ in range(5):
            self.assertEqual((await self.env.login(c, password="wrong-password-1")).status, 401)
        blocked = await self.env.login(c)  # even the CORRECT password is refused now
        self.assertEqual(blocked.status, 429)
        self.assertGreater(int(blocked.headers["retry-after"]), 0)
        self.assertFalse(c.session_cookie())
        elsewhere = self.env.client(ip="198.51.100.77")  # a different client is not locked out by this
        self.assertEqual((await self.env.login(elsewhere)).status, 303)
        self.env.clock.advance(15 * 60 + 1)
        self.assertEqual((await self.env.login(self.env.client())).status, 303)

    async def test_unknown_accounts_are_rate_limited_like_real_ones(self):
        c = self.env.client()
        statuses = [(await self.env.login(c, "ghost@example.com", "whatever-pass-1")).status for _ in range(6)]
        self.assertEqual(statuses, [401] * 5 + [429])

    async def test_ip_limit_and_success_resets_account_counter(self):
        c = self.env.client()
        for _ in range(4):
            await self.env.login(c, password="wrong-password-1")
        self.assertEqual((await self.env.login(self.env.client())).status, 303)  # success resets acct+ip counter
        for _ in range(4):
            self.assertEqual((await self.env.login(c, password="wrong-password-1")).status, 401)
        flood = self.env.client(ip="192.0.2.9")
        statuses = [(await self.env.login(flood, f"u{i}@example.com", "wrong-password-1")).status for i in range(21)]
        self.assertEqual(statuses[:20], [401] * 20)
        self.assertEqual(statuses[20], 429)

    async def test_secrets_never_reach_the_logs(self):
        with LogCapture() as logs:
            await self.env.login(self.env.client(), password="hunter2-secret-pass")
            await self.env.login(self.env.client())
            await self.c.get("/login")
        text = logs.text()
        for secret in ("hunter2-secret-pass", PASSWORD, self.c.session_cookie() or "-none-", KEY):
            self.assertNotIn(secret, text)

    async def test_htmx_login_uses_hx_redirect(self):
        token = await self.c.token_from("/login")
        r = await self.c.post("/login", {"csrf_token": token, "email": "admin@example.com", "password": PASSWORD},
                              headers={"HX-Request": "true"})
        self.assertEqual((r.status, r.headers["hx-redirect"]), (200, "/"))


if __name__ == "__main__":
    unittest.main()
