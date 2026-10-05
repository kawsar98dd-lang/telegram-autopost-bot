"""Pure, Telethon-free logic for Telegram groups: what a chat is and whether the account may post in it.

``RawChat`` is a neutral snapshot of one dialog. ``raw_chat_from_entity`` builds it from a Telethon entity by class and
attribute NAMES (so this module needs no Telethon import and is testable with look-alike objects). ``assess`` turns the
snapshot into a posting verdict using only the rights Telegram itself reports for the connected account.
Nothing here talks to the network, and nothing here tries to get around a restriction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MAX_DIALOGS = 1500
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]")  # control characters and bidi overrides

# permission_status values (mirrored by the CHECK constraint of telegram_groups)
OK, NO_PERMISSION, UNAVAILABLE, RESTRICTED = "ok", "no_permission", "unavailable", "restricted"

REASONS = {
    "forbidden": "Telegram denies this account access to the chat (removed, banned or deleted).",
    "left": "This account has left the group.",
    "deactivated": "The group was deactivated or upgraded to a supergroup.",
    "platform": "Telegram restricts this chat for this account or region.",
    "banned": "This account is banned from the group.",
    "muted": "This account is muted or restricted in the group.",
    "default": "Members of this group are not allowed to send messages.",
    "gone": "This account no longer sees the group (left, removed or deleted).",
}


@dataclass(frozen=True)
class RawChat:
    kind: str                      # 'basic' | 'megagroup' | 'channel' | 'forbidden' | 'user' | 'other'
    chat_id: int
    title: str = ""
    username: str = ""
    left: bool = False
    deactivated: bool = False      # deactivated or migrated to a supergroup
    creator: bool = False
    admin: bool = False
    default_send_banned: bool = False   # "members may not send messages" for everyone
    member_send_banned: bool = False    # restriction that applies to this account only
    member_banned_until: datetime | None = None
    member_view_banned: bool = False    # this account is banned from the group
    platform_restricted: bool = False
    forum: bool = False


@dataclass(frozen=True)
class Assessed:
    chat_id: int
    title: str
    username: str
    chat_type: str          # 'group' | 'supergroup' | 'forum'
    status: str             # ok | no_permission | unavailable | restricted
    detail: str


def clean_title(title: Any) -> str:
    text = _CONTROL.sub("", str(title or "")).strip()
    return (text or "(untitled group)")[:255]


def clean_username(value: Any) -> str:
    text = str(value or "").strip().lstrip("@")
    return text if _USERNAME.match(text) else ""


def raw_chat_from_entity(entity: Any, chat_id: int) -> RawChat:
    """Neutral snapshot of a Telethon entity (User / Chat / Channel / ChatForbidden / ChannelForbidden)."""
    name = type(entity).__name__
    if name == "User":
        return RawChat("user", chat_id)
    if name in ("Chat", "ChatForbidden"):
        kind = "basic" if name == "Chat" else "forbidden"
    elif name in ("Channel", "ChannelForbidden"):
        megagroup = bool(getattr(entity, "megagroup", False))
        if not megagroup:
            return RawChat("channel", chat_id)  # broadcast channels are not group targets
        kind = "megagroup" if name == "Channel" else "forbidden"
    else:
        return RawChat("other", chat_id)

    username = getattr(entity, "username", None)
    if not username:
        for item in getattr(entity, "usernames", None) or []:
            if getattr(item, "active", True) and getattr(item, "username", None):
                username = item.username
                break
    default_rights = getattr(entity, "default_banned_rights", None)
    member_rights = getattr(entity, "banned_rights", None)
    until = getattr(member_rights, "until_date", None) if member_rights is not None else None
    return RawChat(
        kind=kind, chat_id=chat_id, title=clean_title(getattr(entity, "title", "")), username=clean_username(username),
        left=bool(getattr(entity, "left", False)),
        deactivated=bool(getattr(entity, "deactivated", False)) or getattr(entity, "migrated_to", None) is not None,
        creator=bool(getattr(entity, "creator", False)),
        admin=getattr(entity, "admin_rights", None) is not None,
        default_send_banned=bool(getattr(default_rights, "send_messages", False)),
        member_send_banned=bool(getattr(member_rights, "send_messages", False)),
        member_banned_until=until if isinstance(until, datetime) else None,
        member_view_banned=bool(getattr(member_rights, "view_messages", False)),
        platform_restricted=bool(getattr(entity, "restricted", False)),
        forum=bool(getattr(entity, "forum", False)),
    )


def _active(until: datetime | None, now: datetime) -> bool:
    """A restriction with an end date in the past no longer applies; no end date means 'until further notice'."""
    if until is None:
        return True
    moment = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
    return moment > now


def assess(raw: RawChat, now: datetime | None = None) -> Assessed | None:
    """Verdict for one chat, or None if it is not a group target at all (private chats, broadcast channels)."""
    if raw.kind not in ("basic", "megagroup", "forbidden"):
        return None
    now = now or datetime.now(timezone.utc)
    chat_type = "group" if raw.kind == "basic" else ("forum" if raw.forum else "supergroup")

    def verdict(status: str, reason: str = "") -> Assessed:
        return Assessed(raw.chat_id, clean_title(raw.title), clean_username(raw.username), chat_type, status,
                        REASONS[reason] if reason else "")

    if raw.kind == "forbidden":
        return verdict(UNAVAILABLE, "forbidden")
    if raw.left:
        return verdict(UNAVAILABLE, "left")
    if raw.deactivated:
        return verdict(UNAVAILABLE, "deactivated")
    if raw.platform_restricted:
        return verdict(RESTRICTED, "platform")
    if raw.member_view_banned and _active(raw.member_banned_until, now):
        return verdict(RESTRICTED, "banned")
    if raw.member_send_banned and _active(raw.member_banned_until, now):
        return verdict(RESTRICTED, "muted")
    if raw.creator or raw.admin:  # owners and admins are not bound by the group-wide default restriction
        return verdict(OK)
    if raw.default_send_banned:
        return verdict(NO_PERMISSION, "default")
    return verdict(OK)
