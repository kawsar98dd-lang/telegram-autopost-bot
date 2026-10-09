"""Time and JSON helpers shared by the scheduler. All stored instants are UTC.

PostgreSQL (asyncpg) returns ``datetime`` objects and ``jsonb`` as text; the offline SQLite test database returns
strings for both. These helpers accept either, so the same service code runs on both.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

UTC = timezone.utc


def now_utc(clock: Callable[[], float]) -> datetime:
    return datetime.fromtimestamp(clock(), UTC)


def to_dt(value: Any) -> datetime | None:
    """datetime | ISO string | None -> timezone-aware UTC datetime | None (naive values are taken as UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip().replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def iso(moment: datetime) -> str:
    """Stable text form used inside idempotency keys (second precision, UTC)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
