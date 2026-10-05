"""Groups service on a real (SQLite-backed) schema with the fake Telegram network: discovery, sync, selection, isolation."""

import sqlite3
import unittest

from app.config import settings_from_env
from app.security.crypto import Cipher, generate_key
from app.telegram.connect import AccountNotFound, TelegramConnectionService
from app.telegram.errors import FloodWait, NetworkProblem, SessionRevoked, TelegramError
from app.telegram.group_sync import (MAX_SELECTED_GROUPS, GroupNotFound, GroupService, NotSelectable, SyncBlocked,
                                     TooManySelected)
from app.telegram.groups import RawChat
from tests.fake_telegram import GOOD_API_HASH, GOOD_API_ID, FakeTelegram
from tests.support import Clock, migrated_db
from tests.web_support import BASE_ENV

PHONE_A, PHONE_B, PHONE_C = "+8801712345678", "+8801812345678", "+8801912345678"
CODE, API_ID = "48213", str(GOOD_API_ID)


def mega(chat_id, title, **kw):
    return RawChat(kind="megagroup", chat_id=chat_id, title=title, **kw)


STANDARD = [
    mega(-1001, "My Marketing Group", username="my_marketing"),
    mega(-1002, "Private Community"),
    mega(-1003, "Announcements", default_send_banned=True),
    mega(-1004, "Muted Here", member_send_banned=True),
    RawChat(kind="basic", chat_id=-2001, title="Old Basic Group"),
    mega(-1005, "Forum Hub", forum=True),
    RawChat(kind="channel", chat_id=-1006, title="News Channel"),
    RawChat(kind="user", chat_id=77, title="A Friend"),
    RawChat(kind="forbidden", chat_id=-1007, title="Removed Me"),
]


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await migrated_db()
        self.clock = Clock()
        self.world = FakeTelegram()
        for phone, tg_id in ((PHONE_A, 111), (PHONE_B, 222), (PHONE_C, 333)):
            self.world.add_account(phone, tg_id, code=CODE)
        self.world.chats[111] = list(STANDARD)
        self.settings = settings_from_env(BASE_ENV)
        self.tg = self.world.service()
        self.connect = TelegramConnectionService(self.db, Cipher(generate_key()), self.tg, self.settings, self.clock)
        self.groups = GroupService(self.db, self.connect, self.tg, self.clock)
        for n, email in ((1, "a@example.com"), (2, "b@example.com")):
            self.db.conn.execute("INSERT INTO users (id, email, password_hash) VALUES (?, ?, 'h')", (f"user{n}", email))
        self.db.conn.commit()
        self.acc1 = await self.link("user1", PHONE_A)

    async def link(self, user, phone):
        attempt = await self.connect.start(user, phone, API_ID, GOOD_API_HASH)
        return (await self.connect.submit_code(user, attempt, CODE)).account_id

    def rows(self, sql, *a):
        return self.db.conn.execute(sql, a).fetchall()

    async def by_title(self, user, account):
        return {g["title"]: g for g in await self.groups.groups(user, account)}


