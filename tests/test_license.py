import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests.support import Clock, FakeLicenseServer, signing
from app.licensing import protocol
from app.licensing.client import LicenseClient, LicenseRejected, LicenseResponseInvalid, LicenseUnreachable
from app.licensing.manager import CLOCK_TOLERANCE, LicenseManager, LicenseState
from app.licensing.store import MemoryLicenseStore

KEY = "TAP-ABCDE-FGHJK-MNPQR-STVWX"
DAY = 86400


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.private, self.public = signing.generate_keypair()
        self.clock = Clock()
        self.server = FakeLicenseServer(self.private, self.clock)
        self.server.add_license(KEY)
        self.store = MemoryLicenseStore()

    def manager(self, *, installation="inst-1", host="poster.example.com", store=None, transport=None,
                public=None, enforcement=True) -> LicenseManager:
        client = LicenseClient("https://license.example.com", transport=transport or self.server.transport)
        return LicenseManager(store=store or self.store, client=client, public_key=public or self.public,
                              product="telegram-auto-poster", installation_id=installation, host=host,
                              app_version="0.1.0", enforcement=enforcement, clock=self.clock)


class ActivationTests(Base):
    async def test_not_activated_initially(self):
        m = self.manager()
        await m.load()
        self.assertEqual(m.status().state, LicenseState.NOT_ACTIVATED)
        self.assertFalse(m.status().enabled)

    async def test_activate_success_accepts_loosely_typed_key(self):
        m = self.manager()
        status = await m.activate("tap abcde-fghjk mnpqr stvwx")
        self.assertEqual(status.state, LicenseState.ACTIVE)
        self.assertTrue(status.enabled)
        self.assertEqual(self.store.record.license_key, KEY)
        self.assertEqual(self.server.calls[-1][1]["license_key"], KEY)

    async def test_invalid_keys_are_rejected(self):
        m = self.manager()
        with self.assertRaises(LicenseRejected) as ctx:
            await m.activate("hello")
        self.assertEqual(ctx.exception.code, "invalid_key")
        with self.assertRaises(LicenseRejected) as ctx:
            await m.activate("TAP-AAAAA-AAAAA-AAAAA-AAAAA")  # well-formed but unknown
        self.assertEqual(ctx.exception.code, "invalid_key")
        self.assertIsNone(self.store.record)

    async def test_activation_limit_and_transfer(self):
        await self.manager(installation="inst-1", store=MemoryLicenseStore()).activate(KEY)
        second = self.manager(installation="inst-2", store=MemoryLicenseStore())
        with self.assertRaises(LicenseRejected) as ctx:
            await second.activate(KEY)
        self.assertEqual(ctx.exception.code, "activation_limit")
        self.server.release(KEY, "inst-1")  # seller resets / transfers
        self.assertEqual((await second.activate(KEY)).state, LicenseState.ACTIVE)

    async def test_same_installation_may_reactivate(self):
        await self.manager().activate(KEY)
        self.assertEqual((await self.manager(store=MemoryLicenseStore()).activate(KEY)).state, LicenseState.ACTIVE)

    async def test_product_mismatch(self):
        self.server.add_license("TAP-22222-33333-44444-55555", product="other-product")
        with self.assertRaises(LicenseRejected) as ctx:
            await self.manager().activate("TAP-22222-33333-44444-55555")
        self.assertEqual(ctx.exception.code, "product_mismatch")

    async def test_revoked_key_cannot_activate(self):
        self.server.revoke(KEY)
        with self.assertRaises(LicenseRejected):
            await self.manager().activate(KEY)

    async def test_response_signed_with_wrong_key_is_rejected(self):
        other_private, _ = signing.generate_keypair()
        self.server.private_key = other_private
        with self.assertRaises(LicenseResponseInvalid):
            await self.manager().activate(KEY)
        self.assertIsNone(self.store.record)

    async def test_replayed_response_is_rejected(self):
        captured = {}

        async def recording(path, body):
            captured["resp"] = self.server.handle(path, body)
            return captured["resp"]

        await self.manager(transport=recording).activate(KEY)

        async def replay(path, body):
            return captured["resp"]  # old, validly signed, but for an old nonce

        with self.assertRaises(LicenseResponseInvalid):
            await self.manager(transport=replay, store=MemoryLicenseStore()).activate(KEY)

    async def test_response_for_another_installation_is_rejected(self):
        async def cross(path, body):
            return self.server.handle(path, {**body, "installation_id": "someone-else"})

        with self.assertRaises(LicenseResponseInvalid):
            await self.manager(transport=cross).activate(KEY)

    async def test_server_unreachable_on_activation(self):
        self.server.down = True
        with self.assertRaises(LicenseUnreachable):
            await self.manager().activate(KEY)

    async def test_enforcement_disabled_for_development(self):
        m = self.manager(enforcement=False)
        self.assertTrue(m.status().enabled)


