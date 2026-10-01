import base64
import hashlib
import io
import logging
import os
import re
import unittest
from pathlib import Path

from app.security.redact import RedactingFormatter
from app.web.pipeline import Auth
from app.web.routes import ROUTES
from scripts import vendor_htmx
from tests.web_support import PASSWORD, Env

WEB = Path(__file__).resolve().parents[1] / "app" / "web"
SAMPLE_ID = "00000000-0000-0000-0000-000000000abc"


def concrete(path: str) -> str:
    return re.sub(r"\{[a-z_]+\}", SAMPLE_ID, path)


PROTECTED_GET = [concrete(r.path) for r in ROUTES if r.method == "GET" and r.auth is not Auth.ANONYMOUS]


class ProtectedRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start(activated=True, with_admin=True)

    async def test_every_protected_page_redirects_anonymous_visitors(self):
        anon = self.env.client()
        self.assertGreaterEqual(len(PROTECTED_GET), 9)
        for path in PROTECTED_GET:
            r = await anon.get(path)
            self.assertEqual(r.status, 303, path)
            self.assertTrue(r.location.startswith("/login"), path)
            self.assertNotIn("admin@example.com", r.body)

    async def test_api_and_htmx_clients_get_401_or_hx_redirect(self):
        anon = self.env.client()
        r = await anon.get("/account", headers={"Accept": "application/json"})
        self.assertEqual(r.status, 401)
        self.assertIn('"error"', r.body)
        r = await anon.get("/account", headers={"HX-Request": "true"})
        self.assertEqual((r.status, r.headers["hx-redirect"]), (200, "/login"))

    async def test_unauthenticated_state_changes_are_redirected_not_executed(self):
        anon = self.env.client()
        for path in ("/logout", "/account/sessions/revoke-others", "/license/verify", "/activate"):
            r = await anon.post(path, {})
            self.assertEqual(r.status, 303, path)

    async def test_dashboard_shell_content(self):
        c = self.env.client()
        await self.env.login(c)
        page = (await c.get("/")).body
        for text in ("Telegram Auto Poster", "Dashboard", "Telegram accounts", "Groups", "Schedules", "Posting jobs",
                     "Logs", "Account", "Log out", "License status", "Not connected yet", 'id="sidebar"'):
            self.assertIn(text, page)
        for path in ("/telegram", "/groups", "/schedules", "/jobs", "/logs", "/account"):
            r = await c.get(path)
            self.assertEqual(r.status, 200, path)
            self.assertIn("Telegram Auto Poster", r.body)

    async def test_user_data_is_isolated_by_authenticated_user_id(self):
        other_id = await self.env.add_user("member@example.com")
        conn = self.env.db.conn
        for i, owner in enumerate((self.env.admin_id, other_id, other_id)):
            conn.execute("INSERT INTO telegram_accounts (id, user_id, tg_user_id) VALUES (?,?,?)", (f"acc{i}", owner, i))
        conn.commit()
        admin, member = self.env.client(), self.env.client()
        await self.env.login(admin)
        await self.env.login(member, "member@example.com")
        count = lambda html: re.search(r"Telegram accounts</h3><p class=\"big\">(\d+)</p>", html).group(1)  # noqa: E731
        self.assertEqual(count((await admin.get("/")).body), "1")
        self.assertEqual(count((await member.get("/")).body), "2")

    async def test_account_page_and_session_management_only_touch_own_sessions(self):
        other_id = await self.env.add_user("member@example.com")
        a1, a2, m = self.env.client(), self.env.client(), self.env.client()
        for c in (a1, a2):
            await self.env.login(c)
        await self.env.login(m, "member@example.com")
        page = (await a1.get("/account")).body
        self.assertEqual(page.count("TestBrowser/1.0"), 2)  # own two sessions only
        self.assertIn("this session", page)
        self.assertNotIn("member@example.com", page)
        r = await a1.post("/account/sessions/revoke-others", {"csrf_token": await a1.token_from("/account")})
        self.assertEqual(r.status, 303)
        self.assertEqual((await a1.get("/account")).status, 200)
        self.assertEqual((await a2.get("/account")).status, 303)  # revoked
        self.assertEqual((await m.get("/account")).status, 200)  # someone else's session untouched

    async def test_role_permissions_are_enforced(self):
        await self.env.add_user("member@example.com")
        m = self.env.client()
        await self.env.login(m, "member@example.com")
        self.assertEqual((await m.get("/")).status, 200)
        self.assertEqual((await m.get("/activate")).status, 403)
        self.assertEqual((await m.post("/license/verify", {"csrf_token": await m.token_from("/")})).status, 403)


class ErrorHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start(activated=True, with_admin=True)
        self.c = self.env.client()
        await self.env.login(self.c)

    def assert_safe(self, body):
        for leak in ("Traceback", "File \"", ".py", "sqlite", "asyncpg", "Exception", "RuntimeError"):
            self.assertNotIn(leak, body)

    async def test_404_405_are_friendly_for_everyone(self):
        for client in (self.c, self.env.client()):
            r = await client.get("/no/such/page")
            self.assertEqual(r.status, 404)
            self.assertIn("Page not found", r.body)
            self.assert_safe(r.body)
        r = await self.c.request("DELETE", "/account", form={})
        self.assertEqual(r.status, 405)
        r = await self.c.get("/../../etc/passwd")
        self.assertEqual(r.status, 404)

    async def test_413_for_oversized_forms(self):
        r = await self.c.post("/login", {"email": "a" * 40000})
        self.assertEqual(r.status, 413)
        self.assert_safe(r.body)

    async def test_json_and_htmx_errors(self):
        r = await self.c.get("/nope", headers={"Accept": "application/json"})
        self.assertEqual(r.status, 404)
        self.assertEqual(r.headers["content-type"], "application/json")
        r = await self.c.post("/logout", {}, headers={"HX-Request": "true"})
        self.assertEqual((r.status, r.headers["hx-retarget"]), (403, "#flash"))

    async def test_internal_errors_show_a_reference_only_and_are_logged_redacted(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        self.addCleanup(logging.getLogger().removeHandler, handler)

        async def boom(request):
            raise RuntimeError("db exploded, password=hunter2 token=abc123")

        route = next(r for r in self.env.web._routes["/groups"].values())
        self.env.web._routes["/groups"]["GET"] = type(route)(route.name, "GET", route.path, boom, route.auth, route.permission)
        r = await self.c.get("/groups")
        self.assertEqual(r.status, 500)
        self.assertIn("Something went wrong", r.body)
        self.assertRegex(r.body, r"Reference: [0-9a-f]{8}")
        self.assert_safe(r.body)
        self.assertNotIn("hunter2", r.body)
        logged = stream.getvalue()
        self.assertIn("unhandled error", logged)
        self.assertNotIn("hunter2", logged)
        self.assertNotIn("abc123", logged)
        reference = re.search(r"Reference: ([0-9a-f]{8})", r.body).group(1)
        self.assertIn(reference, logged)  # support can find the matching log line

    async def test_429_carries_retry_after(self):
        anon = self.env.client()
        for _ in range(5):
            await self.env.login(anon, password="wrong-password-1")
        r = await self.env.login(anon)
        self.assertEqual(r.status, 429)
        self.assertIn("Too many attempts", r.body)
        self.assertIn("retry-after", r.headers)

    async def test_head_requests_have_no_body(self):
        r = await self.c.request("HEAD", "/")
        self.assertEqual((r.status, r.body), (200, ""))


class SecurityHeaderAndCookieTests(unittest.IsolatedAsyncioTestCase):
    async def test_headers_on_every_kind_of_response(self):
        env = await Env().start(activated=True, with_admin=True)
        c = env.client()
        replies = [await c.get("/login"), await c.get("/nope"), await c.get("/account"), await env.login(c), await c.get("/")]
        for r in replies:
            self.assertIn("default-src 'self'", r.headers["content-security-policy"])
            self.assertIn("script-src 'self'", r.headers["content-security-policy"])
            self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
            self.assertEqual(r.headers["x-content-type-options"], "nosniff")
            self.assertEqual(r.headers["x-frame-options"], "DENY")
            self.assertEqual(r.headers["cache-control"], "no-store")
            self.assertIn("strict-transport-security", r.headers)

    async def test_development_mode_uses_plain_cookie_names_without_secure(self):
        env = await Env().start(env_overrides={"APP_ENV": "development", "APP_URL": "http://localhost:8000"})
        env.settings  # noqa: B018
        c = env.client(origin="http://localhost:8000")
        r = await c.get("/setup")
        cookie = r.set_cookies[0]
        self.assertTrue(cookie.startswith("tap_csrf="))
        self.assertNotIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertNotIn("strict-transport-security", r.headers)

    async def test_production_csrf_cookie_flags(self):
        env = await Env().start()
        r = await env.client().get("/setup")
        cookie = r.set_cookies[0]
        self.assertTrue(cookie.startswith("__Host-tap_csrf="))
        for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
            self.assertIn(flag, cookie)

    async def test_proxy_headers_are_ignored_unless_trusted(self):
        env = await Env().start(with_admin=True, activated=True)
        c = env.client()
        for i in range(5):
            await env.login(c, password="wrong-password-1")
        # spoofing X-Forwarded-For must not dodge the limit when the proxy is not trusted
        r = await c.request("POST", "/login", form={"csrf_token": await c.token_from("/login"), "email": "admin@example.com",
                                                     "password": PASSWORD}, headers={"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(r.status, 429)
        trusted = await Env().start(with_admin=True, activated=True, env_overrides={"TRUST_PROXY_HEADERS": "true"})
        c2 = trusted.client()
        for _ in range(5):
            token = await c2.token_from("/login")
            await c2.post("/login", {"csrf_token": token, "email": "admin@example.com", "password": "wrong-password-1"},
                          headers={"X-Forwarded-For": "1.1.1.1"})
        other_client = await c2.post("/login", {"csrf_token": await c2.token_from("/login"), "email": "admin@example.com",
                                                "password": PASSWORD}, headers={"X-Forwarded-For": "9.9.9.9"})
        self.assertEqual(other_client.status, 303)  # per-address limits work behind a trusted proxy


class StaticAnalysisTests(unittest.TestCase):
    def test_templates_are_csp_clean_and_have_no_external_resources(self):
        for path in (WEB / "templates").rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\sstyle\s*=", path.name)  # no inline styles
            self.assertNotRegex(text, r"\son[a-z]+\s*=", path.name)  # no inline event handlers
            self.assertNotRegex(text, r"<script(?![^>]*\ssrc=)", path.name)  # no inline scripts
            self.assertNotRegex(text, r"(src|href)=\"https?://", path.name)  # nothing loaded from other hosts
            self.assertNotIn("|safe", text, path.name)  # autoescape never bypassed
            self.assertNotIn("autoescape false", text)

    def test_no_cdn_references_anywhere_in_the_front_end(self):
        for path in list((WEB / "static").rglob("*.js")) + list((WEB / "static").rglob("*.css")):
            if path.name.endswith(".min.js"):
                continue
            self.assertNotRegex(path.read_text(encoding="utf-8"), r"https?://(?!www\.w3\.org)", path.name)

    def test_htmx_is_loaded_from_the_local_static_folder_only(self):
        base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn('src="/static/vendor/htmx.min.js"', base)
        self.assertNotRegex(base, r"unpkg|jsdelivr|cdnjs|htmx\.org")
        self.assertIn('"allowEval":false', base)

    def test_vendored_htmx_file_matches_the_pinned_checksum(self):
        target = vendor_htmx.TARGET
        if not target.exists():
            if os.environ.get("REQUIRE_VENDORED_HTMX"):
                self.fail("htmx.min.js is missing but REQUIRE_VENDORED_HTMX is set (CI must vendor it first)")
            self.skipTest("htmx.min.js is not vendored in this build (the sandbox has no internet); "
                          "run: python scripts/vendor_htmx.py")
        self.assertEqual(target.parts[-5:], ("app", "web", "static", "vendor", "htmx.min.js"))
        digest = base64.b64encode(hashlib.sha384(target.read_bytes()).digest()).decode()
        self.assertEqual(digest, vendor_htmx.SHA384_B64)

    def test_vendor_script_rejects_mismatching_downloads(self):
        self.assertNotEqual(vendor_htmx.sha384_b64(b"not htmx"), vendor_htmx.SHA384_B64)

    def test_no_secrets_or_debug_output_in_step2_sources(self):
        root = WEB.parents[1]
        for path in list((root / "app").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"^\s*print\(", path.name) if path.name not in ("migrate.py",) else None
            self.assertNotRegex(text, r"(?i)(password|secret|api_hash)\s*=\s*[\"'][^\"']{8,}[\"']", path.name)
            self.assertNotIn("DEBUG = True", text)
            self.assertNotIn("eval(", text)
            self.assertNotIn("shell=True", text)


if __name__ == "__main__":
    unittest.main()