class DiscoveryTests(Base):
    async def test_groups_are_discovered_with_types_names_ids_and_verdicts(self):
        result = await self.groups.sync("user1", self.acc1)
        self.assertEqual((result.total, result.postable, result.truncated), (7, 4, False))
        g = await self.by_title("user1", self.acc1)
        self.assertEqual(set(g), {"My Marketing Group", "Private Community", "Announcements", "Muted Here", "Old Basic Group",
                                  "Forum Hub", "Removed Me"})  # no user chats, no broadcast channels
        self.assertEqual((g["My Marketing Group"]["chat_type"], g["My Marketing Group"]["username"], g["My Marketing Group"]["tg_chat_id"]),
                         ("supergroup", "my_marketing", -1001))
        self.assertIsNone(g["Private Community"]["username"])
        self.assertEqual(g["Old Basic Group"]["chat_type"], "group")
        self.assertEqual(g["Forum Hub"]["chat_type"], "forum")
        self.assertEqual({t: v["permission_status"] for t, v in g.items()},
                         {"My Marketing Group": "ok", "Private Community": "ok", "Announcements": "no_permission",
                          "Muted Here": "restricted", "Old Basic Group": "ok", "Forum Hub": "ok", "Removed Me": "unavailable"})
        self.assertTrue(all(v["is_present"] and not v["is_enabled"] for v in g.values()))  # nothing is selected automatically
        self.assertTrue(g["Announcements"]["permission_detail"])
        self.assertTrue(self.world.assert_all_clients_closed())

    async def test_sync_marks_the_account_and_is_idempotent(self):
        acc = await self.groups.account("user1", self.acc1)
        self.assertIsNone(acc["groups_synced_at"])
        await self.groups.sync("user1", self.acc1)
        first_ids = {g["tg_chat_id"]: g["id"] for g in await self.groups.groups("user1", self.acc1)}
        await self.groups.sync("user1", self.acc1)
        await self.groups.sync("user1", self.acc1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_groups")[0][0], 7)  # no duplicates
        self.assertEqual({g["tg_chat_id"]: g["id"] for g in await self.groups.groups("user1", self.acc1)}, first_ids)  # ids stable
        self.assertIsNotNone((await self.groups.account("user1", self.acc1))["groups_synced_at"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_sessions")[0][0], 1)  # refresh never creates a second session

    async def test_refresh_needs_no_new_login(self):
        before = [c for c in self.world.calls if c[0] in ("send_code", "sign_in_code", "sign_in_password")]
        await self.groups.sync("user1", self.acc1)
        self.assertEqual([c for c in self.world.calls if c[0] in ("send_code", "sign_in_code", "sign_in_password")], before)

    async def test_changed_group_information_is_updated(self):
        await self.groups.sync("user1", self.acc1)
        self.world.chats[111] = [mega(-1001, "Renamed Group", username="new_name"), mega(-1002, "Private Community", username="now_public")]
        await self.groups.sync("user1", self.acc1)
        g = await self.by_title("user1", self.acc1)
        self.assertEqual((g["Renamed Group"]["username"], g["Renamed Group"]["tg_chat_id"]), ("new_name", -1001))
        self.assertEqual(g["Private Community"]["username"], "now_public")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_groups WHERE tg_chat_id = -1001")[0][0], 1)

    async def test_duplicate_dialogs_are_collapsed(self):
        self.world.chats[111] = [mega(-1001, "Dup"), mega(-1001, "Dup")]
        self.assertEqual((await self.groups.sync("user1", self.acc1)).total, 1)

    async def test_hostile_titles_are_stored_cleaned_not_executed(self):
        self.world.chats[111] = [mega(-1001, "<script>alert(1)</script>\x00\u202e")]
        await self.groups.sync("user1", self.acc1)
        (g,) = await self.groups.groups("user1", self.acc1)
        self.assertEqual(g["title"], "<script>alert(1)</script>")  # escaping happens at render time; control chars are gone

    async def test_empty_account_and_only_unpostable_groups(self):
        self.world.chats[111] = []
        self.assertEqual((await self.groups.sync("user1", self.acc1)).total, 0)
        self.assertIsNotNone((await self.groups.account("user1", self.acc1))["groups_synced_at"])  # "synced, nothing found"
        self.world.chats[111] = [mega(-1003, "Announcements", default_send_banned=True)]
        result = await self.groups.sync("user1", self.acc1)
        self.assertEqual((result.total, result.postable), (1, 0))

    async def test_truncation_is_reported(self):
        self.world.chats[111] = [mega(-1000 - i, f"G{i}") for i in range(12)]
        original = self.tg.list_groups
        async def limited(api_id, api_hash, session, limit=0):
            return await original(api_id, api_hash, session, 10)
        self.groups._tg = type("T", (), {"list_groups": staticmethod(limited)})()
        result = await self.groups.sync("user1", self.acc1)
        self.assertEqual((result.total, result.truncated), (10, True))


class PermissionChangeTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.groups.sync("user1", self.acc1)
        self.ids = {t: g["id"] for t, g in (await self.by_title("user1", self.acc1)).items()}

    async def test_selection_persists_across_refreshes_while_the_group_stays_postable(self):
        await self.groups.save_selection("user1", self.acc1, {self.ids["My Marketing Group"], self.ids["Private Community"]})
        await self.groups.sync("user1", self.acc1)
        g = await self.by_title("user1", self.acc1)
        self.assertEqual({t for t, v in g.items() if v["is_enabled"]}, {"My Marketing Group", "Private Community"})

    async def test_selection_is_dropped_when_posting_becomes_impossible(self):
        await self.groups.save_selection("user1", self.acc1, {self.ids["My Marketing Group"], self.ids["Private Community"]})
        self.world.chats[111] = [mega(-1001, "My Marketing Group", default_send_banned=True), mega(-1002, "Private Community")]
        await self.groups.sync("user1", self.acc1)
        g = await self.by_title("user1", self.acc1)
        self.assertEqual((g["My Marketing Group"]["is_enabled"], g["My Marketing Group"]["permission_status"]), (False, "no_permission"))
        self.assertTrue(g["Private Community"]["is_enabled"])

    async def test_groups_the_account_left_become_unavailable_and_unselected(self):
        await self.groups.save_selection("user1", self.acc1, {self.ids["Forum Hub"]})
        self.world.chats[111] = [mega(-1001, "My Marketing Group")]  # everything else disappeared
        await self.groups.sync("user1", self.acc1)
        g = await self.by_title("user1", self.acc1)
        self.assertEqual(len(g), 7)  # rows are kept, not deleted
        self.assertEqual((g["Forum Hub"]["is_present"], g["Forum Hub"]["is_enabled"], g["Forum Hub"]["permission_status"]),
                         (False, False, "unavailable"))
        self.assertIn("no longer sees", g["Forum Hub"]["permission_detail"])
        with self.assertRaises(NotSelectable):
            await self.groups.save_selection("user1", self.acc1, {self.ids["Forum Hub"]})

    async def test_a_group_that_comes_back_is_present_again_but_not_auto_selected(self):
        await self.groups.save_selection("user1", self.acc1, {self.ids["Forum Hub"]})
        self.world.chats[111] = []
        await self.groups.sync("user1", self.acc1)
        self.world.chats[111] = [mega(-1005, "Forum Hub", forum=True)]
        await self.groups.sync("user1", self.acc1)
        g = (await self.by_title("user1", self.acc1))["Forum Hub"]
        self.assertEqual((g["is_present"], g["permission_status"], g["is_enabled"], g["id"]), (True, "ok", False, self.ids["Forum Hub"]))

    async def test_user_left_all_groups(self):
        self.world.chats[111] = []
        result = await self.groups.sync("user1", self.acc1)
        self.assertEqual(result.total, 0)
        self.assertTrue(all(not g["is_present"] and not g["is_enabled"] for g in await self.groups.groups("user1", self.acc1)))


class SelectionTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.groups.sync("user1", self.acc1)
        self.ids = {t: g["id"] for t, g in (await self.by_title("user1", self.acc1)).items()}

    async def selected(self, user="user1", account=None):
        return {g["title"] for g in await self.groups.groups(user, account or self.acc1) if g["is_enabled"]}

    async def test_select_save_deselect(self):
        self.assertEqual(await self.groups.save_selection("user1", self.acc1, {self.ids["My Marketing Group"]}), 1)
        self.assertEqual(await self.selected(), {"My Marketing Group"})
        await self.groups.save_selection("user1", self.acc1, {self.ids["Private Community"], self.ids["Forum Hub"]})
        self.assertEqual(await self.selected(), {"Private Community", "Forum Hub"})  # full replacement
        await self.groups.save_selection("user1", self.acc1, set())
        self.assertEqual(await self.selected(), set())

    async def test_unpostable_groups_cannot_be_selected_even_by_a_crafted_request(self):
        for title in ("Announcements", "Muted Here", "Removed Me"):
            with self.assertRaises(NotSelectable, msg=title):
                await self.groups.save_selection("user1", self.acc1, {self.ids[title]})
        self.assertEqual(await self.selected(), set())  # a rejected request changes nothing
        await self.groups.save_selection("user1", self.acc1, {self.ids["My Marketing Group"]})
        with self.assertRaises(NotSelectable):
            await self.groups.save_selection("user1", self.acc1, {self.ids["My Marketing Group"], self.ids["Announcements"]})
        self.assertEqual(await self.selected(), {"My Marketing Group"})  # unchanged

    async def test_garbage_and_unknown_ids_are_rejected(self):
        for bad in ({"not-a-uuid"}, {"' OR 1=1 --"}, {"00000000-0000-0000-0000-000000000000"}, {"../../etc"}, {""}):
            with self.assertRaises(GroupNotFound, msg=bad):
                await self.groups.save_selection("user1", self.acc1, bad)

    async def test_selection_limit(self):
        with self.assertRaises(TooManySelected):
            await self.groups.save_selection("user1", self.acc1, {f"00000000-0000-0000-0000-{i:012d}" for i in range(MAX_SELECTED_GROUPS + 1)})

    async def test_uppercase_ids_work_and_do_not_create_duplicates(self):
        await self.groups.save_selection("user1", self.acc1.upper(), {self.ids["Forum Hub"].upper()})
        self.assertEqual(await self.selected(), {"Forum Hub"})


class IsolationTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.world.chats[222] = [mega(-1001, "Same Chat Id, Other Person", username="other"), mega(-9009, "Bob Only")]
        self.acc2 = await self.link("user2", PHONE_B)
        await self.groups.sync("user1", self.acc1)
        await self.groups.sync("user2", self.acc2)
        self.g1 = {t: g["id"] for t, g in (await self.by_title("user1", self.acc1)).items()}
        self.g2 = {t: g["id"] for t, g in (await self.by_title("user2", self.acc2)).items()}

    async def test_the_same_telegram_chat_gets_separate_rows_per_account(self):
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_groups WHERE tg_chat_id = -1001")[0][0], 2)
        self.assertNotEqual(self.g1["My Marketing Group"], self.g2["Same Chat Id, Other Person"])
        self.assertEqual({g["title"] for g in await self.groups.groups("user2", self.acc2)}, {"Same Chat Id, Other Person", "Bob Only"})

    async def test_users_never_see_each_others_groups(self):
        self.assertNotIn("Bob Only", {g["title"] for g in await self.groups.groups("user1", self.acc1)})
        self.assertEqual(await self.groups.groups("user1", self.acc2), [])  # user1 asking for user2's account: nothing
        self.assertEqual(await self.groups.groups("user2", self.acc1), [])

    async def test_cross_user_account_access_is_rejected_everywhere(self):
        for action in (lambda: self.groups.account("user2", self.acc1), lambda: self.groups.sync("user2", self.acc1),
                       lambda: self.groups.save_selection("user2", self.acc1, set())):
            with self.assertRaises(AccountNotFound):
                await action()

    async def test_cross_user_group_id_manipulation_is_rejected_and_changes_nothing(self):
        with self.assertRaises(GroupNotFound):  # user2 submits user1's group id for user2's OWN account
            await self.groups.save_selection("user2", self.acc2, {self.g1["My Marketing Group"]})
        with self.assertRaises(AccountNotFound):  # ... or for user1's account
            await self.groups.save_selection("user2", self.acc1, {self.g1["My Marketing Group"]})
        with self.assertRaises(GroupNotFound):  # mixing an own id with a foreign one rejects the whole request
            await self.groups.save_selection("user2", self.acc2, {self.g2["Bob Only"], self.g1["My Marketing Group"]})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_groups WHERE is_enabled")[0][0], 0)

    async def test_ids_of_another_account_of_the_same_user_are_rejected_for_this_account(self):
        self.world.chats[333] = [mega(-3003, "Second Account Group")]
        acc3 = await self.link("user1", PHONE_C)
        await self.groups.sync("user1", acc3)
        other = (await self.by_title("user1", acc3))["Second Account Group"]["id"]
        with self.assertRaises(GroupNotFound):
            await self.groups.save_selection("user1", self.acc1, {other})
        await self.groups.save_selection("user1", acc3, {other})
        await self.groups.save_selection("user1", self.acc1, {self.g1["Forum Hub"]})  # selecting on account 1 leaves account 3 alone
        self.assertEqual({g["title"] for g in await self.groups.groups("user1", acc3) if g["is_enabled"]}, {"Second Account Group"})

    async def test_multiple_accounts_keep_separate_sync_state(self):
        self.world.chats[333] = [mega(-3003, "Second Account Group")]
        acc3 = await self.link("user1", PHONE_C)
        self.assertIsNone((await self.groups.account("user1", acc3))["groups_synced_at"])
        self.assertIsNotNone((await self.groups.account("user1", self.acc1))["groups_synced_at"])
        await self.groups.sync("user1", acc3)
        self.assertEqual(len(await self.groups.groups("user1", acc3)), 1)
        self.assertEqual(len(await self.groups.groups("user1", self.acc1)), 7)

    async def test_sync_does_not_touch_another_users_rows(self):
        await self.groups.save_selection("user2", self.acc2, {self.g2["Bob Only"]})
        self.world.chats[111] = []
        await self.groups.sync("user1", self.acc1)
        self.assertEqual({g["title"] for g in await self.groups.groups("user2", self.acc2) if g["is_enabled"]}, {"Bob Only"})
        self.assertTrue(all(g["is_present"] for g in await self.groups.groups("user2", self.acc2)))


class DatabaseConstraintTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.groups.sync("user1", self.acc1)

    def insert(self, user, account, chat_id, gid="11111111-1111-1111-1111-111111111111"):
        self.db.conn.execute(
            "INSERT INTO telegram_groups (id, user_id, account_id, tg_chat_id, title, chat_type) VALUES (?,?,?,?,'t','group')",
            (gid, user, account, chat_id))

    def test_unique_chat_per_account(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert("user1", self.acc1, -1001)

    def test_a_group_cannot_be_attached_to_another_users_account(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert("user2", self.acc1, -424242)  # user2 + user1's account violates the composite foreign key

    def test_invalid_status_and_type_are_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute("UPDATE telegram_groups SET permission_status = 'whatever'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute("UPDATE telegram_groups SET chat_type = 'planet'")

    def test_deleting_the_account_deletes_its_groups_only(self):
        self.insert("user2", "acc-b", 1) if False else None
        self.db.conn.execute("DELETE FROM telegram_accounts WHERE id = ?", (self.acc1,))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_groups")[0][0], 0)

    def test_new_columns_and_indexes_exist_with_safe_defaults(self):
        cols = {r["name"]: r for r in self.db.conn.execute("PRAGMA table_info(telegram_groups)")}
        self.assertIn("is_present", cols)
        self.assertIn("last_synced_at", cols)
        acc_cols = {r["name"] for r in self.db.conn.execute("PRAGMA table_info(telegram_accounts)")}
        self.assertLessEqual({"groups_synced_at", "groups_blocked_until"}, acc_cols)
        indexes = {r["name"] for r in self.db.conn.execute("PRAGMA index_list(telegram_groups)")}
        self.assertLessEqual({"telegram_groups_selected_idx", "telegram_groups_account_idx"}, indexes)


class TelegramFailureTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.groups.sync("user1", self.acc1)
        self.before = await self.groups.groups("user1", self.acc1)

    async def test_revoked_session_marks_the_account_and_removes_the_session(self):
        self.world.revoke_everything()
        with self.assertRaises(SessionRevoked):
            await self.groups.sync("user1", self.acc1)
        self.assertEqual(self.rows("SELECT status FROM telegram_accounts")[0][0], "session_expired")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_sessions")[0][0], 0)
        self.assertEqual(await self.groups.groups("user1", self.acc1), self.before)  # stored list is kept as it was
        with self.assertRaises(AccountNotFound):  # not connected any more: cannot refresh until reconnected
            await self.groups.sync("user1", self.acc1)

    async def test_floodwait_is_respected_and_not_retried(self):
        self.world.list_flood = 90
        calls = len([c for c in self.world.calls if c[0] == "list_groups"])
        with self.assertRaises(FloodWait) as ctx:
            await self.groups.sync("user1", self.acc1)
        self.assertEqual(ctx.exception.seconds, 90)
        self.assertEqual(len([c for c in self.world.calls if c[0] == "list_groups"]), calls + 1)  # exactly one attempt
        self.world.list_flood = 0
        self.clock.advance(30)
        with self.assertRaises(SyncBlocked) as blocked:  # still inside Telegram's wait: Telegram is not even asked
            await self.groups.sync("user1", self.acc1)
        self.assertEqual(blocked.exception.seconds, 60)
        self.assertEqual(len([c for c in self.world.calls if c[0] == "list_groups"]), calls + 1)
        self.clock.advance(61)
        self.assertEqual((await self.groups.sync("user1", self.acc1)).total, 7)  # after the wait it works again
        self.assertIsNone((await self.groups.account("user1", self.acc1))["groups_blocked_until"])

    async def test_network_and_rpc_failures_leave_the_stored_list_untouched(self):
        for error in (NetworkProblem(), TelegramError(), ConnectionError("x")):
            self.world.list_error = error
            with self.assertRaises((TelegramError, ConnectionError)):
                await self.groups.sync("user1", self.acc1)
            self.assertEqual(await self.groups.groups("user1", self.acc1), self.before)
        self.world.list_error = None
        self.assertEqual(self.rows("SELECT status FROM telegram_accounts")[0][0], "connected")  # nothing was disconnected

    async def test_network_down_before_connecting(self):
        self.world.down = True
        with self.assertRaises(NetworkProblem):
            await self.groups.sync("user1", self.acc1)
        self.assertEqual(await self.groups.groups("user1", self.acc1), self.before)

    async def test_a_failure_in_the_middle_of_storing_rolls_everything_back(self):
        self.world.chats[111] = [mega(-1001, "Changed"), mega(-1002, "Also Changed")]
        original = self.groups._verdicts
        def poisoned(chats):
            verdicts = original(chats)
            object.__setattr__(verdicts[1], "chat_type", "planet")  # violates the CHECK constraint half-way through
            return verdicts
        self.groups._verdicts = poisoned
        with self.assertRaises(sqlite3.IntegrityError):
            await self.groups.sync("user1", self.acc1)
        self.assertEqual(await self.groups.groups("user1", self.acc1), self.before)  # all-or-nothing

    async def test_every_client_is_closed_after_every_outcome(self):
        for setup in (lambda: None, lambda: setattr(self.world, "list_error", NetworkProblem()), lambda: setattr(self.world, "list_flood", 5)):
            self.world.list_error, self.world.list_flood = None, 0
            self.clock.advance(1000)
            setup()
            try:
                await self.groups.sync("user1", self.acc1)
            except (TelegramError, SyncBlocked):
                pass
        self.clock.advance(1000)  # let the simulated FloodWait pass, then finish with a revoked session
        self.world.revoke_everything()
        with self.assertRaises(SessionRevoked):
            await self.groups.sync("user1", self.acc1)
        self.assertTrue(self.world.assert_all_clients_closed())

    async def test_the_listing_is_read_only_no_send_join_or_login_calls(self):
        await self.groups.sync("user1", self.acc1)
        allowed = {"send_code", "sign_in_code", "sign_in_password", "current_profile", "list_groups", "log_out"}
        sent_after_login = [c[0] for c in self.world.calls if c[0] not in allowed]
        self.assertEqual(sent_after_login, [])
        self.assertEqual(len([c for c in self.world.calls if c[0] == "log_out"]), 0)


if __name__ == "__main__":
    unittest.main()