class VerificationTests(Base):
    async def asyncSetUp(self):
        self.m = self.manager()
        await self.m.activate(KEY)

    async def test_offline_grace_then_verification_required(self):
        self.server.down = True
        self.clock.advance(2 * DAY)
        self.assertEqual((await self.m.ensure_fresh()).state, LicenseState.GRACE)
        self.assertTrue(self.m.status().enabled)
        self.assertEqual(self.store.record.last_error, "unreachable")
        self.clock.advance(6 * DAY)  # 8 days since last contact, grace is 7
        self.assertEqual((await self.m.ensure_fresh()).state, LicenseState.VERIFY_REQUIRED)
        self.assertFalse(self.m.status().enabled)

    async def test_recovers_when_server_returns(self):
        self.server.down = True
        self.clock.advance(3 * DAY)
        await self.m.ensure_fresh()
        self.server.down = False
        self.clock.advance(10 * 60)  # after the retry back-off
        self.assertEqual((await self.m.ensure_fresh()).state, LicenseState.ACTIVE)
        self.assertEqual(self.store.record.last_error, "")

    async def test_no_hammering_when_server_is_down(self):
        self.server.down = True
        self.clock.advance(2 * DAY)
        await self.m.ensure_fresh()
        before = len(self.server.calls)
        self.clock.advance(30)
        await self.m.ensure_fresh()
        self.assertEqual(len(self.server.calls), before)

    async def test_fresh_license_does_not_call_server(self):
        before = len(self.server.calls)
        self.clock.advance(60)
        await self.m.ensure_fresh()
        self.assertEqual(len(self.server.calls), before)

    async def test_revocation_and_reactivation(self):
        self.server.revoke(KEY)
        self.clock.advance(13 * 3600)  # half of the verify interval
        self.assertEqual((await self.m.ensure_fresh()).state, LicenseState.REVOKED)
        self.assertFalse(self.m.status().enabled)
        self.server.reactivate(KEY)
        self.assertEqual((await self.m.verify()).state, LicenseState.ACTIVE)

    async def test_transfer_deactivates_old_installation(self):
        self.server.release(KEY, "inst-1")
        self.assertEqual((await self.m.verify()).state, LicenseState.NOT_ACTIVATED)

    async def test_unsigned_refusal_never_switches_a_customer_off(self):
        self.server.unsigned_error = (403, {"error": "revoked", "message": "forged by a man in the middle"})
        status = await self.m.verify()
        self.assertEqual(status.state, LicenseState.ACTIVE)
        self.assertEqual(self.store.record.last_error, "revoked")

    async def test_garbage_signature_on_verify_is_ignored(self):
        async def bad(path, body):
            status, data = self.server.handle(path, body)
            data["signature"] = protocol.b64url_encode(b"x" * 64)
            return status, data

        m = self.manager(transport=bad)
        await m.load()
        self.assertEqual((await m.verify()).state, LicenseState.ACTIVE)

    async def test_expiry(self):
        self.server.licenses[KEY]["expires_at"] = int(self.clock()) + 5 * DAY
        await self.m.verify()
        self.clock.advance(4 * DAY)
        self.server.down = True  # even offline, the signed expiry date applies
        self.assertNotEqual(self.m.status().state, LicenseState.EXPIRED)
        self.clock.advance(2 * DAY)
        self.assertEqual(self.m.status().state, LicenseState.EXPIRED)


