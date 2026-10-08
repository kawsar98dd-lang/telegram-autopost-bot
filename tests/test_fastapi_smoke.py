"""Runs the REAL FastAPI application (lifespan, mounts, middleware) whenever FastAPI + httpx are installed.

The sandbox used for development has neither, so this file is skipped there; CI installs them and sets
REQUIRE_FASTAPI_TESTS=1, which turns a skip into a failure.
"""

import asyncio
import os
import re
import unittest

from tests.web_support import PASSWORD, Env

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_FASTAPI = False

BASE = "https://poster.example.com"


def build(**env_kwargs):
    env = asyncio.run(Env().start(**env_kwargs))
    from app.web.main import create_app

    return env, create_app(env.settings, db=env.db, license_manager=env.manager, clock=env.clock)


class RequireFastApi(unittest.TestCase):
    def test_fastapi_is_installed_when_required(self):
        if os.environ.get("REQUIRE_FASTAPI_TESTS") and not HAVE_FASTAPI:
            self.fail("REQUIRE_FASTAPI_TESTS is set but fastapi/httpx are not installed")


@unittest.skipUnless(HAVE_FASTAPI, "fastapi/httpx not installed in this environment (environment limitation)")
class FastApiSmokeTests(unittest.TestCase):
    def test_app_is_a_real_fastapi_instance_with_expected_mounts(self):
        _, app = build()
        self.assertIsInstance(app, FastAPI)
        paths = {getattr(r, "path", "") for r in app.routes}
        self.assertIn("/health", paths)
        self.assertIn("/health/ready", paths)
        self.assertIn("/static", paths)

    def test_startup_serve_and_shutdown(self):
        env, app = build(activated=True, with_admin=True)
        with TestClient(app, base_url=BASE) as client:  # runs lifespan start-up ... and shutdown on exit
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            self.assertEqual(client.get("/health/ready").json(), {"status": "ready"})
            css = client.get("/static/style.css")
            self.assertEqual(css.status_code, 200)
            self.assertIn("text/css", css.headers["content-type"])
            page = client.get("/login")
            self.assertEqual(page.status_code, 200)
            # the security pipeline is connected to the mounted pages
            self.assertIn("default-src 'self'", page.headers["content-security-policy"])
            self.assertEqual(page.headers["x-frame-options"], "DENY")
            missing = client.get("/no/such/page")
            self.assertEqual(missing.status_code, 404)
            self.assertIn("Page not found", missing.text)
            self.assertNotIn("Traceback", missing.text)

    def test_full_login_through_real_fastapi(self):
        env, app = build(activated=True, with_admin=True)
        with TestClient(app, base_url=BASE, follow_redirects=False) as client:
            self.assertEqual(client.get("/").status_code, 303)  # anonymous -> login
            token = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/login").text).group(1)
            denied = client.post("/login", data={"email": "admin@example.com", "password": PASSWORD})
            self.assertEqual(denied.status_code, 403)  # CSRF enforced end to end
            ok = client.post("/login", data={"csrf_token": token, "email": "admin@example.com", "password": PASSWORD})
            self.assertEqual((ok.status_code, ok.headers["location"]), (303, "/"))
            self.assertEqual(client.get("/").status_code, 200)
            self.assertIn("Dashboard", client.get("/").text)

    def test_groups_pages_through_real_fastapi(self):
        env, app = build(activated=True, with_admin=True)
        with TestClient(app, base_url=BASE, follow_redirects=False) as client:
            self.assertEqual(client.get("/groups").status_code, 303)  # anonymous -> login
            token = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/login").text).group(1)
            client.post("/login", data={"csrf_token": token, "email": "admin@example.com", "password": PASSWORD})
            page = client.get("/groups")
            self.assertEqual(page.status_code, 200)
            self.assertIn("No Telegram account connected", page.text)
            self.assertNotIn("upcoming release", page.text)
            token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
            unknown = "00000000-0000-0000-0000-000000000abc"
            self.assertEqual(client.get(f"/groups/accounts/{unknown}").status_code, 404)  # unknown ids are plain 404s
            self.assertEqual(client.post(f"/groups/accounts/{unknown}/refresh", data={}).status_code, 403)  # CSRF enforced
            self.assertEqual(client.post(f"/groups/accounts/{unknown}/refresh", data={"csrf_token": token}).status_code, 404)

    def test_posts_and_image_upload_through_real_fastapi(self):
        from tests.posts_support import png

        env, app = build(activated=True, with_admin=True)
        with TestClient(app, base_url=BASE, follow_redirects=False) as client:
            self.assertEqual(client.get("/posts").status_code, 303)  # anonymous -> login
            token = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/login").text).group(1)
            client.post("/login", data={"csrf_token": token, "email": "admin@example.com", "password": PASSWORD})
            page = client.get("/posts")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Connect a Telegram account first", page.text)
            token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
            unknown = "00000000-0000-0000-0000-000000000abc"
            self.assertEqual(client.get(f"/posts/{unknown}").status_code, 404)
            self.assertEqual(client.get("/posts/not-a-uuid").status_code, 404)
            files = {"image": ("a.png", png(), "image/png")}
            self.assertEqual(client.post("/posts", data={"account_id": unknown, "body": "x"}, files=files).status_code, 403)  # CSRF
            r = client.post("/posts", data={"csrf_token": token, "account_id": unknown, "body": "x"}, files=files)
            self.assertEqual(r.status_code, 404)  # unknown account: a plain 404, multipart parsed by the real stack
            self.assertEqual(client.get(f"/posts/{unknown}/image").status_code, 404)

    def test_license_gate_is_connected(self):
        env, app = build(with_admin=True)  # no license activated
        with TestClient(app, base_url=BASE, follow_redirects=False) as client:
            r = client.get("/groups")
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/activate"))
            self.assertEqual(client.get("/health").status_code, 200)  # health stays reachable
            r = client.get("/groups", headers={"accept": "application/json"})
            self.assertEqual(r.status_code, 503)

    def test_first_run_setup_flow_through_real_fastapi(self):
        env, app = build()
        with TestClient(app, base_url=BASE, follow_redirects=False) as client:
            self.assertEqual(client.get("/login").headers["location"], "/setup")
            page = client.get("/setup")
            self.assertEqual(page.status_code, 200)
            token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
            r = client.post("/setup", data={"csrf_token": token, "email": "owner@example.com",
                                            "password": PASSWORD, "password_confirm": PASSWORD})
            self.assertEqual(r.status_code, 303)
            self.assertEqual(client.get("/setup").status_code, 404)


if __name__ == "__main__":
    unittest.main()
