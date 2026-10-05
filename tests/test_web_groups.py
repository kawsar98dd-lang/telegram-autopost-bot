import io
import logging
import re
import unittest

from app.security.redact import RedactingFormatter
from app.telegram.errors import NetworkProblem, TelegramError
from app.telegram.groups import RawChat
from tests.fake_telegram import GOOD_API_HASH, GOOD_API_ID
from tests.web_support import Env

PHONE_A, PHONE_B, PHONE_C = "+8801712345678", "+8801812345678", "+8801912345678"
CODE = "48213"
API_ID = str(GOOD_API_ID)


def mega(chat_id, title, **kw):
    return RawChat(kind="megagroup", chat_id=chat_id, title=title, **kw)


CHATS_A = [mega(-1001, "My Marketing Group", username="my_marketing"), mega(-1002, "Private Community"),
           mega(-1003, "Announcements", default_send_banned=True), RawChat(kind="basic", chat_id=-2001, title="Old Basic Group")]


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env().start(activated=True, with_admin=True)
        self.world = self.env.world
        self.world.add_account(PHONE_A, 111, code=CODE, first_name="Alice", username="alice")
        self.world.add_account(PHONE_B, 222, code=CODE, first_name="Bob", username="bob")
        self.world.add_account(PHONE_C, 333, code=CODE, first_name="Carol", username="carol")
        self.world.chats[111] = list(CHATS_A)
        self.world.chats[222] = [mega(-1001, "Bobs Chat With Same Id"), mega(-9009, "Bob Only")]
        self.member_id = await self.env.add_user("member@example.com")
        self.alice = self.env.client()
        self.bob = self.env.client()
        await self.env.login(self.alice)
        await self.env.login(self.bob, "member@example.com")
        self.acc_alice = await self.link(self.env.admin_id, PHONE_A)
        self.acc_bob = await self.link(self.member_id, PHONE_B)

    async def link(self, user_id, phone):
        connect = self.env.ctx.telegram_connect
        attempt = await connect.start(user_id, phone, API_ID, GOOD_API_HASH)
        return (await connect.submit_code(user_id, attempt, CODE)).account_id

    async def refresh(self, client, account_id):
        token = await client.token_from(f"/groups/accounts/{account_id}")
        return await client.post(f"/groups/accounts/{account_id}/refresh", {"csrf_token": token})

    async def save(self, client, account_id, group_ids, token_page=None, **extra):
        token = await client.token_from(token_page or f"/groups/accounts/{account_id}")  # the form of one's OWN page
        form = {"csrf_token": token, **{f"sel_{g}": "1" for g in group_ids}, **extra}
        return await client.post(f"/groups/accounts/{account_id}/save", form)

    def db_groups(self, account_id):
        return {r["title"]: dict(r) for r in self.env.db.conn.execute(
            "SELECT * FROM telegram_groups WHERE account_id = ?", (account_id,))}

    def gid(self, account_id, title):
        return self.db_groups(account_id)[title]["id"]


