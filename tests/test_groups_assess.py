"""Pure logic: turning Telegram dialogs into 'can this account post here?' verdicts."""

import unittest
from datetime import datetime, timedelta, timezone

from app.telegram.groups import (NO_PERMISSION, OK, RESTRICTED, UNAVAILABLE, RawChat, assess, clean_title, clean_username,
                                 raw_chat_from_entity)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def chat(**kw) -> RawChat:
    return RawChat(**{"kind": "megagroup", "chat_id": -1001234567890, "title": "Marketing", **kw})


class AssessTests(unittest.TestCase):
    def test_ordinary_member_of_an_open_supergroup_can_post(self):
        a = assess(chat(), NOW)
        self.assertEqual((a.status, a.chat_type, a.detail), (OK, "supergroup", ""))

    def test_basic_group_forum_and_types(self):
        self.assertEqual(assess(chat(kind="basic", chat_id=-555), NOW).chat_type, "group")
        self.assertEqual(assess(chat(forum=True), NOW).chat_type, "forum")
        self.assertEqual(assess(chat(), NOW).chat_type, "supergroup")

    def test_public_and_private_groups_are_both_targets(self):
        public, private = assess(chat(username="my_public_group"), NOW), assess(chat(), NOW)
        self.assertEqual((public.username, public.status), ("my_public_group", OK))
        self.assertEqual((private.username, private.status), ("", OK))

    def test_group_wide_ban_on_sending_blocks_ordinary_members(self):
        a = assess(chat(default_send_banned=True), NOW)
        self.assertEqual(a.status, NO_PERMISSION)
        self.assertIn("not allowed to send", a.detail)

    def test_admins_and_owners_are_not_bound_by_the_group_wide_restriction(self):
        for kw in ({"admin": True}, {"creator": True}):
            self.assertEqual(assess(chat(default_send_banned=True, **kw), NOW).status, OK, kw)

    def test_member_specific_restriction_beats_everything_but_forbidden_states(self):
        a = assess(chat(member_send_banned=True), NOW)
        self.assertEqual((a.status, "muted" in a.detail), (RESTRICTED, True))
        self.assertEqual(assess(chat(member_send_banned=True, admin=True), NOW).status, RESTRICTED)

    def test_temporary_restriction_expires(self):
        active = chat(member_send_banned=True, member_banned_until=NOW + timedelta(days=1))
        expired = chat(member_send_banned=True, member_banned_until=NOW - timedelta(minutes=1))
        naive_expired = chat(member_send_banned=True, member_banned_until=(NOW - timedelta(hours=2)).replace(tzinfo=None))
        self.assertEqual(assess(active, NOW).status, RESTRICTED)
        self.assertEqual(assess(expired, NOW).status, OK)
        self.assertEqual(assess(naive_expired, NOW).status, OK)

    def test_banned_from_the_group(self):
        a = assess(chat(member_view_banned=True), NOW)
        self.assertEqual((a.status, "banned" in a.detail), (RESTRICTED, True))

    def test_left_deactivated_forbidden_and_platform_restricted(self):
        self.assertEqual(assess(chat(left=True), NOW).status, UNAVAILABLE)
        self.assertEqual(assess(chat(kind="basic", deactivated=True), NOW).status, UNAVAILABLE)
        self.assertEqual(assess(chat(kind="forbidden"), NOW).status, UNAVAILABLE)
        a = assess(chat(platform_restricted=True), NOW)
        self.assertEqual((a.status, "restricts" in a.detail), (RESTRICTED, True))

    def test_unavailable_beats_admin_rights(self):
        self.assertEqual(assess(chat(left=True, admin=True, creator=True), NOW).status, UNAVAILABLE)

    def test_things_that_are_not_group_targets_are_excluded(self):
        for kind in ("user", "channel", "other"):
            self.assertIsNone(assess(chat(kind=kind), NOW), kind)

    def test_details_are_fixed_texts_not_telegram_supplied(self):
        for kw in ({"left": True}, {"default_send_banned": True}, {"member_send_banned": True}, {"kind": "forbidden"}):
            self.assertLess(len(assess(chat(title="x <b>y</b>", **kw), NOW).detail), 100)


