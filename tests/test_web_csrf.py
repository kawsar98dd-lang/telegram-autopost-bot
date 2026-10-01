import re
import unittest

from app.web.pipeline import UNSAFE_METHODS
from app.web.routes import ROUTES
from tests.web_support import PASSWORD, Env


class CsrfTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start(activated=True, with_admin=True)
        self.c = self.env.client()

    async def signed_in(self):
        c = self.env.client()
        await self.env.login(c)
        return c

    async def test_login_form_valid_and_invalid_tokens(self):
        good = await self.c.token_from("/login")
        cases = {
            "missing": None, "empty": "", "garbage": "abc", "tampered": good[:-2] + "AA",
            "other browser": await self.env.client().token_from("/login"),
        }
        for label, token in cases.items():
            c = self.env.client()
            c.cookies.update(self.c.cookies)
            form = {"email": "admin@example.com", "password": PASSWORD}
            if token is not None:
                form["csrf_token"] = token
            r = await c.post("/login", form)
            self.assertEqual(r.status, 403, label)
            self.assertFalse(c.session_cookie(), label)
        ok = await self.c.post("/login", {"csrf_token": good, "email": "admin@example.com", "password": PASSWORD})
        self.assertEqual(ok.status, 303)

    async def test_browser_without_any_token_cookie_is_refused(self):
        r = await self.env.client().post("/login", {"csrf_token": "x", "email": "a@example.com", "password": PASSWORD})
        self.assertEqual(r.status, 403)

    async def test_every_state_changing_route_requires_a_token(self):
        unsafe = [r for r in ROUTES if r.method in UNSAFE_METHODS]
        self.assertGreaterEqual(len(unsafe), 5)
        for route in unsafe:
            if route.name == "setup":
                continue  # covered in the first-run tests (setup is only reachable on fresh installs)
            c = self.env.client() if route.name == "login" else await self.signed_in()
            path = re.sub(r"\{[a-z_]+\}", "00000000-0000-0000-0000-000000000abc", route.path)
            r = await c.post(path, {"license_key": KEY_FOR_TESTS, "email": "a@example.com"})
            self.assertEqual(r.status, 403, route.path)
            r = await c.post(path, {"csrf_token": "forged"})
            self.assertEqual(r.status, 403, route.path)

    async def test_forms_send_a_token_in_every_post_form(self):
        c = await self.signed_in()
        for path in ("/", "/account", "/activate"):
            html = (await c.get(path)).body
            forms = html.count('method="post"')
            self.assertGreaterEqual(html.count('name="csrf_token"'), forms, path)
        self.assertIn('name="csrf-token"', (await c.get("/")).body)

    async def test_token_is_bound_to_the_session_and_rotates_with_it(self):
        a, b = await self.signed_in(), await self.signed_in()
        token_a = await a.token_from("/")
        self.assertNotEqual(token_a, await b.token_from("/"))
        stolen = await b.post("/logout", {"csrf_token": token_a})  # one user's token on another session
        self.assertEqual(stolen.status, 403)
        self.assertTrue(b.session_cookie())
        pre_login = await self.c.token_from("/login")
        await self.c.post("/login", {"csrf_token": pre_login, "email": "admin@example.com", "password": PASSWORD})
        self.assertNotEqual(pre_login, await self.c.token_from("/"))
        self.assertEqual((await self.c.post("/logout", {"csrf_token": pre_login})).status, 403)

    async def test_htmx_uses_the_header(self):
        c = await self.signed_in()
        token = await c.token_from("/")
        r = await c.post("/license/verify", {}, headers={"HX-Request": "true", "X-CSRF-Token": token})
        self.assertEqual(r.status, 200)
        self.assertIn('id="license-card"', r.body)
        r = await c.post("/license/verify", {}, headers={"HX-Request": "true"})
        self.assertEqual(r.status, 403)
        self.assertEqual(r.headers["hx-retarget"], "#flash")
        self.assertIn("expired", r.body)
        r = await c.post("/license/verify", {}, headers={"HX-Request": "true", "X-CSRF-Token": "nope"})
        self.assertEqual(r.status, 403)

    async def test_cross_site_requests_are_refused_even_with_a_valid_token(self):
        c = await self.signed_in()
        token = await c.token_from("/")
        for origin in ("https://evil.example", "null", "http://poster.example.com", "https://poster.example.com.evil.io"):
            r = await c.post("/logout", {"csrf_token": token}, origin=origin)
            self.assertEqual(r.status, 403, origin)
        r = await c.post("/logout", {"csrf_token": token}, origin=None, headers={"Referer": "https://evil.example/x"})
        self.assertEqual(r.status, 403)
        self.assertTrue(c.session_cookie())
        ok = await c.post("/logout", {"csrf_token": token}, origin=None)  # no Origin/Referer: token decides
        self.assertEqual(ok.status, 303)

    async def test_get_requests_never_change_state(self):
        c = await self.signed_in()
        for path in ("/logout", "/license/verify", "/account/sessions/revoke-others"):
            self.assertEqual((await c.get(path)).status, 405, path)
        self.assertTrue(c.session_cookie())


KEY_FOR_TESTS = "TAP-ABCDE-FGHJK-MNPQR-STVWX"

if __name__ == "__main__":
    unittest.main()
