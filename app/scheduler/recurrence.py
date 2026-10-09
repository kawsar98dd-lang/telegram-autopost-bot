"""Schedule definitions and next-run calculation. Pure functions: no database, no clock, no network.

Meaning of every schedule kind (the same text is shown to the user and written in docs/STEP6_SCHEDULER.md):

once    One specific local date and time in the chosen timezone. Runs once, never earlier than that moment.
daily   Every day at each of 1-24 chosen local times ("HH:MM") in the chosen timezone.
weekly  On the chosen weekdays (Monday=0 ... Sunday=6) at each chosen local time in the chosen timezone.
custom  "Every N minutes/hours/days": the first run is the chosen start instant, then every N minutes of ELAPSED time
        (N x 60 seconds, independent of daylight saving), optionally until an end instant. N must be at least
        MIN_INTERVAL_MINUTES and at most MAX_INTERVAL_MINUTES. There is no expression language: only these numbers.

Daylight saving (daily / weekly / once, local wall-clock times):
* a local time that does not exist (the clock jumps forward) runs at the first moment after the jump, i.e. 02:30 on a
  spring-forward day runs at 03:30 local;
* a local time that exists twice (the clock goes back) runs the first time it occurs, once.
Bangladesh (Asia/Dhaka) currently has no daylight saving, so none of this changes anything there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .timeutil import UTC

KINDS = ("once", "daily", "weekly", "custom")
DEFAULT_TIMEZONE = "Asia/Dhaka"
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 366 * 24 * 60
MAX_TIMES_PER_DAY = 24
MAX_ONCE_HORIZON_DAYS = 3 * 366          # a one-time post may be scheduled at most ~3 years ahead
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_TZ_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ScheduleError(Exception):
    """The schedule definition is not valid; ``errors`` are plain sentences that are safe to show."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class Spec:
    """A validated schedule definition. Instants are UTC; ``times`` and ``weekdays`` are local-time concepts."""

    kind: str
    timezone: str
    run_at: datetime | None = None            # once
    times: tuple[str, ...] = ()               # daily / weekly, "HH:MM", sorted, unique
    weekdays: tuple[int, ...] = ()            # weekly, 0=Monday
    interval_minutes: int | None = None       # custom
    starts_at: datetime | None = None         # custom: first run
    ends_at: datetime | None = None           # custom: no run after this instant
    problems: list[str] = field(default_factory=list, compare=False)


def zone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name or raise ScheduleError. Names are matched strictly (no paths, no abbreviations)."""
    text = (name or "").strip()
    if not _TZ_NAME.match(text) or len(text) > 64:
        raise ScheduleError(["The timezone must be an IANA name such as Asia/Dhaka."])
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleError([f"Unknown timezone '{text[:40]}'. Use an IANA name such as Asia/Dhaka."]) from None


def local_to_utc(naive: datetime, tz: ZoneInfo) -> datetime:
    """Local wall-clock time -> UTC with the daylight-saving rules described in the module docstring."""
    candidate = naive.replace(tzinfo=tz, fold=0)
    as_utc = candidate.astimezone(UTC)
    # A time inside a "spring forward" gap does not survive the round trip; the first-fold conversion then already lies
    # after the gap, which is exactly the documented behaviour.
    return as_utc


def parse_local(text: str, tz: ZoneInfo, what: str) -> datetime:
    raw = (text or "").strip()
    if not raw:
        raise ScheduleError([f"Choose a {what}."])
    try:  # the browser sends "YYYY-MM-DDTHH:MM"; "YYYY-MM-DD HH:MM" is accepted as well
        naive = datetime.strptime(raw.replace("T", " ")[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        raise ScheduleError([f"The {what} is not a valid date and time."]) from None
    if not 2000 <= naive.year <= 2100:
        raise ScheduleError([f"The {what} is out of range."])
    return local_to_utc(naive, tz)


def parse_times(raw: str) -> tuple[str, ...]:
    """'09:00, 18:30' / newlines / semicolons -> ('09:00', '18:30'). Strict HH:MM (24 hour)."""
    items = [p.strip() for p in re.split(r"[,;\s]+", raw or "") if p.strip()]
    if not items:
        raise ScheduleError(["Enter at least one time of day, for example 09:00."])
    bad = [p for p in items if not _HHMM.match(p)]
    if bad:
        raise ScheduleError([f"'{bad[0][:10]}' is not a valid time. Use 24-hour HH:MM, for example 18:30."])
    unique = sorted(set(items))
    if len(unique) > MAX_TIMES_PER_DAY:
        raise ScheduleError([f"Use at most {MAX_TIMES_PER_DAY} times per day."])
    return tuple(unique)


def parse_weekdays(raw: list[str]) -> tuple[int, ...]:
    try:
        days = sorted({int(x) for x in raw})
    except (TypeError, ValueError):
        raise ScheduleError(["The weekdays are not valid."]) from None
    if not days:
        raise ScheduleError(["Choose at least one weekday."])
    if any(d < 0 or d > 6 for d in days):
        raise ScheduleError(["The weekdays are not valid."])
    return tuple(days)


def interval_minutes(amount_raw: str, unit: str) -> int:
    try:
        amount = int((amount_raw or "").strip())
    except ValueError:
        raise ScheduleError(["The interval must be a whole number."]) from None
    factor = {"minutes": 1, "hours": 60, "days": 1440}.get(unit)
    if factor is None:
        raise ScheduleError(["Choose minutes, hours or days for the interval."])
    minutes = amount * factor
    if amount < 1 or minutes < MIN_INTERVAL_MINUTES:
        raise ScheduleError([f"The interval must be at least {MIN_INTERVAL_MINUTES} minutes."])
    if minutes > MAX_INTERVAL_MINUTES:
        raise ScheduleError(["The interval is too long (at most 366 days)."])
    return minutes


def build_spec(*, kind: str, tz_name: str, now: datetime, once_at: str = "", times: str = "", weekdays: list[str] | None = None,
               every: str = "", unit: str = "hours", starts: str = "", ends: str = "") -> Spec:
    """Validate the user's input and return a Spec; collects ALL problems into one ScheduleError."""
    errors: list[str] = []
    if kind not in KINDS:
        raise ScheduleError(["Choose one-time, daily, weekly or custom."])
    try:
        tz = zone(tz_name)
    except ScheduleError as exc:
        raise ScheduleError(exc.errors) from None
    tz_clean = tz.key
    try:
        if kind == "once":
            run_at = parse_local(once_at, tz, "date and time")
            if run_at <= now:
                errors.append("That date and time is in the past. Choose a future moment.")
            elif run_at > now + timedelta(days=MAX_ONCE_HORIZON_DAYS):
                errors.append("That date is too far in the future (at most three years).")
            spec = Spec("once", tz_clean, run_at=run_at)
        elif kind == "daily":
            spec = Spec("daily", tz_clean, times=parse_times(times))
        elif kind == "weekly":
            spec = Spec("weekly", tz_clean, times=parse_times(times), weekdays=parse_weekdays(weekdays or []))
        else:
            minutes = interval_minutes(every, unit)
            start = parse_local(starts, tz, "start date and time")
            end = parse_local(ends, tz, "end date and time") if (ends or "").strip() else None
            if start <= now:
                errors.append("The start must be in the future.")
            if end is not None and end <= start:
                errors.append("The end must be after the start.")
            spec = Spec("custom", tz_clean, interval_minutes=minutes, starts_at=start, ends_at=end)
    except ScheduleError as exc:
        errors.extend(exc.errors)
        spec = None
    if errors or spec is None:
        raise ScheduleError(errors)
    return spec


