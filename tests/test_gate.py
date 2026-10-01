import unittest

from tests.support import Clock, FakeLicenseServer, signing
from app.licensing.client import LicenseClient
from app.licensing.gate import LicenseGateMiddleware
from app.licensing.manager import LicenseManager
from app.licensing.store import MemoryLicenseStore

KEY = "TAP-ABCDE-FGHJK-MNPQR-STVWX"


async def run(app, path="/", headers=(), scope_type="http"):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    await app({"type": scope_type, "path": path, "headers": list(headers)}, receive, send)
    return sent


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        private, public = signing.generate_keypair()
        clock = Clock()
        server = FakeLicenseServer(private, clock)
        server.add_license(KEY)
        client = LicenseClient("https://license.example.com", transport=server.transport)
        self.manager = LicenseManager(store=MemoryLicenseStore(), client=client, public_key=public,
                                      product="telegram-auto-poster", installation_id="i", host="h.example.com",
                                      app_version="0.1.0", clock=clock)
        self.reached = []

        async def downstream(scope, receive, send):
            self.reached.append(scope.get("path", scope["type"]))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        self.gate = LicenseGateMiddleware(downstream, self.manager)

    async def test_blocks_dashboard_without_license_but_allows_health_and_static(self):
        sent = await run(self.gate, "/dashboard")
        self.assertEqual(sent[0]["status"], 303)
        self.assertEqual(dict(sent[0]["headers"])[b"location"], b"/activate")
        self.assertEqual(self.reached, [])
        for path in ("/health", "/static/style.css", "/login", "/activate", "/license/verify", "/setup", "/logout"):
            await run(self.gate, path)
        self.assertEqual(len(self.reached), 7)

    async def test_prefix_lookalike_is_not_exempt(self):
        for path in ("/healthz-admin", "/loginx", "/activate-evil", "/static-files"):
            sent = await run(self.gate, path)
            self.assertEqual(sent[0]["status"], 303, path)

    async def test_json_for_api_and_htmx_clients(self):
        sent = await run(self.gate, "/api/x", headers=[(b"accept", b"application/json")])
        self.assertIn(b"application/json", dict(sent[0]["headers"])[b"content-type"])
        self.assertEqual(sent[0]["status"], 503)
        self.assertIn(b'"license_required"', sent[1]["body"])
        sent = await run(self.gate, "/x", headers=[(b"hx-request", b"true")])
        self.assertEqual(dict(sent[0]["headers"])[b"hx-redirect"], b"/activate")

    async def test_allows_everything_once_activated(self):
        await self.manager.activate(KEY)
        sent = await run(self.gate, "/dashboard")
        self.assertEqual(sent[0]["status"], 200)

    async def test_websocket_is_closed_and_lifespan_passes_through(self):
        sent = await run(self.gate, "/ws", scope_type="websocket")
        self.assertEqual(sent[0]["type"], "websocket.close")
        self.reached.clear()

        async def noop(*args):
            return {"type": "lifespan.startup"}

        await self.gate({"type": "lifespan"}, noop, noop)  # no path key, must pass straight through
        self.assertEqual(self.reached, ["lifespan"])

    async def test_page_never_shows_license_secrets(self):
        await self.manager.activate(KEY)
        self.manager._record = self.manager._record.with_(payload_json="{}")  # tampered
        sent = await run(self.gate, "/dashboard")
        self.assertNotIn(KEY.encode(), repr(sent).encode())


if __name__ == "__main__":
    unittest.main()
