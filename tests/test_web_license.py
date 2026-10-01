import re
import unittest

from tests.web_support import KEY, PASSWORD, Env

LIMITED = "TAP-22222-33333-44444-55555"


class ActivationPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start(with_admin=True)
        self.c = self.env.client()
        self.assertEqual((await self.env.login(self.c)).location, "/activate")  # admin lands on activation

    async def activate(self, key, client=None, **kw):
        client = client or self.c
        return await client.post("/activate", {"csrf_token": await client.token_from("/activate"), "license_key": key}, **kw)

    async def test_page_shows_not_activated(self):
        page = await self.c.get("/activate")
        self.assertEqual(page.status, 200)
        self.assertIn("Not activated", page.body)
        self.assertIn("Activate your license", page.body)

    async def test_gate_sends_the_app_to_activation_until_a_license_is_active(self):
        self.assertEqual((await self.c.get("/")).location, "/activate")
        self.assertEqual((await self.c.get("/groups")).location, "/activate")
        r = await self.c.get("/", headers={"Accept": "application/json"})
        self.assertEqual(r.status, 503)
        self.assertEqual((await self.c.get("/health")).status, 404)  # /health belongs to the FastAPI host, not this app
        self.assertEqual((await self.activate(KEY)).location, "/")
        self.assertEqual((await self.c.get("/")).status, 200)
        self.assertIn("Active", (await self.c.get("/")).body)

    async def test_safe_messages_for_every_failure(self):
        self.env.server.add_license(LIMITED)
        self.env.server.licenses[LIMITED]["installations"]["someone-else"] = "other.example.com"
        self.env.server.add_license("TAP-AAAAA-BBBBB-CCCCC-DDDDD", expires_at=int(self.env.clock()) - 10)
        self.env.server.add_license("TAP-EEEEE-FFFFF-GGGGG-HHHHH", product="other-product")
        revoked = "TAP-JJJJJ-KKKKK-LLLLL-MMMMM"
        self.env.server.add_license(revoked)
        self.env.server.revoke(revoked)
        cases = [
            ("garbage", 400, "not recognised"),
            ("TAP-ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ", 400, "not recognised"),
            ("TAP-EEEEE-FFFFF-GGGGG-HHHHH", 400, "different product"),
            (LIMITED, 409, "activation limit"),
            ("TAP-AAAAA-BBBBB-CCCCC-DDDDD", 403, "expired"),
            (revoked, 403, "revoked"),
        ]
        for key, status, needle in cases:
            r = await self.activate(key)
            self.assertEqual(r.status, status, key)
            self.assertIn(needle, r.body, key)
            self.assertNotIn(key, r.body)  # the submitted key is never echoed back
            self.assertNotIn("Traceback", r.body)
        self.assertFalse(self.env.manager.status().enabled)

    async def test_unreachable_and_invalid_server_answers(self):
        self.env.server.down = True
        r = await self.activate(KEY)
        self.assertEqual(r.status, 503)
        self.assertIn("could not be reached", r.body)
        self.assertNotIn("ConnectionError", r.body)
        self.env.server.down = False
        self.env.server.private_key = __import__("tests.support", fromlist=["signing"]).signing.generate_keypair()[0]
        r = await self.activate(KEY)
        self.assertEqual(r.status, 502)
        self.assertIn("could not be verified", r.body)

    async def test_server_supplied_text_is_never_shown(self):
        self.env.server.unsigned_error = (403, {"error": "weird_code", "message": "INTERNAL-SECRET-DETAIL sk_live_123"})
        r = await self.activate(KEY)
        self.assertEqual(r.status, 400)
        self.assertIn("could not be activated", r.body)
        self.assertNotIn("INTERNAL-SECRET-DETAIL", r.body)
        self.assertNotIn("sk_live_123", r.body)

    async def test_pages_never_reveal_the_license_key_or_signature(self):
        await self.activate(KEY)
        record = self.env.manager._record
        for path in ("/", "/activate", "/account", "/ui/license-card"):
            body = (await self.c.get(path)).body
            self.assertNotIn(KEY, body, path)
            self.assertNotIn(record.signature, body, path)
            found = [k for k in re.findall(r"TAP-[A-Z0-9]{5}(?:-[A-Z0-9]{5}){3}", body) if "XXXXX" not in k]
            self.assertEqual(found, [], path)  # no key-shaped string anywhere (only the input placeholder)

    async def test_only_administrators_can_activate(self):
        await self.activate(KEY)
        await self.env.add_user("member@example.com")
        member = self.env.client()
        await self.env.login(member, "member@example.com")
        self.assertEqual((await member.get("/")).status, 200)
        self.assertEqual((await member.get("/activate")).status, 403)
        token = await member.token_from("/")
        self.assertEqual((await member.post("/activate", {"csrf_token": token, "license_key": KEY})).status, 403)
        self.assertEqual((await member.post("/license/verify", {"csrf_token": token})).status, 403)
        self.assertNotIn('href="/activate"', (await member.get("/")).body)

    async def test_unauthenticated_users_cannot_reach_activation(self):
        anon = self.env.client()
        self.assertEqual((await anon.get("/activate")).location, "/login?next=%2Factivate")
        r = await anon.post("/activate", {"license_key": KEY})
        self.assertEqual(r.status, 303)
        self.assertFalse(self.env.manager.status().enabled)

    async def test_activation_attempts_are_rate_limited(self):
        statuses = [(await self.activate("TAP-ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ")).status for _ in range(11)]
        self.assertEqual(statuses[:10], [400] * 10)
        self.assertEqual(statuses[10], 429)
        self.assertEqual((await self.activate(KEY)).status, 429)  # even a valid key waits

    async def test_htmx_activation_errors_are_swapped_into_the_flash_area(self):
        token = await self.c.token_from("/activate")
        r = await self.c.post("/activate", {"license_key": "TAP-ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ"},
                              headers={"HX-Request": "true", "X-CSRF-Token": token})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.headers["hx-retarget"], "#flash")
        self.assertNotIn("<html", r.body)
        ok = await self.c.post("/activate", {"license_key": KEY}, headers={"HX-Request": "true", "X-CSRF-Token": token})
        self.assertEqual((ok.status, ok.headers["hx-redirect"]), (200, "/"))


class OfflineGraceAndTamperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start(activated=True, with_admin=True)
        self.c = self.env.client()
        await self.env.login(self.c)

    async def fresh_login(self):
        c = self.env.client()  # sessions idle out long before the licence grace period does
        await self.env.login(c)
        return c

    async def test_grace_period_keeps_the_app_usable_then_verification_is_required(self):
        self.env.server.down = True
        self.env.clock.advance(2 * 86400)
        await self.env.manager.ensure_fresh()
        c = await self.fresh_login()
        page = await c.get("/")
        self.assertEqual(page.status, 200)
        self.assertIn("offline grace", page.body)
        self.assertIn("could not reach the license server", page.body)
        self.env.clock.advance(6 * 86400)
        await self.env.manager.ensure_fresh()
        c = await self.fresh_login()
        self.assertEqual((await c.get("/")).location, "/activate")
        self.assertIn("Verification required", (await c.get("/activate")).body)
        self.env.server.down = False
        token = await c.token_from("/activate")
        r = await c.post("/license/verify", {"csrf_token": token})
        self.assertEqual((r.status, r.location), (303, "/"))
        self.assertEqual((await c.get("/")).status, 200)

    async def test_clock_rollback_blocks_the_app(self):
        self.env.clock.advance(3 * 3600)
        await self.env.manager.ensure_fresh()
        self.env.clock.advance(-86400)
        self.assertEqual((await self.c.get("/")).location, "/activate")
        self.assertIn("Check failed", (await self.c.get("/activate")).body)

    async def test_revocation_takes_effect_and_can_be_undone(self):
        self.env.server.revoke(KEY)
        await self.env.manager.verify()
        self.assertEqual((await self.c.get("/")).location, "/activate")
        self.assertIn("Disabled", (await self.c.get("/activate")).body)
        self.env.server.reactivate(KEY)
        token = await self.c.token_from("/activate")
        await self.c.post("/license/verify", {"csrf_token": token})
        self.assertEqual((await self.c.get("/")).status, 200)

    async def test_status_card_fragment_for_htmx_polling(self):
        r = await self.c.get("/ui/license-card", headers={"HX-Request": "true"})
        self.assertEqual(r.status, 200)
        self.assertTrue(r.body.lstrip().startswith("<section"))
        self.assertIn("Active", r.body)


if __name__ == "__main__":
    unittest.main()