def _local_candidates(spec: Spec, day: date, tz: ZoneInfo) -> list[datetime]:
    return [local_to_utc(datetime.combine(day, time(int(t[:2]), int(t[3:]))), tz) for t in spec.times]


def next_after(spec: Spec, after: datetime) -> datetime | None:
    """The first run strictly after ``after`` (UTC), or None when the schedule has no further run."""
    after = after.astimezone(UTC)
    if spec.kind == "once":
        return spec.run_at if spec.run_at and spec.run_at > after else None
    if spec.kind == "custom":
        assert spec.starts_at is not None and spec.interval_minutes
        step = timedelta(minutes=spec.interval_minutes)
        if after < spec.starts_at:
            candidate = spec.starts_at
        else:
            candidate = spec.starts_at + step * ((after - spec.starts_at) // step + 1)
        return None if spec.ends_at is not None and candidate > spec.ends_at else candidate
    tz = zone(spec.timezone)
    day = after.astimezone(tz).date() - timedelta(days=1)  # one day of slack for zone offsets and gap shifting
    for _ in range(14):  # weekly needs at most 7 days; 14 leaves room for the slack day
        day += timedelta(days=1)
        if spec.kind == "weekly" and day.weekday() not in spec.weekdays:
            continue
        for moment in sorted(_local_candidates(spec, day, tz)):
            if moment > after:
                return moment
    return None


def upcoming(spec: Spec, after: datetime, count: int = 3) -> list[datetime]:
    runs: list[datetime] = []
    cursor = after
    while len(runs) < count:
        nxt = next_after(spec, cursor)
        if nxt is None:
            break
        runs.append(nxt)
        cursor = nxt
    return runs


def describe(spec: Spec) -> str:
    """One plain sentence for the pages."""
    if spec.kind == "once":
        return "Once"
    if spec.kind == "daily":
        return "Every day at " + ", ".join(spec.times) + f" ({spec.timezone})"
    if spec.kind == "weekly":
        return "Every " + ", ".join(WEEKDAY_NAMES[d] for d in spec.weekdays) + " at " + ", ".join(spec.times) + f" ({spec.timezone})"
    minutes = spec.interval_minutes or 0
    if minutes % 1440 == 0:
        every = f"{minutes // 1440} day(s)"
    elif minutes % 60 == 0:
        every = f"{minutes // 60} hour(s)"
    else:
        every = f"{minutes} minutes"
    return f"Every {every}" + (" until the end date" if spec.ends_at else "")


def spec_from_row(row) -> Spec:
    """Rebuild a Spec from a ``schedules`` row (PostgreSQL record or SQLite row)."""
    from .timeutil import loads, to_dt

    return Spec(
        kind=row["kind"], timezone=row["timezone"], run_at=to_dt(row["run_at"]),
        times=tuple(loads(row["times_of_day"], [])), weekdays=tuple(int(d) for d in loads(row["days_of_week"], [])),
        interval_minutes=row["interval_minutes"], starts_at=to_dt(row["starts_at"]), ends_at=to_dt(row["ends_at"]))