class PageTests(Base):
    async def test_the_placeholder_is_gone_and_the_page_is_functional(self):
        r = await self.alice.get("/groups")
        self.assertEqual((r.status, r.location), (303, f"/groups/accounts/{self.acc_alice}"))
        page = (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body
        for text in ("Groups", "Refresh groups", "Alice", "@alice", "No groups loaded yet"):
            self.assertIn(text, page)
        self.assertNotIn("upcoming release", page)

    async def test_anonymous_visitors_are_sent_to_login(self):
        anon = self.env.client()
        for path in ("/groups", f"/groups/accounts/{self.acc_alice}"):
            r = await anon.get(path)
            self.assertEqual((r.status, r.location.split("?")[0]), (303, "/login"), path)
        for path in (f"/groups/accounts/{self.acc_alice}/refresh", f"/groups/accounts/{self.acc_alice}/save"):
            self.assertEqual((await anon.post(path, {})).status, 303)

    async def test_no_account_connected(self):
        carol = await self.env.add_user("nobody@example.com")
        c = self.env.client()
        await self.env.login(c, "nobody@example.com")
        page = (await c.get("/groups")).body
        self.assertIn("No Telegram account connected", page)
        self.assertIn("/telegram/add", page)

    async def test_account_exists_but_is_not_connected(self):
        await self.env.ctx.telegram_connect.disconnect(self.env.admin_id, self.acc_alice)
        page = await self.alice.get("/groups")
        self.assertEqual(page.status, 200)
        self.assertIn("No connected Telegram account", page.body)
        self.assertIn("/telegram", page.body)
        direct = (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body
        self.assertIn("Telegram account not connected", direct)
        self.assertNotIn('name="sel_', direct)

    async def test_session_expired_state_is_explained(self):
        self.world.revoke_everything()
        await self.env.ctx.telegram_connect.check(self.env.admin_id, self.acc_alice)
        self.assertIn("expired or was ended", (await self.alice.get("/groups")).body)

    async def test_malformed_ids_are_plain_404s(self):
        for path in ("/groups/accounts/not-a-uuid", "/groups/accounts/" + "a" * 36, "/groups/accounts/../telegram",
                     f"/groups/accounts/{self.acc_alice}/../../x", "/groups/accounts/00000000-0000-0000-0000-000000000000"):
            self.assertEqual((await self.alice.get(path)).status, 404, path)


class RefreshAndSelectTests(Base):
    async def test_refresh_shows_real_groups_with_verdicts(self):
        r = await self.refresh(self.alice, self.acc_alice)
        self.assertEqual((r.status, r.location), (303, f"/groups/accounts/{self.acc_alice}?notice=refreshed"))
        page = (await self.alice.get(f"/groups/accounts/{self.acc_alice}", query="notice=refreshed")).body
        for text in ("Groups refreshed from Telegram.", "My Marketing Group", "Private Community", "Announcements", "Old Basic Group",
                     "Supergroup", "Public", "@my_marketing", "Private", "-1001", "Can post", "Cannot post", "Not selectable",
                     "4 groups", "3 can post", "0 selected", "Save selected groups"):
            self.assertIn(text, page, text)
        self.assertEqual(page.count('type="checkbox"'), 3)  # the non-postable group has no checkbox at all
        self.assertNotIn("Bobs Chat With Same Id", page)  # the other user's groups never appear
        self.assertNotIn("Bob Only", page)

    async def test_select_save_reload_and_deselect(self):
        await self.refresh(self.alice, self.acc_alice)
        marketing, private = self.gid(self.acc_alice, "My Marketing Group"), self.gid(self.acc_alice, "Private Community")
        r = await self.save(self.alice, self.acc_alice, [marketing])
        self.assertEqual((r.status, r.location), (303, f"/groups/accounts/{self.acc_alice}?notice=saved"))
        page = (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body
        self.assertIn("1 selected", page)
        self.assertRegex(page, rf'name="sel_{marketing}" value="1" checked')
        self.assertNotRegex(page, rf'name="sel_{private}" value="1" checked')
        await self.refresh(self.alice, self.acc_alice)  # a refresh keeps a still-valid selection
        self.assertIn("1 selected", (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body)
        await self.save(self.alice, self.acc_alice, [])  # deselect everything
        self.assertIn("0 selected", (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body)

    async def test_selection_is_persisted_in_the_database_for_the_right_user_and_account(self):
        await self.refresh(self.alice, self.acc_alice)
        await self.save(self.alice, self.acc_alice, [self.gid(self.acc_alice, "Private Community")])
        row = self.env.db.conn.execute("SELECT user_id, account_id, title FROM telegram_groups WHERE is_enabled").fetchall()
        self.assertEqual([tuple(r) for r in row], [(self.env.admin_id, self.acc_alice, "Private Community")])

    async def test_hostile_titles_are_escaped_in_the_page(self):
        self.world.chats[111] = [mega(-1001, '<img src=x onerror=alert(1)>"><script>alert(2)</script>')]
        await self.refresh(self.alice, self.acc_alice)
        page = (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body
        self.assertNotIn("<script>alert(2)", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;alert(2)", page)

    async def test_empty_states_after_a_refresh(self):
        self.world.chats[111] = []
        await self.refresh(self.alice, self.acc_alice)
        self.assertIn("No groups found", (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body)
        self.world.chats[111] = [mega(-1003, "Announcements", default_send_banned=True)]
        await self.refresh(self.alice, self.acc_alice)
        page = (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body
        self.assertIn("None of these groups currently allows", page)
        self.assertNotIn("Save selected groups", page)
        self.world.chats[111] = []  # the user left every group
        await self.refresh(self.alice, self.acc_alice)
        self.assertIn("Announcements", (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body)  # kept, unavailable
        self.assertIn("no longer sees", (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body)

    async def test_multiple_accounts_can_be_switched(self):
        acc_two = await self.link(self.env.admin_id, PHONE_C)
        self.world.chats[333] = [mega(-3003, "Carols Group")]
        page = (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body
        self.assertIn(f"/groups/accounts/{acc_two}", page)
        await self.refresh(self.alice, acc_two)
        self.assertIn("Carols Group", (await self.alice.get(f"/groups/accounts/{acc_two}")).body)
        self.assertNotIn("Carols Group", (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body)


class CrossUserTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.refresh(self.alice, self.acc_alice)
        await self.refresh(self.bob, self.acc_bob)
        await self.save(self.alice, self.acc_alice, [self.gid(self.acc_alice, "My Marketing Group")])
        self.alice_ids = {t: g["id"] for t, g in self.db_groups(self.acc_alice).items()}
        self.bob_ids = {t: g["id"] for t, g in self.db_groups(self.acc_bob).items()}

    def enabled(self):
        return sorted(r[0] for r in self.env.db.conn.execute("SELECT title FROM telegram_groups WHERE is_enabled"))

    async def test_another_users_account_page_refresh_and_save_are_404(self):
        token = await self.bob.token_from(f"/groups/accounts/{self.acc_bob}")
        self.assertEqual((await self.bob.get(f"/groups/accounts/{self.acc_alice}")).status, 404)
        self.assertEqual((await self.bob.post(f"/groups/accounts/{self.acc_alice}/refresh", {"csrf_token": token})).status, 404)
        self.assertEqual((await self.bob.post(f"/groups/accounts/{self.acc_alice}/save", {"csrf_token": token})).status, 404)
        self.assertEqual(self.enabled(), ["My Marketing Group"])  # untouched

    async def test_submitting_another_users_group_ids_cannot_select_or_deselect_them(self):
        for target in (self.acc_bob, self.acc_alice):
            r = await self.save(self.bob, target, [self.alice_ids["Private Community"]], token_page=f"/groups/accounts/{self.acc_bob}")
            self.assertEqual(r.status, 404, target)
        r = await self.save(self.bob, self.acc_bob, [self.bob_ids["Bob Only"], self.alice_ids["Private Community"]])
        self.assertEqual(r.status, 404)  # a mixed request is rejected as a whole
        self.assertEqual(self.enabled(), ["My Marketing Group"])
        self.assertNotIn("Bob Only", self.enabled())

    async def test_the_response_does_not_reveal_whether_the_foreign_id_exists(self):
        real = await self.save(self.bob, self.acc_bob, [self.alice_ids["Private Community"]])
        fake = await self.save(self.bob, self.acc_bob, ["11111111-2222-3333-4444-555555555555"])
        self.assertEqual((real.status, fake.status), (404, 404))
        self.assertEqual(real.body.replace(self.alice_ids["Private Community"], ""), fake.body)

    async def test_client_supplied_user_or_account_fields_are_ignored(self):
        r = await self.save(self.bob, self.acc_bob, [self.bob_ids["Bob Only"]], user_id=self.env.admin_id,
                            owner_account=self.acc_alice, owner="admin")
        self.assertEqual(r.status, 303)
        enabled = self.env.db.conn.execute("SELECT user_id, account_id, title FROM telegram_groups WHERE title = 'Bob Only' AND is_enabled").fetchall()
        self.assertEqual([tuple(x) for x in enabled], [(self.member_id, self.acc_bob, "Bob Only")])
        self.assertEqual(self.enabled(), ["Bob Only", "My Marketing Group"])

    async def test_each_page_only_lists_the_viewers_own_groups(self):
        a, b = (await self.alice.get(f"/groups/accounts/{self.acc_alice}")).body, (await self.bob.get(f"/groups/accounts/{self.acc_bob}")).body
        self.assertTrue("My Marketing Group" in a and "Bob Only" not in a)
        self.assertTrue("Bob Only" in b and "My Marketing Group" not in b)
        for html in (a, b):
            self.assertNotIn(self.alice_ids["Private Community"] if html is b else self.bob_ids["Bob Only"], html)

    async def test_the_same_telegram_chat_id_in_two_accounts_stays_separate(self):
        await self.save(self.bob, self.acc_bob, [self.bob_ids["Bobs Chat With Same Id"]])
        self.assertEqual(self.enabled(), ["Bobs Chat With Same Id", "My Marketing Group"])
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM telegram_groups WHERE tg_chat_id = -1001").fetchone()[0], 2)


class SecurityAndErrorTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.refresh(self.alice, self.acc_alice)

    async def test_state_changing_requests_need_a_csrf_token(self):
        gid = self.gid(self.acc_alice, "Private Community")
        for path, form in ((f"/groups/accounts/{self.acc_alice}/refresh", {}), (f"/groups/accounts/{self.acc_alice}/save", {f"sel_{gid}": "1"})):
            self.assertEqual((await self.alice.post(path, form)).status, 403)
            self.assertEqual((await self.alice.post(path, {**form, "csrf_token": "forged"})).status, 403)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM telegram_groups WHERE is_enabled").fetchone()[0], 0)

    async def test_get_requests_never_change_anything(self):
        for suffix in ("/refresh", "/save"):
            self.assertEqual((await self.alice.get(f"/groups/accounts/{self.acc_alice}{suffix}")).status, 405)

    async def test_crafted_selection_of_a_group_that_cannot_post_is_refused(self):
        r = await self.save(self.alice, self.acc_alice, [self.gid(self.acc_alice, "Announcements")])
        self.assertEqual(r.status, 409)
        self.assertIn("cannot be used for posting", r.body)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM telegram_groups WHERE is_enabled").fetchone()[0], 0)

    async def test_too_many_selected_groups(self):
        r = await self.save(self.alice, self.acc_alice, [f"00000000-0000-0000-0000-{i:012d}" for i in range(201)])
        self.assertEqual(r.status, 409)
        self.assertIn("at most 200", r.body)

    def assert_safe(self, body):
        for leak in ("Traceback", "File \"", ".py", "Telethon", "FloodWaitError", "RuntimeError", "sqlite", "asyncpg"):
            self.assertNotIn(leak, body)

    async def test_floodwait_shows_the_wait_and_blocks_early_retries(self):
        self.world.list_flood = 120
        r = await self.refresh(self.alice, self.acc_alice)
        self.assertEqual((r.status, r.headers["retry-after"]), (429, "120"))
        self.assertIn("wait 120 seconds", r.body)
        self.assert_safe(r.body)
        self.world.list_flood = 0
        calls = len([c for c in self.world.calls if c[0] == "list_groups"])
        again = await self.refresh(self.alice, self.acc_alice)
        self.assertEqual(again.status, 429)
        self.assertEqual(len([c for c in self.world.calls if c[0] == "list_groups"]), calls)  # Telegram was not asked again
        self.assertIn("Try again in", again.body)
        self.env.clock.advance(125)
        self.assertEqual((await self.refresh(self.alice, self.acc_alice)).status, 303)

    async def test_revoked_session_is_explained_and_the_account_is_flagged(self):
        self.world.revoke_everything()
        r = await self.refresh(self.alice, self.acc_alice)
        self.assertEqual(r.status, 409)
        self.assertIn("no longer accepts this session", r.body)
        self.assert_safe(r.body)
        self.assertEqual(self.env.db.conn.execute("SELECT status FROM telegram_accounts WHERE id = ?", (self.acc_alice,)).fetchone()[0], "session_expired")
        self.assertIn("expired or was ended", (await self.alice.get("/groups")).body)

    async def test_network_and_unexpected_telegram_errors_are_generic(self):
        self.world.list_error = NetworkProblem()
        r = await self.refresh(self.alice, self.acc_alice)
        self.assertEqual(r.status, 503)
        self.assertIn("temporarily unavailable", r.body)
        self.assert_safe(r.body)
        self.world.list_error = TelegramError("secret detail api_hash=abcdef0123456789abcdef0123456789")
        r = await self.refresh(self.alice, self.acc_alice)
        self.assertEqual(r.status, 502)
        self.assertNotIn("abcdef0123", r.body)
        self.assertNotIn("secret detail", r.body)
        self.assertIn("My Marketing Group", r.body)  # the previously stored list is still shown

    async def test_a_runtime_error_inside_the_service_becomes_a_500_with_a_reference_only(self):
        async def boom(*a, **k):
            raise RuntimeError("db exploded api_hash=0123456789abcdef0123456789abcdef")
        self.env.ctx.groups.sync = boom
        r = await self.refresh(self.alice, self.acc_alice)
        self.assertEqual(r.status, 500)
        self.assertRegex(r.body, r"Reference: [0-9a-f]{8}")
        self.assertNotIn("0123456789abcdef", r.body)

    async def test_refreshing_is_rate_limited_per_user(self):
        statuses = [(await self.refresh(self.alice, self.acc_alice)).status for _ in range(7)]  # one refresh already done in setUp
        self.assertEqual(statuses[:5], [303] * 5)
        self.assertEqual(statuses[5:], [429, 429])
        self.assertEqual((await self.refresh(self.bob, self.acc_bob)).status, 303)  # another user is not affected

    async def test_no_telegram_secret_reaches_pages_or_logs(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(name)s %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            await self.refresh(self.alice, self.acc_alice)
            self.world.revoke_everything()
            await self.refresh(self.alice, self.acc_alice)
            await self.alice.get(f"/groups/accounts/{self.acc_alice}")
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        usable_before = None
        for secret in (GOOD_API_HASH, PHONE_A, PHONE_A[1:], "gAAAA"):
            self.assertNotIn(secret, stream.getvalue(), secret)
            for reply in self.alice.log:
                self.assertNotIn(secret, reply.body, secret)
                self.assertNotIn(secret, " ".join(reply.headers.values()))
        self.assertIsNone(usable_before)


if __name__ == "__main__":
    unittest.main()
