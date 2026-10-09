"""Executes claimed jobs: re-validates everything, composes the message with the canonical composer, sends, records.

For EVERY job, right before sending (a saved permission snapshot is never trusted):
  1. the account is connected and belongs to the job's user; the stored session decrypts (UsableSession);
  2. the target group is a group of that same user AND account and is still part of the schedule;
  3. Telegram's current view of the chat (fresh dialog list, same read-only call as the Step 4 refresh) is assessed with
     the Step 4 permission logic (app/telegram/groups.assess);
  4. the final text is built by app/posts/composer.compose() from the stored body - the footer is appended exactly once
     there, at send time, and nowhere else - and checked against the Telegram limits (caption limit for photos);
  5. the image bytes come from the storage backend and are validated again (app/posts/media.validate_image);
  6. the job is still ours and not cancelled (queue.begin_send), and only then the request goes out.
The worker never joins/leaves chats, never changes rights, never asks for a code or password, and never uses the Bot API.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from ..posts import composer
from ..posts.limits import max_image_bytes
from ..posts.media import MediaError, validate_image
from ..posts.storage import MediaStorage
from ..telegram.connect import AccountNotFound, TelegramConnectionService
from ..telegram.errors import FloodWait, NetworkProblem, SessionRevoked, TelegramError
from ..telegram.groups import OK, assess
from ..telegram.service import TelegramClientService
from . import policy
from .queue import Job, JobQueue
from .timeutil import now_utc, to_dt

log = logging.getLogger("scheduler.executor")
OUTCOME_WRITE_TRIES = 3


class JobExecutor:
    def __init__(self, db: Any, queue: JobQueue, connect: TelegramConnectionService, telegram: TelegramClientService,
                 storage: MediaStorage, settings: Any, clock: Callable[[], float], *, worker_id: str,
                 rng: Callable[[], float] = random.random,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, stop: asyncio.Event | None = None) -> None:
        self._db, self._queue, self._connect, self._tg, self._storage = db, queue, connect, telegram, storage
        self._settings, self._clock, self.worker_id, self._rng, self._sleep = settings, clock, worker_id, rng, sleep
        self._stop = stop or asyncio.Event()

    # ---- entry point --------------------------------------------------------------------------------------------
    async def run_once(self) -> bool:
        """Claim and process one account's batch. Returns False when there was nothing to do."""
        claimed = await self._queue.claim_account_batch(self.worker_id)
        if claimed is None:
            return False
        account_id, jobs = claimed
        try:
            await self._run_batch(account_id, jobs)
        finally:  # whatever happened: unfinished, never-started jobs go back and the account is freed
            leftover = [j.id for j in jobs]
            await self._queue.release_unstarted(self.worker_id, leftover)
            await self._queue.release_account(account_id, self.worker_id)
        return True

    # ---- one account ----------------------------------------------------------------------------------------------
    async def _run_batch(self, account_id: str, jobs: list[Job]) -> None:
        queue = self._queue
        account = await self._db.fetchrow("SELECT id, user_id, status FROM telegram_accounts WHERE id = $1", account_id)
        if account is None or str(account["status"]) != "connected":
            return await self._fail_all(jobs, "account_unavailable")
        user_id = str(account["user_id"])
        mine = [j for j in jobs if j.user_id == user_id]
        for j in jobs:
            if j.user_id != user_id:
                await queue.mark_failed(j, self.worker_id, "account_mismatch")
        try:
            usable = await self._connect.load_session(user_id, account_id)
        except AccountNotFound:
            return await self._fail_all(mine, "account_unavailable")

        try:
            async with self._tg.posting_session(usable.api_id, usable.api_hash, usable.session) as ps:
                try:
                    raw_chats = {c.chat_id: c for c in await ps.fresh_groups()}
                except TelegramError as exc:
                    return await self._batch_error(account_id, user_id, mine, exc)
                sent_in_batch = 0
                for index, job in enumerate(mine):
                    if self._stop.is_set():
                        return  # graceful shutdown: the rest is released by run_once
                    try:
                        verdict = await self._prepare(job, user_id, account_id, raw_chats)
                    except _Fail as fail:
                        await queue.mark_failed(job, self.worker_id, fail.code)
                        continue
                    text, image, image_name, chat_id = verdict
                    cap_until = await self._hourly_cap(account_id)
                    if cap_until is not None:  # too many posts in the last hour: wait, do not fail
                        await queue.release_unstarted(self.worker_id, [j.id for j in mine[index:]], until=cap_until)
                        return
                    if sent_in_batch:
                        await self._sleep(self._settings.post_min_interval_seconds)
                    await queue.extend(account_id, job.id, self.worker_id)
                    if not await queue.begin_send(job, self.worker_id):
                        continue  # cancelled (or lease lost) in the meantime: nothing was sent
                    sent_in_batch += 1
                    try:
                        message_ids = await ps.send(chat_id, text, image, image_name)
                    except TelegramError as exc:
                        if not await self._after_send_error(account_id, user_id, job, exc, mine[index + 1:]):
                            return
                        continue
                    await self._persist(lambda: queue.mark_posted(job, self.worker_id, message_ids))
        except TelegramError as exc:  # connect failed before anything was sent
            await self._batch_error(account_id, user_id, mine, exc)

    async def _prepare(self, job: Job, user_id: str, account_id: str, raw_chats: dict):
        now = now_utc(self._clock)
        if (now - job.scheduled_for).total_seconds() > policy.MISSED_AFTER_SECONDS:
            raise _Fail("missed")
        if job.group_id is None:
            raise _Fail("target_missing")
        group = await self._db.fetchrow(
            "SELECT id, tg_chat_id FROM telegram_groups WHERE id = $1 AND user_id = $2 AND account_id = $3",
            job.group_id, user_id, account_id)
        if group is None:
            raise _Fail("target_missing")
        if job.schedule_id is not None and await self._db.fetchrow(
                "SELECT 1 AS x FROM schedules s JOIN schedule_targets t ON t.schedule_id = s.id AND t.user_id = s.user_id "
                "WHERE s.id = $1 AND s.user_id = $2 AND s.account_id = $3 AND t.group_id = $4",
                job.schedule_id, user_id, account_id, job.group_id) is None:
            raise _Fail("target_missing")
        chat_id = int(group["tg_chat_id"])
        raw = raw_chats.get(chat_id)
        if raw is None:
            await self._record_permission(user_id, account_id, job.group_id, "unavailable", "This account no longer sees the group.")
            raise _Fail("group_unavailable")
        verdict = assess(raw, now)
        if verdict is None or verdict.status != OK:
            code = "group_unavailable" if verdict is not None and verdict.status == "unavailable" else "no_permission"
            await self._record_permission(user_id, account_id, job.group_id, verdict.status if verdict else "unavailable",
                                          verdict.detail if verdict else "")
            raise _Fail(code)
        image = image_name = None
        if job.media:
            if job.media.get("storage_backend") != self._storage.backend:
                raise _Fail("media_missing")
            image = await self._storage.get(job.media["storage_key"])
            if image is None:
                raise _Fail("media_missing")
            try:
                checked = validate_image("image.png" if job.media.get("content_type") == "image/png" else "image.jpg", image, max_image_bytes(self._settings.max_upload_mb))
            except MediaError:
                raise _Fail("invalid_media") from None
            image_name = "image.png" if checked.content_type == "image/png" else "image.jpg"
        try:  # THE canonical composition: footer appended exactly once, limits checked on the FINAL text
            message = composer.compose(job.text, has_image=image is not None)
            composer.check(message)
        except composer.MessageError:
            raise _Fail("message_invalid") from None
        return message.text, image, image_name, chat_id

    async def _record_permission(self, user_id: str, account_id: str, group_id: str, status: str, detail: str) -> None:
        """Keep the Step 4 view honest: a group that failed the live check is no longer a selected, postable target."""
        if status not in ("no_permission", "unavailable", "restricted"):
            return
        await self._db.execute(
            "UPDATE telegram_groups SET permission_status = $4, permission_detail = $5, is_enabled = FALSE, "
            "permission_checked_at = $6, updated_at = $6 WHERE id = $1 AND user_id = $2 AND account_id = $3",
            group_id, user_id, account_id, status, detail or None, now_utc(self._clock))

    async def _hourly_cap(self, account_id: str) -> datetime | None:
        now = now_utc(self._clock)
        row = await self._db.fetchrow(
            "SELECT COUNT(*) AS n, MIN(send_started_at) AS first_sent FROM posting_jobs WHERE account_id = $1 "
            "AND send_started_at >= $2 AND delivery_state IN ('sent', 'uncertain')", account_id, now - timedelta(hours=1))
        if int(row["n"]) < self._settings.max_posts_per_hour:
            return None
        return to_dt(row["first_sent"]) + timedelta(hours=1, seconds=1)

    # ---- error handling -------------------------------------------------------------------------------------------
    async def _after_send_error(self, account_id: str, user_id: str, job: Job, exc: TelegramError, rest: list[Job]) -> bool:
        """Record the outcome of a failed send. Returns True if the batch may continue with the next job."""
        queue, now = self._queue, now_utc(self._clock)
        decision = policy.decide(exc, attempts=job.attempts, max_attempts=job.max_attempts, rng=self._rng)
        if decision.action == "uncertain":
            await self._persist(lambda: queue.mark_failed(job, self.worker_id, "delivery_uncertain", uncertain=True))
            return True  # the other targets are independent groups; only THIS job is in doubt
        if decision.action == "defer":
            until = now + timedelta(seconds=decision.delay)
            await self._persist(lambda: queue.defer(job, self.worker_id, until, decision.code, counts_attempt=False))
            await queue.block_account(account_id, until)  # nobody sends for this account before Telegram's wait is over
            await queue.release_unstarted(self.worker_id, [j.id for j in rest], until=until)
            return False
        if decision.action == "revoked":
            await self._persist(lambda: queue.mark_failed(job, self.worker_id, "session_revoked"))
            await self._connect.expire_session(user_id, account_id)
            await self._fail_all(rest, "session_revoked")
            return False
        await self._persist(lambda: queue.mark_failed(job, self.worker_id, decision.code))
        return True

    async def _batch_error(self, account_id: str, user_id: str, jobs: list[Job], exc: TelegramError) -> None:
        """A failure BEFORE any send (connect / dialog list): certain that nothing was delivered."""
        queue, now = self._queue, now_utc(self._clock)
        if isinstance(exc, SessionRevoked):
            await self._connect.expire_session(user_id, account_id)
            return await self._fail_all(jobs, "session_revoked")
        if isinstance(exc, FloodWait):
            until = now + timedelta(seconds=exc.seconds + self._rng() * policy.FLOOD_JITTER_MAX_SECONDS)
            await queue.block_account(account_id, until)
            await queue.release_unstarted(self.worker_id, [j.id for j in jobs], until=until)
            return
        # connect / dialog-list failure: certain that nothing was delivered, so a bounded retry with back-off is safe
        for job in jobs:
            decision = policy.decide(NetworkProblem(), attempts=job.attempts, max_attempts=job.max_attempts, rng=self._rng)
            if decision.action == "fail":
                await queue.mark_failed(job, self.worker_id, decision.code)
            else:
                await queue.defer(job, self.worker_id, now + timedelta(seconds=decision.delay), decision.code,
                                  counts_attempt=True)

    async def _fail_all(self, jobs: list[Job], code: str) -> None:
        for job in jobs:
            await self._queue.mark_failed(job, self.worker_id, code)

    async def _persist(self, write: Callable[[], Awaitable[bool]]) -> None:
        """Outcome writes are retried a few times: a message that WAS sent must not stay 'sending' because of a DB blip.
        If every try fails the lease expires and recover_stale() marks the job uncertain (never resent)."""
        for attempt in range(OUTCOME_WRITE_TRIES):
            try:
                await write()
                return
            except Exception:  # noqa: BLE001
                log.exception("could not record a job outcome (try %d)", attempt + 1)
                await self._sleep(1.0 * (attempt + 1))


class _Fail(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