class SanitisingTests(unittest.TestCase):
    def test_titles_lose_control_and_bidi_characters_and_are_bounded(self):
        self.assertEqual(clean_title("Deals\x00 \u202eevil\x07"), "Deals evil")
        self.assertEqual(clean_title("   "), "(untitled group)")
        self.assertEqual(clean_title(None), "(untitled group)")
        self.assertEqual(len(clean_title("x" * 1000)), 255)

    def test_usernames_must_look_like_usernames(self):
        self.assertEqual(clean_username("@valid_name1"), "valid_name1")
        for bad in ("a", "has space", "<script>", "x" * 65, "ünï", None, ""):
            self.assertEqual(clean_username(bad), "", bad)


# look-alikes of Telethon's classes: the converter works on class and attribute NAMES only
class User: ...
class Chat:
    def __init__(self, **kw): self.__dict__.update({"title": "Basic", "left": False, "creator": False, **kw})
class Channel:
    def __init__(self, **kw): self.__dict__.update({"title": "Super", "megagroup": True, **kw})
class ChatForbidden:
    title = "Gone"
class ChannelForbidden:
    title = "Gone too"
    megagroup = True
class Rights:
    def __init__(self, **kw): self.__dict__.update(kw)
class UsernameEntry:
    def __init__(self, username, active=True): self.username, self.active = username, active


class EntityConversionTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(raw_chat_from_entity(User(), 5).kind, "user")
        self.assertEqual(raw_chat_from_entity(Chat(), -5).kind, "basic")
        self.assertEqual(raw_chat_from_entity(Channel(), -100).kind, "megagroup")
        self.assertEqual(raw_chat_from_entity(Channel(megagroup=False, broadcast=True), -100).kind, "channel")
        self.assertEqual(raw_chat_from_entity(ChatForbidden(), -7).kind, "forbidden")
        self.assertEqual(raw_chat_from_entity(ChannelForbidden(), -100).kind, "forbidden")
        self.assertEqual(raw_chat_from_entity(object(), 1).kind, "other")

    def test_rights_are_read_from_telegrams_fields(self):
        raw = raw_chat_from_entity(Channel(
            title="Biz", username="biz_group", forum=True,
            default_banned_rights=Rights(send_messages=True),
            banned_rights=Rights(send_messages=True, view_messages=False, until_date=datetime(2030, 1, 1, tzinfo=timezone.utc)),
            admin_rights=None, creator=False, left=False), -1009)
        self.assertEqual((raw.title, raw.username, raw.forum, raw.default_send_banned, raw.member_send_banned, raw.member_view_banned),
                         ("Biz", "biz_group", True, True, True, False))
        self.assertEqual(raw.member_banned_until.year, 2030)
        self.assertFalse(raw.admin)
        self.assertTrue(raw_chat_from_entity(Channel(admin_rights=Rights()), -1).admin)
        self.assertTrue(raw_chat_from_entity(Chat(creator=True), -1).creator)

    def test_migrated_basic_group_and_deactivated(self):
        self.assertTrue(raw_chat_from_entity(Chat(migrated_to=object()), -1).deactivated)
        self.assertTrue(raw_chat_from_entity(Chat(deactivated=True), -1).deactivated)
        self.assertFalse(raw_chat_from_entity(Chat(migrated_to=None), -1).deactivated)

    def test_additional_usernames_are_used_when_there_is_no_primary_one(self):
        raw = raw_chat_from_entity(Channel(username=None, usernames=[UsernameEntry("old_one", False), UsernameEntry("live_one")]), -1)
        self.assertEqual(raw.username, "live_one")

    def test_end_to_end_verdicts_from_entities(self):
        cases = [(Chat(default_banned_rights=Rights(send_messages=True)), NO_PERMISSION),
                 (Chat(), OK), (Chat(left=True), UNAVAILABLE),
                 (Channel(admin_rights=Rights(), default_banned_rights=Rights(send_messages=True)), OK),
                 (Channel(restricted=True), RESTRICTED), (ChannelForbidden(), UNAVAILABLE)]
        for entity, expected in cases:
            self.assertEqual(assess(raw_chat_from_entity(entity, -9), NOW).status, expected, type(entity).__name__)

    def test_malformed_values_do_not_crash(self):
        weird = Channel(title=None, username=12345, banned_rights=Rights(send_messages=None, until_date="tomorrow"))
        raw = raw_chat_from_entity(weird, -1)
        self.assertEqual((raw.title, raw.username, raw.member_banned_until), ("(untitled group)", "", None))


if __name__ == "__main__":
    unittest.main()
