"""The PostgreSQL job queue: turning due schedules into jobs, claiming jobs, recording outcomes, recovering after crashes.

PostgreSQL is the single source of truth; nothing here keeps authoritative state in memory.

Concurrency design
------------------
* Schedule -> jobs ("materialisation"): each due schedule is locked with ``SELECT ... FOR UPDATE SKIP LOCKED`` inside a
  short transaction (database work only, never Telegram), its jobs are inserted with ``ON CONFLICT (idempotency_key) DO
  NOTHING`` and ``next_run_at`` is advanced with a compare-and-set. Two workers can therefore never create the same
  occurrence: the second one skips the locked row, or finds ``next_run_at`` already moved, or hits the unique key.
* Claiming: ``UPDATE posting_jobs ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING ...`` is one atomic
  statement; it sets the lease (``locked_by`` + ``lease_expires_at``) and counts the attempt.
* One Telegram account is served by one worker at a time: an atomic ``UPDATE telegram_accounts SET send_locked_by ... WHERE
  the lock is free or expired`` takes the account (a FloodWait in ``send_blocked_until`` keeps it closed).
* A worker that dies leaves expired leases; ``recover_stale`` resolves them WITHOUT ever resending a job whose send had
  started (those become ``uncertain``).
No transaction is held open while talking to Telegram.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from ..db.models import new_id
from . import policy
from .recurrence import ScheduleError, next_after, spec_from_row
from .timeutil import dumps, iso, loads, now_utc, to_dt

log = logging.getLogger("scheduler.queue")

PENDING = ("scheduled", "waiting")
JOB_COLUMNS = ("id, user_id, schedule_id, post_id, account_id, group_id, group_title, text_snapshot, media_snapshot, "
               "scheduled_for, attempts, max_attempts, idempotency_key")


@dataclass
class Job:
    id: str
    user_id: str
    schedule_id: str | None
    post_id: str | None
    account_id: str
    group_id: str | None
    group_title: str
    text: str
    media: dict | None
    scheduled_for: datetime
    attempts: int
    max_attempts: int
    key: str

    @classmethod
    def from_row(cls, r) -> "Job":
        s = lambda v: str(v) if v is not None else None  # noqa: E731
        media = loads(r["media_snapshot"], [])
        if isinstance(media, list):
            media = media[0] if media else None
        return cls(s(r["id"]), s(r["user_id"]), s(r["schedule_id"]), s(r["post_id"]), s(r["account_id"]), s(r["group_id"]),
                   r["group_title"], r["text_snapshot"], media or None, to_dt(r["scheduled_for"]), int(r["attempts"]),
                   int(r["max_attempts"]), r["idempotency_key"])


async def sync_post_status(db: Any, user_id: str, post_id: str | None) -> None:
    """Derive posts.status from the schedules/jobs of that post (drafts are the only editable state)."""
    if not post_id:
        return
    row = await db.fetchrow("SELECT status FROM posts WHERE id = $1 AND user_id = $2", post_id, user_id)
    if row is None or row["status"] == "draft" and not await _has_any(db, user_id, post_id):
        return
    if await db.fetchrow("SELECT 1 AS x FROM schedules WHERE post_id = $1 AND user_id = $2 AND status IN ('active', 'paused')",
                         post_id, user_id):
        new = "scheduled"
    elif await db.fetchrow("SELECT 1 AS x FROM posting_jobs WHERE post_id = $1 AND user_id = $2 "
                           "AND status IN ('scheduled', 'waiting', 'processing')", post_id, user_id):
        new = "sending"
    elif await db.fetchrow("SELECT 1 AS x FROM posting_jobs WHERE post_id = $1 AND user_id = $2 "
                           "AND (status = 'posted' OR delivery_state = 'uncertain')", post_id, user_id):
        new = "sent"
    elif await db.fetchrow("SELECT 1 AS x FROM posting_jobs WHERE post_id = $1 AND user_id = $2 AND status = 'failed'",
                           post_id, user_id):
        new = "failed"
    else:
        new = "draft"  # everything was cancelled before anything happened: editable again
    await db.execute("UPDATE posts SET status = $3, updated_at = CURRENT_TIMESTAMP WHERE id = $1 AND user_id = $2",
                     post_id, user_id, new)


async def _has_any(db: Any, user_id: str, post_id: str) -> bool:
    return bool(await db.fetchrow("SELECT 1 AS x FROM schedules WHERE post_id = $1 AND user_id = $2", post_id, user_id))


class JobQueue:
    def __init__(self, db: Any, clock: Callable[[], float]) -> None:
        self._db, self._clock = db, clock

    # ---- helpers --------------------------------------------------------------------------------------------
    def now(self) -> datetime:
        return now_utc(self._clock)

    @property
    def _skip_locked(self) -> str:
        return " FOR UPDATE SKIP LOCKED" if getattr(self._db, "dialect", "postgres") == "postgres" else ""

    async def log(self, db: Any, user_id: str, job_id: str | None, level: str, event: str, message: str, **details: Any) -> None:
        """One audit row in posting_logs. Only ids, codes and counters go into ``details`` - never text or secrets."""
        await db.execute(
            "INSERT INTO posting_logs (id, user_id, job_id, level, event, message, details, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            new_id(), user_id, job_id, level, event, message[:300], dumps(details), self.now())

    # ---- schedule -> jobs ----------------------------------------------------------------------------------
    async def materialize_due(self, limit: int = 50) -> int:
        """Create the jobs of every schedule whose next_run_at has arrived. Returns the number of occurrences created."""
        now = self.now()
        ids = [str(r["id"]) for r in await self._db.fetch(
            "SELECT id FROM schedules WHERE status = 'active' AND next_run_at <= $1 ORDER BY next_run_at, id LIMIT $2",
            now, limit)]
        created = 0
        for schedule_id in ids:
            try:
                created += await self._materialize_one(schedule_id, now)
            except Exception:  # noqa: BLE001 - one broken schedule must not stop the others
                log.exception("could not create jobs for a schedule (schedule_id=%s)", schedule_id)
        return created

    async def _materialize_one(self, schedule_id: str, now: datetime) -> int:
        async with self._db.transaction() as tx:
            row = await tx.fetchrow(
                "SELECT id, user_id, post_id, account_id, kind, timezone, run_at, times_of_day, days_of_week, "
                "interval_minutes, starts_at, ends_at, next_run_at FROM schedules "
                "WHERE id = $1 AND status = 'active' AND next_run_at <= $2" + self._skip_locked, schedule_id, now)
            if row is None:  # another worker has it, or it was already advanced / paused / cancelled
                return 0
            user_id, post_id, account_id = str(row["user_id"]), str(row["post_id"]), str(row["account_id"])
            occurrence = to_dt(row["next_run_at"])
            try:
                spec = spec_from_row(row)
                following = next_after(spec, occurrence)
            except ScheduleError:
                await tx.execute("UPDATE schedules SET status = 'paused', updated_at = $2 WHERE id = $1", schedule_id, now)
                await self.log(tx, user_id, None, "error", "schedule_invalid", "The schedule definition is invalid; paused.",
                               schedule_id=schedule_id)
                return 0
            post = await tx.fetchrow("SELECT body FROM posts WHERE id = $1 AND user_id = $2 AND account_id = $3",
                                     post_id, user_id, account_id)
            media = await tx.fetchrow(
                "SELECT content_type, size_bytes, sha256, storage_backend, storage_key FROM post_media "
                "WHERE post_id = $1 AND user_id = $2", post_id, user_id)
            targets = await tx.fetch(
                "SELECT g.id, g.title FROM schedule_targets t JOIN telegram_groups g ON g.id = t.group_id "
                "AND g.user_id = t.user_id WHERE t.schedule_id = $1 AND t.user_id = $2 AND g.account_id = $3 "
                "ORDER BY g.id", schedule_id, user_id, account_id)
            late = (now - occurrence).total_seconds() > policy.MISSED_AFTER_SECONDS
            made = 0
            if post is not None:
                snapshot = dumps([{k: media[k] for k in ("content_type", "size_bytes", "sha256", "storage_backend",
                                                         "storage_key")}]) if media else "[]"
                for g in targets:
                    job_id = new_id()
                    inserted = await tx.fetchrow(
                        "INSERT INTO posting_jobs (id, user_id, schedule_id, post_id, account_id, group_id, group_title, "
                        "text_snapshot, media_snapshot, scheduled_for, status, attempts, next_attempt_at, idempotency_key, "
                        "error_code, error_message, finished_at, created_at, updated_at) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 0, $10, $12, $13, $14, $15, $16, $16) "
                        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
                        job_id, user_id, schedule_id, post_id, account_id, str(g["id"]), g["title"], post["body"], snapshot,
                        occurrence, "failed" if late else "scheduled", f"{schedule_id}:{iso(occurrence)}:{g['id']}",
                        "missed" if late else None, policy.safe_message("missed") if late else None,
                        now if late else None, now)
                    if inserted is not None:
                        made += 1
                        if late:
                            await self.log(tx, user_id, job_id, "warning", "missed", policy.safe_message("missed"),
                                           schedule_id=schedule_id, group_id=str(g["id"]))
            # advance: never into the past more than the policy allows (only the FIRST missed occurrence is recorded)
            if following is not None and following <= now and (now - following).total_seconds() > policy.MISSED_AFTER_SECONDS:
                following = next_after(spec, now)
            new_status = "active" if following is not None else "completed"
            moved = await tx.fetchrow(
                "UPDATE schedules SET next_run_at = $3, last_run_at = $4, status = $5, updated_at = $6 "
                "WHERE id = $1 AND status = 'active' AND next_run_at = $2 RETURNING id",
                schedule_id, row["next_run_at"], following, occurrence, new_status, now)
            if moved is None:  # lost a race (cannot happen while the row is locked); undo everything
                raise RuntimeError("schedule changed while its jobs were being created")
        await sync_post_status(self._db, user_id, post_id)
        return 1 if made else 0

    # ---- claiming ---------------------------------------------------------------------------------------------
    async def claim_account_batch(self, worker_id: str, *, batch: int = policy.CLAIM_BATCH,
                                  lease: int = policy.LEASE_SECONDS) -> tuple[str, list[Job]] | None:
        """Take one free account that has due jobs and claim up to ``batch`` of its jobs. None if there is nothing to do."""
        now = self.now()
        candidates = await self._db.fetch(
            "SELECT j.account_id AS account_id, MIN(j.next_attempt_at) AS first_due FROM posting_jobs j "
            "WHERE j.status IN ('scheduled', 'waiting') AND j.next_attempt_at <= $1 "
            "AND NOT EXISTS (SELECT 1 FROM schedules s WHERE s.id = j.schedule_id AND s.status = 'paused') "
            "GROUP BY j.account_id ORDER BY first_due LIMIT 10", now)
        for cand in candidates:
            account_id = str(cand["account_id"])
            locked = await self._db.fetchrow(
                "UPDATE telegram_accounts SET send_locked_by = $2, send_lock_expires_at = $3 WHERE id = $1 "
                "AND (send_locked_by IS NULL OR send_lock_expires_at <= $4) "
                "AND (send_blocked_until IS NULL OR send_blocked_until <= $4) RETURNING id",
                account_id, worker_id, now + timedelta(seconds=policy.ACCOUNT_LOCK_SECONDS), now)
            if locked is None:
                continue  # another worker serves this account, or Telegram told us to wait
            rows = await self._db.fetch(
                "UPDATE posting_jobs SET status = 'processing', locked_by = $1, lease_expires_at = $2, "
                "attempts = attempts + 1, updated_at = $3 WHERE id IN ("
                "SELECT j.id FROM posting_jobs j WHERE j.account_id = $4 AND j.status IN ('scheduled', 'waiting') "
                "AND j.next_attempt_at <= $3 AND NOT EXISTS (SELECT 1 FROM schedules s WHERE s.id = j.schedule_id "
                "AND s.status = 'paused') ORDER BY j.next_attempt_at, j.id LIMIT $5" + self._skip_locked + ") "
                "AND status IN ('scheduled', 'waiting') RETURNING " + JOB_COLUMNS,
                worker_id, now + timedelta(seconds=lease), now, account_id, batch)
            if not rows:
                await self.release_account(account_id, worker_id)
                continue
            jobs = sorted((Job.from_row(r) for r in rows), key=lambda j: (j.scheduled_for, j.id))
            return account_id, jobs
        return None

    async def extend(self, account_id: str, job_id: str | None, worker_id: str, lease: int = policy.LEASE_SECONDS) -> None:
        until = self.now() + timedelta(seconds=lease)
        await self._db.execute("UPDATE telegram_accounts SET send_lock_expires_at = $3 WHERE id = $1 AND send_locked_by = $2",
                               account_id, worker_id, max(until, self.now() + timedelta(seconds=policy.ACCOUNT_LOCK_SECONDS)))
        if job_id:
            await self._db.execute("UPDATE posting_jobs SET lease_expires_at = $3 WHERE id = $1 AND locked_by = $2 "
                                   "AND status = 'processing'", job_id, worker_id, until)

    async def release_account(self, account_id: str, worker_id: str) -> None:
        await self._db.execute("UPDATE telegram_accounts SET send_locked_by = NULL, send_lock_expires_at = NULL "
                               "WHERE id = $1 AND send_locked_by = $2", account_id, worker_id)

    async def block_account(self, account_id: str, until: datetime) -> None:
        await self._db.execute("UPDATE telegram_accounts SET send_blocked_until = $2 WHERE id = $1", account_id, until)

    # ---- outcomes ------------------------------------------------------------------------------------------------
    async def begin_send(self, job: Job, worker_id: str) -> bool:
        """Last gate before Telegram: only a job that is still ours and still processing may start sending.

        Cancelling a job (status -> cancelled) therefore wins up to this very moment. The write is committed BEFORE the
        request goes out, so a crash from here on is recorded as 'sending' and recovered as 'uncertain'.
        """
        now = self.now()
        row = await self._db.fetchrow(
            "UPDATE posting_jobs SET delivery_state = 'sending', send_started_at = $3, lease_expires_at = $4, updated_at = $3 "
            "WHERE id = $1 AND locked_by = $2 AND status = 'processing' AND delivery_state = 'not_sent' RETURNING id",
            job.id, worker_id, now, now + timedelta(seconds=policy.LEASE_SECONDS))
        return row is not None

    async def mark_posted(self, job: Job, worker_id: str, message_ids: list[int]) -> bool:
        now = self.now()
        row = await self._db.fetchrow(
            "UPDATE posting_jobs SET status = 'posted', delivery_state = 'sent', telegram_message_ids = $3, posted_at = $4, "
            "finished_at = $4, error_code = NULL, error_message = NULL, locked_by = NULL, lease_expires_at = NULL, "
            "updated_at = $4 WHERE id = $1 AND locked_by = $2 AND status = 'processing' RETURNING id",
            job.id, worker_id, dumps(message_ids), now)
        if row is not None:
            await self.log(self._db, job.user_id, job.id, "info", "sent", "Posted to Telegram.", post_id=job.post_id,
                           group_id=job.group_id, attempt=job.attempts, delivery="sent")
            await sync_post_status(self._db, job.user_id, job.post_id)
        return row is not None

    async def mark_failed(self, job: Job, worker_id: str, code: str, *, uncertain: bool = False) -> bool:
        now = self.now()
        row = await self._db.fetchrow(
            "UPDATE posting_jobs SET status = 'failed', delivery_state = $3, error_code = $4, error_message = $5, "
            "finished_at = $6, locked_by = NULL, lease_expires_at = NULL, updated_at = $6 "
            "WHERE id = $1 AND locked_by = $2 AND status = 'processing' RETURNING id",
            job.id, worker_id, "uncertain" if uncertain else "not_sent", code, policy.safe_message(code), now)
        if row is not None:
            await self.log(self._db, job.user_id, job.id, "error", "uncertain" if uncertain else "failed",
                           policy.safe_message(code), post_id=job.post_id, group_id=job.group_id, attempt=job.attempts,
                           error_code=code, delivery="uncertain" if uncertain else "not_sent")
            await sync_post_status(self._db, job.user_id, job.post_id)
        return row is not None

    async def defer(self, job: Job, worker_id: str, until: datetime, code: str, *, counts_attempt: bool) -> bool:
        """Back to the queue for a later attempt. Only for jobs for which it is CERTAIN that nothing was delivered."""
        now = self.now()
        row = await self._db.fetchrow(
            "UPDATE posting_jobs SET status = 'waiting', delivery_state = 'not_sent', send_started_at = NULL, "
            "next_attempt_at = $3, error_code = $4, error_message = $5, locked_by = NULL, lease_expires_at = NULL, "
            "attempts = CASE WHEN $6 THEN attempts WHEN attempts > 0 THEN attempts - 1 ELSE 0 END, updated_at = $7 "
            "WHERE id = $1 AND locked_by = $2 AND status = 'processing' RETURNING id",
            job.id, worker_id, until, code, policy.safe_message(code), counts_attempt, now)
        if row is not None:
            await self.log(self._db, job.user_id, job.id, "warning", "deferred", policy.safe_message(code),
                           post_id=job.post_id, group_id=job.group_id, attempt=job.attempts, error_code=code,
                           retry_at=iso(until))
        return row is not None

    async def release_unstarted(self, worker_id: str, job_ids: list[str], *, until: datetime | None = None) -> None:
        """Give claimed jobs back that were never sent (shutdown, FloodWait of the account, hourly cap ...)."""
        now = self.now()
        for job_id in job_ids:
            await self._db.execute(
                "UPDATE posting_jobs SET status = 'scheduled', locked_by = NULL, lease_expires_at = NULL, "
                "next_attempt_at = COALESCE($3, next_attempt_at), "
                "attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END, updated_at = $4 "
                "WHERE id = $1 AND locked_by = $2 AND status = 'processing' AND delivery_state = 'not_sent'",
                job_id, worker_id, until, now)

    # ---- recovery ----------------------------------------------------------------------------------------------
    async def recover_stale(self) -> dict[str, int]:
        """Resolve jobs whose worker vanished (lease expired).

        * the send had STARTED (delivery_state 'sending'): the message may exist -> failed + uncertain, never resent;
        * claimed but not started: back to the queue, unless it has already been claimed max_attempts times (a job that
          keeps killing the worker is failed instead of looping forever).
        """
        now = self.now()
        # The account lock of a worker whose job lease has expired is released together with its jobs. This must happen
        # BEFORE the jobs are recovered (afterwards the evidence, locked_by, is gone) and only for the lock holder that
        # owns an expired job on that very account and NO job with a live lease there: a worker that is alive keeps
        # extending its leases, and a lock that was just taken (no jobs claimed yet) is never touched here.
        await self._db.execute(
            "UPDATE telegram_accounts SET send_locked_by = NULL, send_lock_expires_at = NULL WHERE send_locked_by IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM posting_jobs j WHERE j.account_id = telegram_accounts.id "
            "AND j.locked_by = telegram_accounts.send_locked_by AND j.status = 'processing' AND j.lease_expires_at <= $1) "
            "AND NOT EXISTS (SELECT 1 FROM posting_jobs j WHERE j.account_id = telegram_accounts.id "
            "AND j.locked_by = telegram_accounts.send_locked_by AND j.status = 'processing' AND j.lease_expires_at > $1)", now)
        uncertain = await self._db.fetch(
            "UPDATE posting_jobs SET status = 'failed', delivery_state = 'uncertain', error_code = 'delivery_uncertain', "
            "error_message = $2, finished_at = $1, locked_by = NULL, lease_expires_at = NULL, updated_at = $1 "
            "WHERE status = 'processing' AND lease_expires_at <= $1 AND delivery_state = 'sending' "
            "RETURNING id, user_id, post_id, group_id", now, policy.safe_message("delivery_uncertain"))
        crashed = await self._db.fetch(
            "UPDATE posting_jobs SET status = 'failed', delivery_state = 'not_sent', error_code = 'worker_crashed', "
            "error_message = $2, finished_at = $1, locked_by = NULL, lease_expires_at = NULL, updated_at = $1 "
            "WHERE status = 'processing' AND lease_expires_at <= $1 AND delivery_state = 'not_sent' "
            "AND attempts >= max_attempts RETURNING id, user_id, post_id, group_id", now, policy.safe_message("worker_crashed"))
        requeued = await self._db.fetch(
            "UPDATE posting_jobs SET status = 'scheduled', locked_by = NULL, lease_expires_at = NULL, updated_at = $1 "
            "WHERE status = 'processing' AND lease_expires_at <= $1 AND delivery_state = 'not_sent' "
            "AND attempts < max_attempts RETURNING id, user_id, post_id, group_id", now)
        for rows, event, code, level in ((uncertain, "uncertain", "delivery_uncertain", "error"),
                                         (crashed, "failed", "worker_crashed", "error"), (requeued, "recovered", "", "warning")):
            for r in rows:
                await self.log(self._db, str(r["user_id"]), str(r["id"]), level, event,
                               policy.safe_message(code) if code else "Recovered after the worker stopped.",
                               post_id=str(r["post_id"]) if r["post_id"] else None, error_code=code or None)
                await sync_post_status(self._db, str(r["user_id"]), str(r["post_id"]) if r["post_id"] else None)
        await self._db.execute("UPDATE telegram_accounts SET send_locked_by = NULL, send_lock_expires_at = NULL "
                               "WHERE send_lock_expires_at IS NOT NULL AND send_lock_expires_at <= $1", now)
        return {"uncertain": len(uncertain), "worker_crashed": len(crashed), "requeued": len(requeued)}