class TamperTests(Base):
    async def asyncSetUp(self):
        self.m = self.manager()
        await self.m.activate(KEY)

    async def _edit(self, **changes):
        self.store.record = self.store.record.with_(**changes)
        await self.m.load()

    async def test_editing_payload_breaks_signature(self):
        payload = json.loads(self.store.record.payload_json)
        payload["verify_interval_seconds"] = 10**9
        payload["offline_grace_seconds"] = 10**9
        await self._edit(payload_json=json.dumps(payload))
        self.assertEqual(self.m.status().state, LicenseState.TAMPERED)

    async def test_flipping_revoked_to_active_is_detected(self):
        self.server.revoke(KEY)
        await self.m.verify()
        payload = json.loads(self.store.record.payload_json)
        payload["status"] = "active"
        await self._edit(payload_json=json.dumps(payload))
        self.assertEqual(self.m.status().state, LicenseState.TAMPERED)

    async def test_editing_local_timestamps_cannot_extend_grace(self):
        self.clock.advance(30 * DAY)
        await self._edit(last_verified_at=int(self.clock()), last_attempt_at=int(self.clock()))
        self.assertEqual(self.m.status().state, LicenseState.VERIFY_REQUIRED)

    async def test_garbage_and_signature_edits(self):
        good = self.store.record  # captured before any edit
        await self._edit(payload_json="not json")
        self.assertEqual(self.m.status().state, LicenseState.TAMPERED)
        await self._edit(payload_json=good.payload_json)  # restoring the genuine data works again
        self.assertEqual(self.m.status().state, LicenseState.ACTIVE)
        await self._edit(signature=protocol.b64url_encode(b"y" * 64))
        self.assertEqual(self.m.status().state, LicenseState.TAMPERED)

    async def test_clock_rollback_is_detected(self):
        self.clock.advance(2 * 3600)
        await self.m.ensure_fresh()  # persists the high-water mark
        self.clock.advance(-DAY)
        self.assertEqual(self.m.status().state, LicenseState.TAMPERED)
        self.assertIn("clock", self.m.status().detail)

    async def test_small_clock_adjustments_are_tolerated(self):
        self.clock.advance(3600)
        await self.m.ensure_fresh()
        self.clock.advance(-(CLOCK_TOLERANCE - 60))
        self.assertTrue(self.m.status().enabled)

    async def test_copied_database_on_another_installation_or_host(self):
        for kwargs in ({"installation": "inst-2"}, {"host": "other.example.com"}):
            with self.subTest(kwargs=kwargs):
                other = self.manager(**kwargs)
                await other.load()
                self.assertEqual(other.status().state, LicenseState.MISMATCH)

    async def test_signature_from_a_different_key_is_not_trusted(self):
        _, other_public = signing.generate_keypair()
        other = self.manager(public=other_public)
        await other.load()
        self.assertEqual(other.status().state, LicenseState.TAMPERED)


class ProtocolTests(unittest.TestCase):
    def test_canonical_json_matches_between_client_and_server(self):
        payload = {"b": 1, "a": ["ü", None, True], "c": {"z": 1, "y": 2}}
        self.assertEqual(protocol.canonical_json(payload), signing.canonical_json(payload))

    def test_signature_roundtrip_and_tamper(self):
        private, public = signing.generate_keypair()
        signed = signing.sign_payload(private, {"a": 1})
        self.assertTrue(protocol.verify_signature(public, signed["payload"], signed["signature"]))
        self.assertFalse(protocol.verify_signature(public, {"a": 2}, signed["signature"]))
        self.assertFalse(protocol.verify_signature("", {"a": 1}, signed["signature"]))

    def test_generated_keys_are_valid_and_unique(self):
        keys = {signing.generate_license_key() for _ in range(200)}
        self.assertEqual(len(keys), 200)
        for k in keys:
            self.assertEqual(protocol.normalize_license_key(k), k)

    def test_normalize(self):
        self.assertEqual(protocol.normalize_license_key(" tap-abcde fghjk-mnpqr-stvwx "), KEY)
        for bad in ("", "abc", "XYZ-ABCDE-FGHJK-MNPQR-STVWX", KEY + "A"):
            with self.assertRaises(ValueError):
                protocol.normalize_license_key(bad)

    def test_client_url_rules(self):
        LicenseClient("https://license.example.com")
        LicenseClient("http://127.0.0.1:9000")
        with self.assertRaises(ValueError):
            LicenseClient("http://license.example.com")
        with self.assertRaises(ValueError):
            LicenseClient("ftp://x")


class RealHttpTests(Base):
    """End-to-end over a real socket using the client's built-in urllib transport."""

    def _serve(self):
        server = self.server

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status, data = server.handle(self.path, body)
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def _real_manager(self, url):
        return LicenseManager(store=self.store, client=LicenseClient(url, timeout=3), public_key=self.public,
                              product="telegram-auto-poster", installation_id="inst-1", host="poster.example.com",
                              app_version="0.1.0", clock=self.clock)

    async def test_activate_verify_and_refusal_over_http(self):
        m = self._real_manager(self._serve())
        self.assertEqual((await m.activate(KEY)).state, LicenseState.ACTIVE)
        self.assertEqual((await m.verify()).state, LicenseState.ACTIVE)
        with self.assertRaises(LicenseRejected) as ctx:
            await self._real_manager(self._serve()).activate("TAP-22222-33333-44444-55555")
        self.assertEqual(ctx.exception.code, "invalid_key")

    async def test_connection_refused_is_unreachable(self):
        m = self._real_manager("http://127.0.0.1:1")
        with self.assertRaises(LicenseUnreachable):
            await m.activate(KEY)


if __name__ == "__main__":
    unittest.main()
