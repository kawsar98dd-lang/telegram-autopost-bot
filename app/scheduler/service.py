"""Schedules as the web application manages them: create, review, list, pause, resume, cancel, job history.

Nothing here talks to Telegram and nothing here sends a message: the web process only writes schedules; the worker
(app/workers/main.py -> app/scheduler/executor.py) turns them into jobs and sends them.

Isolation rules (every method): all SQL is scoped by user_id; a foreign or unknown id (schedule, job, post, group) looks
exactly like a missing one (ScheduleNotFound / PostNotFound); the account is always the post's own account and is never
read from the request; targets must be groups of that post that are CURRENTLY postable; the final message is never
accepted from the client, it is composed from the stored draft by app/posts/composer.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..db.models import new_id
from ..posts import composer
from ..posts.service import PostLocked, PostNotFound, PostService
from ..telegram.group_sync import is_uuid
from . import policy, recurrence
from .queue import JobQueue, sync_post_status
from .recurrence import ScheduleError, Spec, spec_from_row
from .timeutil import dumps, loads, now_utc, to_dt

log = logging.getLogger(__name__)

MAX_OPEN_SCHEDULES = 100
JOB_PAGE = 100


class ScheduleNotFound(Exception):
    pass


class ScheduleConflict(Exception):
    """The schedule/job exists but is not in a state in which the action is possible."""


class ScheduleInvalid(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class ScheduleInput:
    post_id: str
    group_ids: set[str] = field(default_factory=set)
    kind: str = "once"
    timezone: str = recurrence.DEFAULT_TIMEZONE
    once_at: str = ""
    times: str = ""
    weekdays: list[str] = field(default_factory=list)
    every: str = ""
    unit: str = "hours"
    starts: str = ""
    ends: str = ""


def job_state(row: dict) -> tuple[str, str]:
    """(label, tone) for the pages. The six states the user must be able to tell apart."""
    status, delivery = row["status"], row.get("delivery_state") or "not_sent"
    if status == "scheduled":
        return "Queued", "warn"
    if status == "processing":
        return ("Sending now", "warn") if delivery == "sending" else ("Running", "warn")
    if status == "waiting":
        return "Waiting to retry", "warn"
    if status == "posted":
        return "Posted", "ok"
    if status == "cancelled":
        return "Cancelled", "muted"
    if delivery == "uncertain":
        return "Uncertain - check the group", "bad"
    return "Failed", "bad"


def schedule_state(row: dict) -> tuple[str, str]:
    if row["status"] == "active":
        return "Active", "ok"
    if row["status"] == "paused":
        return "Paused", "warn"
    if row.get("cancelled_at"):
        return "Cancelled", "muted"
    return "Completed", "ok"


class ScheduleService:
    def __init__(self, db: Any, posts: PostService, clock: Callable[[], float]) -> None:
        self._db, self._posts, self._clock = db, posts, clock
        self._queue = JobQueue(db, clock)

    def _now(self) -> datetime:
        return now_utc(self._clock)

    # ---- the form / review ------------------------------------------------------------------------------------
    async def draft_for_scheduling(self, user_id: str, post_id: str) -> dict:
        """The draft, its postable targets and its final message (same composer as the preview and the worker)."""
        post = await self._posts.get(user_id, post_id)  # PostNotFound for foreign / unknown posts
        if post["status"] != "draft":
            raise PostLocked()
        message = composer.compose(post["body"], has_image=post["media"] is not None)
        targets = [t for t in post["targets"] if t["eligible"]]
        return {"post": post, "message": message, "targets": targets, "stale": len(post["targets"]) - len(targets)}

    async def _validated(self, user_id: str, data: ScheduleInput) -> tuple[dict, Spec, list[dict]]:
        info = await self.draft_for_scheduling(user_id, data.post_id)
        post = info["post"]
        errors: list[str] = []
        try:
            await self._posts.account(user_id, post["account_id"])
        except PostNotFound:
            raise ScheduleInvalid(["The Telegram account of this post is not connected. Reconnect it first."]) from None
        allowed = {t["id"].lower(): t for t in info["targets"]}
        known = {t["id"].lower() for t in post["targets"]}
        wanted = {g.lower() for g in data.group_ids}
        if not all(is_uuid(g) for g in wanted) or not wanted <= known:
            raise ScheduleNotFound()  # a group that is not a target of THIS post: same answer as "missing"
        chosen = [allowed[g] for g in sorted(wanted) if g in allowed]
        if len(chosen) != len(wanted):
            errors.append("One of the chosen groups can no longer be posted to. Refresh the groups and edit the draft.")
        if not wanted:
            errors.append("Choose at least one target group.")
        try:
            composer.check(info["message"])
        except composer.MessageError as exc:
            errors.append(exc.message)
        spec = None
        try:
            spec = recurrence.build_spec(kind=data.kind, tz_name=data.timezone, now=self._now(), once_at=data.once_at,
                                         times=data.times, weekdays=data.weekdays, every=data.every, unit=data.unit,
                                         starts=data.starts, ends=data.ends)
        except ScheduleError as exc:
            errors.extend(exc.errors)
        if errors or spec is None:
            raise ScheduleInvalid(errors)
        return info, spec, chosen

    async def review(self, user_id: str, data: ScheduleInput) -> dict:
        info, spec, chosen = await self._validated(user_id, data)
        runs = recurrence.upcoming(spec, self._now(), 3)
        return {**info, "spec": spec, "describe": recurrence.describe(spec), "runs": runs, "chosen": chosen,
                "tz": recurrence.zone(spec.timezone)}

    # ---- creating --------------------------------------------------------------------------------------------
    async def create(self, user_id: str, data: ScheduleInput) -> str:
        info, spec, chosen = await self._validated(user_id, data)
        post = info["post"]
        open_count = await self._db.fetchrow(
            "SELECT COUNT(*) AS n FROM schedules WHERE user_id = $1 AND status IN ('active', 'paused')", user_id)
        if int(open_count["n"]) >= MAX_OPEN_SCHEDULES:
            raise ScheduleInvalid([f"You can have at most {MAX_OPEN_SCHEDULES} active schedules."])
        first = recurrence.next_after(spec, self._now())
        if first is None:
            raise ScheduleInvalid(["This schedule has no run in the future."])
        schedule_id, now = new_id(), self._now()
        async with self._db.transaction() as tx:
            await tx.execute(
                "INSERT INTO schedules (id, user_id, post_id, account_id, kind, timezone, run_at, times_of_day, days_of_week, "
                "interval_minutes, starts_at, ends_at, status, next_run_at, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, 'active', $13, $14, $14)",
                schedule_id, user_id, post["id"], post["account_id"], spec.kind, spec.timezone, spec.run_at,
                dumps(list(spec.times)), dumps(list(spec.weekdays)), spec.interval_minutes, spec.starts_at, spec.ends_at,
                first, now)
            for group in chosen:
                await tx.execute("INSERT INTO schedule_targets (schedule_id, group_id, user_id) VALUES ($1, $2, $3)",
                                 schedule_id, group["id"], user_id)
            locked = await tx.fetchrow(  # the draft becomes read-only; two requests cannot both schedule it
                "UPDATE posts SET status = 'scheduled', updated_at = $3 WHERE id = $1 AND user_id = $2 AND status = 'draft' "
                "RETURNING id", post["id"], user_id, now)
            if locked is None:
                raise PostLocked()
        log.info("schedule created (schedule_id=%s kind=%s targets=%d)", schedule_id, spec.kind, len(chosen))
        return schedule_id

    # ---- reading ---------------------------------------------------------------------------------------------
    async def list_schedules(self, user_id: str) -> list[dict]:
        rows = await self._db.fetch(
            "SELECT s.id, s.kind, s.timezone, s.status, s.next_run_at, s.last_run_at, s.cancelled_at, s.created_at, "
            "p.title, p.body, "
            "(SELECT COUNT(*) FROM schedule_targets t WHERE t.schedule_id = s.id AND t.user_id = s.user_id) AS targets, "
            "(SELECT COUNT(*) FROM posting_jobs j WHERE j.schedule_id = s.id AND j.user_id = s.user_id "
            " AND j.status IN ('scheduled', 'processing', 'waiting')) AS pending, "
            "(SELECT COUNT(*) FROM posting_jobs j WHERE j.schedule_id = s.id AND j.user_id = s.user_id "
            " AND j.status = 'posted') AS posted, "
            "(SELECT COUNT(*) FROM posting_jobs j WHERE j.schedule_id = s.id AND j.user_id = s.user_id "
            " AND j.status = 'failed') AS failed "
            "FROM schedules s JOIN posts p ON p.id = s.post_id AND p.user_id = s.user_id "
            "WHERE s.user_id = $1 ORDER BY s.created_at DESC, s.id LIMIT 200", user_id)
        out = []
        for r in rows:
            d = dict(r) | {"id": str(r["id"])}
            d["state"], d["tone"] = schedule_state(d)
            out.append(d)
        return out

    async def get(self, user_id: str, schedule_id: str) -> dict:
        if not is_uuid(schedule_id):
            raise ScheduleNotFound()
        row = await self._db.fetchrow(
            "SELECT s.*, p.title, p.body FROM schedules s JOIN posts p ON p.id = s.post_id AND p.user_id = s.user_id "
            "WHERE s.id = $1 AND s.user_id = $2", schedule_id, user_id)
        if row is None:
            raise ScheduleNotFound()
        data = dict(row) | {"id": str(row["id"]), "post_id": str(row["post_id"]), "account_id": str(row["account_id"])}
        data["state"], data["tone"] = schedule_state(data)
        data["spec"] = spec_from_row(row)
        data["describe"] = recurrence.describe(data["spec"])
        data["targets"] = [dict(r) | {"id": str(r["id"])} for r in await self._db.fetch(
            "SELECT g.id, g.title FROM schedule_targets t JOIN telegram_groups g ON g.id = t.group_id AND g.user_id = t.user_id "
            "WHERE t.schedule_id = $1 AND t.user_id = $2 ORDER BY lower(g.title), g.id", schedule_id, user_id)]
        data["jobs"] = await self.list_jobs(user_id, schedule_id=schedule_id, limit=50)
        data["events"] = [dict(r) | {"details": loads(r["details"], {})} for r in await self._db.fetch(
            "SELECT l.level, l.event, l.message, l.created_at FROM posting_logs l JOIN posting_jobs j ON j.id = l.job_id "
            "AND j.user_id = l.user_id WHERE j.schedule_id = $1 AND l.user_id = $2 ORDER BY l.created_at DESC, l.id LIMIT 30",
            schedule_id, user_id)]
        return data

    async def list_jobs(self, user_id: str, *, schedule_id: str | None = None, limit: int = JOB_PAGE) -> list[dict]:
        base = ("SELECT j.id, j.schedule_id, j.group_title, j.status, j.delivery_state, j.scheduled_for, j.attempts, "
                "j.max_attempts, j.next_attempt_at, j.error_code, j.error_message, j.posted_at, j.finished_at, "
                "p.title AS post_title, p.body AS post_body FROM posting_jobs j "
                "LEFT JOIN posts p ON p.id = j.post_id AND p.user_id = j.user_id WHERE j.user_id = $1")
        if schedule_id:
            rows = await self._db.fetch(base + " AND j.schedule_id = $2 ORDER BY j.scheduled_for DESC, j.id LIMIT $3",
                                        user_id, schedule_id, limit)
        else:
            rows = await self._db.fetch(base + " ORDER BY j.scheduled_for DESC, j.id LIMIT $2", user_id, limit)
        out = []
        for r in rows:
            d = dict(r) | {"id": str(r["id"])}
            d["state"], d["tone"] = job_state(d)
            d["needs_attention"] = d["status"] == "failed" and (d["error_code"] in policy.NEEDS_ATTENTION)
            out.append(d)
        return out

    # ---- state changes ----------------------------------------------------------------------------------------
    async def _owned(self, user_id: str, schedule_id: str) -> dict:
        if not is_uuid(schedule_id):
            raise ScheduleNotFound()
        row = await self._db.fetchrow("SELECT * FROM schedules WHERE id = $1 AND user_id = $2", schedule_id, user_id)
        if row is None:
            raise ScheduleNotFound()
        return dict(row) | {"id": str(row["id"]), "post_id": str(row["post_id"])}

    async def pause(self, user_id: str, schedule_id: str) -> None:
        sched = await self._owned(user_id, schedule_id)
        row = await self._db.fetchrow(
            "UPDATE schedules SET status = 'paused', updated_at = $3 WHERE id = $1 AND user_id = $2 AND status = 'active' "
            "RETURNING id", sched["id"], user_id, self._now())
        if row is None:
            raise ScheduleConflict()

    async def resume(self, user_id: str, schedule_id: str) -> None:
        sched = await self._owned(user_id, schedule_id)
        if sched["status"] != "paused":
            raise ScheduleConflict()
        spec = spec_from_row(sched)
        now = self._now()
        nxt = recurrence.next_after(spec, now)  # occurrences that fell into the pause are skipped, not caught up
        if nxt is None:
            raise ScheduleInvalid(["This schedule has no run in the future any more. Cancel it and create a new one."])
        row = await self._db.fetchrow(
            "UPDATE schedules SET status = 'active', next_run_at = $3, updated_at = $4 WHERE id = $1 AND user_id = $2 "
            "AND status = 'paused' RETURNING id", sched["id"], user_id, nxt, now)
        if row is None:
            raise ScheduleConflict()

    async def cancel(self, user_id: str, schedule_id: str) -> int:
        """Stop a schedule and cancel its not-yet-sending jobs. A job that has already started sending cannot be recalled."""
        sched = await self._owned(user_id, schedule_id)
        now = self._now()
        async with self._db.transaction() as tx:
            changed = await tx.fetchrow(
                "UPDATE schedules SET status = 'completed', cancelled_at = $3, next_run_at = NULL, updated_at = $3 "
                "WHERE id = $1 AND user_id = $2 AND status IN ('active', 'paused') RETURNING id", sched["id"], user_id, now)
            jobs = await tx.fetch(
                "UPDATE posting_jobs SET status = 'cancelled', error_code = 'cancelled', error_message = $3, finished_at = $4, "
                "locked_by = NULL, lease_expires_at = NULL, updated_at = $4 WHERE schedule_id = $1 AND user_id = $2 AND "
                "(status IN ('scheduled', 'waiting') OR (status = 'processing' AND delivery_state = 'not_sent')) RETURNING id",
                sched["id"], user_id, policy.safe_message("cancelled"), now)
            if changed is None and not jobs:
                raise ScheduleConflict()
            for j in jobs:
                await self._queue.log(tx, user_id, str(j["id"]), "info", "cancelled", policy.safe_message("cancelled"),
                                      schedule_id=sched["id"])
        await sync_post_status(self._db, user_id, sched["post_id"])
        return len(jobs)

    async def resolve_job(self, user_id: str, job_id: str, action: str) -> None:
        """A person decides about a FAILED job: 'retry' (send again) or 'confirm_sent' (it did arrive; uncertain jobs only)."""
        if not is_uuid(job_id):
            raise ScheduleNotFound()
        job = await self._db.fetchrow("SELECT id, post_id, status, delivery_state FROM posting_jobs WHERE id = $1 AND user_id = $2",
                                      job_id, user_id)
        if job is None:
            raise ScheduleNotFound()
        now = self._now()
        if action == "confirm_sent":
            row = await self._db.fetchrow(
                "UPDATE posting_jobs SET status = 'posted', delivery_state = 'sent', posted_at = $3, error_code = NULL, "
                "error_message = NULL, updated_at = $3 WHERE id = $1 AND user_id = $2 AND status = 'failed' "
                "AND delivery_state = 'uncertain' RETURNING id", job_id, user_id, now)
            event, message = "resolved", "Confirmed by the user: the post was delivered."
        elif action == "retry":
            row = await self._db.fetchrow(
                "UPDATE posting_jobs SET status = 'scheduled', delivery_state = 'not_sent', attempts = 0, scheduled_for = $3, "
                "next_attempt_at = $3, error_code = NULL, error_message = NULL, finished_at = NULL, send_started_at = NULL, "
                "updated_at = $3 WHERE id = $1 AND user_id = $2 AND status = 'failed' RETURNING id", job_id, user_id, now)
            event, message = "retried", "Requeued by the user."
        else:
            raise ScheduleConflict()
        if row is None:
            raise ScheduleConflict()
        await self._queue.log(self._db, user_id, job_id, "info", event, message, delivery_before=str(job["delivery_state"]))
        await sync_post_status(self._db, user_id, str(job["post_id"]) if job["post_id"] else None)
