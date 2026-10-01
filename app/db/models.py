"""Typed row models and status vocabularies.

The string values of the enums below are mirrored by CHECK constraints in
migrations/0001_initial.sql; tests/test_schema.py fails if the two drift apart.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, TypeVar


def new_id() -> str:
    return str(uuid.uuid4())


class AccountStatus(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    SESSION_EXPIRED = "session_expired"
    REVOKED = "revoked"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ChatType(str, Enum):
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"
    FORUM = "forum"


class PermissionStatus(str, Enum):
    UNKNOWN = "unknown"
    OK = "ok"
    NO_PERMISSION = "no_permission"
    UNAVAILABLE = "unavailable"
    RESTRICTED = "restricted"


class ScheduleKind(str, Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    CUSTOM = "custom"


class ScheduleStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class JobStatus(str, Enum):
    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    WAITING = "waiting"  # retry / FloodWait back-off
    POSTED = "posted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LoginState(str, Enum):
    CODE_SENT = "code_sent"
    PASSWORD_NEEDED = "password_needed"


class LogLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


T = TypeVar("T")


class _Row:
    @classmethod
    def from_record(cls: type[T], record: Mapping[str, Any]) -> T:
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in dict(record).items() if k in names})  # type: ignore[call-arg]


@dataclass
class User(_Row):
    id: str
    email: str
    password_hash: str
    is_admin: bool = False
    is_active: bool = True
    timezone: str = "UTC"
    display_name: str | None = None
    created_at: datetime | None = None


@dataclass
class TelegramAccount(_Row):
    id: str
    user_id: str
    tg_user_id: int
    status: str = AccountStatus.CONNECTED.value
    username: str | None = None
    first_name: str | None = None
    phone_masked: str | None = None


@dataclass
class TelegramGroup(_Row):
    id: str
    user_id: str
    account_id: str
    tg_chat_id: int
    title: str
    chat_type: str
    permission_status: str = PermissionStatus.UNKNOWN.value
    is_enabled: bool = False
    username: str | None = None
    permission_detail: str | None = None


@dataclass
class Post(_Row):
    id: str
    user_id: str
    body: str
    media: list = field(default_factory=list)
    title: str | None = None


@dataclass
class Schedule(_Row):
    id: str
    user_id: str
    post_id: str
    account_id: str
    kind: str
    timezone: str
    status: str = ScheduleStatus.ACTIVE.value
    run_at: datetime | None = None
    times_of_day: list = field(default_factory=list)
    days_of_week: list = field(default_factory=list)
    interval_minutes: int | None = None
    next_run_at: datetime | None = None


@dataclass
class PostingJob(_Row):
    id: str
    user_id: str
    account_id: str
    group_title: str
    text_snapshot: str
    scheduled_for: datetime
    next_attempt_at: datetime
    idempotency_key: str
    status: str = JobStatus.SCHEDULED.value
    attempts: int = 0
    max_attempts: int = 5
    schedule_id: str | None = None
    group_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
