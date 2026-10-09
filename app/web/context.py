"""Everything the web layer needs, assembled once (and easy to fake in tests)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import branding
from ..auth.ratelimit import RateLimiter
from ..auth.sessions import SessionService
from ..auth.users import UserService
from ..config import Settings
from ..licensing.manager import LicenseManager
from ..security.crypto import Cipher
from ..telegram.connect import TelegramConnectionService
from ..telegram.group_sync import GroupService
from ..posts.service import PostService
from ..posts.storage import make_storage
from ..scheduler.service import ScheduleService
from ..telegram.service import TelegramClientService

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _fmt_time(value: Any) -> str:
    """Accepts unix seconds, datetime objects (PostgreSQL) and ISO strings (SQLite)."""
    if not value:
        return "-"
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(value, (int, float)):
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(value)))
    text = str(value)
    if text.isdigit():
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(text)))
    try:
        return _fmt_time(datetime.fromisoformat(text))
    except ValueError:
        return "-"


def _fmt_local(value: Any, tz_name: str) -> str:
    """An instant shown in the schedule's own timezone, e.g. '2026-10-10 09:00 (Asia/Dhaka)'."""
    from zoneinfo import ZoneInfo

    from ..scheduler.timeutil import to_dt

    try:
        moment = to_dt(value)
        if moment is None:
            return "-"
        return moment.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M") + f" ({tz_name})"
    except Exception:  # noqa: BLE001 - a display helper never breaks a page
        return _fmt_time(value)


def make_template_env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))
    env.filters["utc"] = _fmt_time
    env.filters["localtime"] = _fmt_local
    env.globals.update(app_name=branding.APP_NAME, app_description=branding.APP_DESCRIPTION,
                       support_url=branding.SUPPORT_URL)
    return env


@dataclass
class AppContext:
    settings: Settings
    db: Any
    license: LicenseManager
    users: UserService
    sessions: SessionService
    limiter: RateLimiter
    telegram_connect: TelegramConnectionService
    groups: GroupService
    posts: PostService
    schedules: ScheduleService
    clock: Callable[[], float] = time.time
    templates: Environment = field(default_factory=make_template_env)

    @property
    def session_cookie(self) -> str:
        return "__Host-tap_session" if self.settings.cookie_secure else "tap_session"

    @property
    def csrf_cookie(self) -> str:
        return "__Host-tap_csrf" if self.settings.cookie_secure else "tap_csrf"


def build_context(settings: Settings, db: Any, manager: LicenseManager, *, cipher: Cipher | None = None,
                  telegram: TelegramClientService | None = None, clock=time.time) -> AppContext:
    cipher = cipher or Cipher(settings.session_encryption_key)
    telegram = telegram or TelegramClientService()
    connect = TelegramConnectionService(db, cipher, telegram, settings, clock)
    posts = PostService(db, connect, make_storage(settings, db, clock), settings, clock)
    return AppContext(
        settings=settings, db=db, license=manager, users=UserService(db),
        sessions=SessionService(db, clock, settings.session_idle_minutes * 60, settings.session_max_hours * 3600),
        limiter=RateLimiter(db), telegram_connect=connect, groups=GroupService(db, connect, telegram, clock),
        posts=posts, schedules=ScheduleService(db, posts, clock), clock=clock,
    )
