"""Executor + queue behaviour on the offline SQLite database with a fake Telegram network.

PostgreSQL-specific behaviour (real SKIP LOCKED contention) is covered by tests/test_postgres_integration.py, which CI runs
against a real PostgreSQL. Here the same SQL runs on SQLite (lock clause omitted), so logic, states and isolation are tested.
"""

import asyncio
import json
import unittest
from datetime import timedelta

from app.posts import composer
from app.posts.storage import make_storage
from app.scheduler import policy
from app.scheduler.executor import JobExecutor
from app.scheduler.queue import JobQueue
from app.scheduler.service import ScheduleInput, ScheduleInvalid, ScheduleNotFound
from app.telegram import errors as e
from app.telegram.groups import RawChat
from tests.posts_support import PostsBase, jpeg, png
from tests.test_web_groups import mega

ONCE = "2027-01-16T09:00"   # 03:00 UTC, the day after the test clock (2027-01-15 08:00 UTC)


class SchedBase(PostsBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        env = self.env
        self.user = env.admin_id
        self.queue = JobQueue(env.db, env.clock)
        self.sleeps = []

        async def fake_sleep(seconds):
            self.sleeps.append(seconds)
        self.fake_sleep = fake_sleep
        self.stop = asyncio.Event()
        self.executor = self.make_executor("worker-1")

    def make_executor(self, worker_id):
        env = self.env
        return JobExecutor(env.db, JobQueue(env.db, env.clock), env.ctx.telegram_connect, env.world.service(),
                           make_storage(env.settings, env.db, env.clock), env.settings, env.clock, worker_id=worker_id,
                           rng=lambda: 0.5, sleep=self.fake_sleep, stop=self.stop)

    async def schedule(self, post_id=None, groups=None, **kw):
        post_id = post_id or await self.make_post(groups=groups or (self.g_ok,))
        data = dict(kind="once", once_at=ONCE, timezone="Asia/Dhaka")
        data.update(kw)
        return post_id, await self.env.ctx.schedules.create(
            self.user, ScheduleInput(post_id=post_id, group_ids=set(groups or (self.g_ok,)), **data))

    def advance_to_due(self):
        self.env.clock.advance(24 * 3600)   # 2027-01-16 08:00 UTC, five hours after the 03:00 UTC run

    async def run_all(self, executor=None):
        executor = executor or self.executor
        n = 0
        while await executor.run_once():
            n += 1
            self.assertLess(n, 50)
        return n

    def jobs(self, **where):
        sql = "SELECT * FROM posting_jobs"
        rows = [dict(r) for r in self.env.db.conn.execute(sql + " ORDER BY scheduled_for, group_title")]
        return [r for r in rows if all(r[k] == v for k, v in where.items())]

    def only_job(self):
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1, jobs)
        return jobs[0]

    def logs(self, event=None):
        return [dict(r) for r in self.env.db.conn.execute("SELECT * FROM posting_logs ORDER BY created_at")
                if event is None or r["event"] == event]

    def due_now(self):
        """Run time reached (and not yet 'missed'): jump to just after 03:00 UTC."""
        self.env.clock.now = 1_800_000_000 + 19 * 3600 + 60  # 2027-01-16 03:01 UTC


class MaterialisationTests(SchedBase):
    async def test_nothing_is_created_before_the_run_time(self):
        await self.schedule()
        self.assertEqual(await self.queue.materialize_due(), 0)
        self.assertEqual(self.jobs(), [])

    async def test_one_job_per_target_with_stable_idempotency_key(self):
        post, sid = await self.schedule(groups=(self.g_ok, self.g_ok2))
        self.due_now()
        self.assertEqual(await self.queue.materialize_due(), 1)
        jobs = self.jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual({j["status"] for j in jobs}, {"scheduled"})
        self.assertEqual(len({j["idempotency_key"] for j in jobs}), 2)
        self.assertTrue(all(j["idempotency_key"].startswith(sid) for j in jobs))
        row = self.env.db.conn.execute("SELECT status, next_run_at FROM schedules WHERE id = ?", (sid,)).fetchone()
        self.assertEqual((row["status"], row["next_run_at"]), ("completed", None))  # once: finished after materialising

    async def test_materialising_twice_never_duplicates(self):
        await self.schedule()
        self.due_now()
        await asyncio.gather(self.queue.materialize_due(), JobQueue(self.env.db, self.env.clock).materialize_due())
        await self.queue.materialize_due()
        self.assertEqual(len(self.jobs()), 1)

    async def test_stale_pointer_cannot_recreate_an_occurrence(self):
        _, sid = await self.schedule(kind="daily", times="09:00")
        self.due_now()
        await self.queue.materialize_due()
        self.env.db.conn.execute("UPDATE schedules SET next_run_at = (SELECT scheduled_for FROM posting_jobs) WHERE id = ?", (sid,))
        self.env.db.conn.commit()
        await self.queue.materialize_due()     # the same occurrence again: the unique key refuses the duplicate job
        self.assertEqual(len(self.jobs()), 1)

    async def test_daily_schedule_advances_to_the_next_day(self):
        _, sid = await self.schedule(kind="daily", times="09:00")
        self.due_now()
        await self.queue.materialize_due()
        row = self.env.db.conn.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertTrue(str(row["next_run_at"]).startswith("2027-01-17 03:00"))
        self.assertTrue(str(row["last_run_at"]).startswith("2027-01-16 03:00"))

    async def test_recurrence_continues_after_a_failed_run(self):
        _, sid = await self.schedule(kind="daily", times="09:00")
        self.due_now()
        self.world.chats[111] = [mega(-1001, "My Marketing Group", default_send_banned=True)]
        await self.queue.materialize_due()
        await self.run_all()
        self.assertEqual(self.only_job()["status"], "failed")
        self.env.clock.now += 24 * 3600
        await self.queue.materialize_due()
        self.assertEqual(len(self.jobs()), 2)  # tomorrow's occurrence was still generated

    async def test_downtime_records_one_missed_job_and_skips_ahead(self):
        _, sid = await self.schedule(kind="daily", times="09:00")
        self.env.clock.now += 5 * 24 * 3600   # worker was down for five days
        await self.queue.materialize_due()
        job = self.only_job()
        self.assertEqual((job["status"], job["error_code"]), ("failed", "missed"))
        nxt = self.env.db.conn.execute("SELECT next_run_at FROM schedules WHERE id = ?", (sid,)).fetchone()[0]
        self.assertGreater(str(nxt), "2027-01-20")  # not five catch-up posts: jumps ahead
        self.assertEqual(self.sleeps, [])
        await self.run_all()
        self.assertEqual(self.world.sent, [])  # a missed post is NOT sent late


class ClaimAndRecoveryTests(SchedBase):
    async def test_claim_is_exclusive_per_account_and_job(self):
        await self.schedule(groups=(self.g_ok, self.g_ok2))
        self.due_now()
        await self.queue.materialize_due()
        first = await self.queue.claim_account_batch("w1")
        second = await self.queue.claim_account_batch("w2")
        self.assertIsNotNone(first)
        self.assertEqual(len(first[1]), 2)
        self.assertIsNone(second)                         # nothing left, and the account is locked anyway
        self.assertEqual({j["attempts"] for j in self.jobs()}, {1})

    async def test_account_lock_blocks_other_workers_until_released_or_expired(self):
        await self.schedule()
        self.due_now()
        await self.queue.materialize_due()
        acc = self.acc_alice
        self.env.db.conn.execute("UPDATE telegram_accounts SET send_locked_by='other', send_lock_expires_at=? WHERE id=?",
                                 (self.queue.now() + timedelta(minutes=5), acc))
        self.env.db.conn.commit()
        self.assertIsNone(await self.queue.claim_account_batch("w1"))
        self.env.clock.advance(601)  # lock expired (the job is still within the 6 h window)
        self.assertIsNotNone(await self.queue.claim_account_batch("w1"))

    async def test_paused_schedule_jobs_are_not_claimed_and_resume_after(self):
        _, sid = await self.schedule()
        self.due_now()
        await self.queue.materialize_due()
        self.env.db.conn.execute("UPDATE schedules SET status='paused' WHERE id=?", (sid,))
        self.env.db.conn.commit()
        self.assertIsNone(await self.queue.claim_account_batch("w1"))
        self.env.db.conn.execute("UPDATE schedules SET status='completed' WHERE id=?", (sid,))
        self.env.db.conn.commit()
        self.assertIsNotNone(await self.queue.claim_account_batch("w1"))

    async def test_expired_lease_before_send_returns_to_the_queue(self):
        await self.schedule()
        self.due_now()
        await self.queue.materialize_due()
        await self.queue.claim_account_batch("dead-worker")
        self.env.clock.advance(policy.LEASE_SECONDS + 1)
        stats = await self.queue.recover_stale()
        self.assertEqual(stats["requeued"], 1)
        job = self.only_job()
        self.assertEqual((job["status"], job["locked_by"]), ("scheduled", None))
        self.assertEqual(job["attempts"], 1)              # the crashed claim still counts: no endless crash loop
        await self.run_all()
        self.assertEqual(len(self.world.sent), 1)

    async def test_expired_lease_after_send_started_becomes_uncertain_and_is_never_resent(self):
        await self.schedule()
        self.due_now()
        await self.queue.materialize_due()
        _, jobs = await self.queue.claim_account_batch("dead-worker")
        self.assertTrue(await self.queue.begin_send(jobs[0], "dead-worker"))   # ... and the worker dies here
        self.env.clock.advance(policy.LEASE_SECONDS + 1)
        self.assertEqual((await self.queue.recover_stale())["uncertain"], 1)
        job = self.only_job()
        self.assertEqual((job["status"], job["delivery_state"], job["error_code"]), ("failed", "uncertain", "delivery_uncertain"))
        await self.run_all()
        self.assertEqual(self.world.sent, [])              # NOT resent automatically

    def lock_of(self, account):
        return self.env.db.conn.execute("SELECT send_locked_by FROM telegram_accounts WHERE id=?", (account,)).fetchone()[0]

    async def test_dead_workers_account_lock_is_cleared_even_if_the_lock_itself_has_not_expired(self):
        await self.schedule()
        self.due_now()
        await self.queue.materialize_due()
        account, jobs = await self.queue.claim_account_batch("dead", lease=1)   # job lease 1 s, account lock 300 s
        self.env.clock.advance(5)
        self.assertEqual(self.lock_of(account), "dead")
        await self.queue.recover_stale()
        self.assertIsNone(self.lock_of(account))
        self.assertEqual(self.only_job()["status"], "scheduled")

    async def test_a_live_workers_account_lock_is_not_cleared(self):
        await self.schedule(groups=(self.g_ok, self.g_ok2))
        self.due_now()
        await self.queue.materialize_due()
        account, jobs = await self.queue.claim_account_batch("alive", batch=1)  # one job claimed, lease still valid
        await self.queue.recover_stale()
        self.assertEqual(self.lock_of(account), "alive")
        self.assertEqual(self.only_job_status("processing"), 1)
        self.assertIsNone(await self.queue.claim_account_batch("other"))        # the account is still exclusive

    async def test_a_fresh_lock_without_jobs_and_other_accounts_are_untouched(self):
        await self.schedule()
        self.env.db.conn.execute("UPDATE telegram_accounts SET send_locked_by='fresh', send_lock_expires_at=? WHERE id=?",
                                 (self.queue.now() + timedelta(minutes=5), self.acc_alice))
        self.env.db.conn.execute("UPDATE telegram_accounts SET send_locked_by='bob-worker', send_lock_expires_at=? WHERE id=?",
                                 (self.queue.now() + timedelta(minutes=5), self.acc_bob))
        self.env.db.conn.commit()
        await self.queue.recover_stale()
        self.assertEqual((self.lock_of(self.acc_alice), self.lock_of(self.acc_bob)), ("fresh", "bob-worker"))

    async def test_expired_lock_without_any_job_is_still_cleared_by_its_own_expiry(self):
        self.env.db.conn.execute("UPDATE telegram_accounts SET send_locked_by='gone', send_lock_expires_at=? WHERE id=?",
                                 (self.queue.now() - timedelta(seconds=1), self.acc_alice))
        self.env.db.conn.commit()
        await self.queue.recover_stale()
        self.assertIsNone(self.lock_of(self.acc_alice))

    def only_job_status(self, status):
        return len(self.jobs(status=status))

    async def test_a_job_that_keeps_killing_workers_is_failed(self):
        await self.schedule()
        self.due_now()
        await self.queue.materialize_due()
        for _ in range(5):
            await self.queue.claim_account_batch("dead")
            self.env.clock.advance(policy.LEASE_SECONDS + 1)
            await self.queue.recover_stale()
            self.env.db.conn.execute("UPDATE telegram_accounts SET send_locked_by=NULL")
            self.env.db.conn.commit()
        job = self.only_job()
        self.assertEqual((job["status"], job["error_code"]), ("failed", "worker_crashed"))

    async def test_two_workers_share_the_jobs_of_two_accounts_without_overlap(self):
        # second account of the same admin user is not needed: Bob's account has its own job
        await self.schedule()
        post_b = await self.make_post(self.bob, self.acc_bob, groups=(self.g_bob,))
        await self.env.ctx.schedules.create(self.env.db.conn.execute("SELECT user_id FROM telegram_accounts WHERE id=?",
                                            (self.acc_bob,)).fetchone()[0],
                                            ScheduleInput(post_id=post_b, group_ids={self.g_bob}, kind="once", once_at=ONCE))
        self.due_now()
        await self.queue.materialize_due()
        a = await self.queue.claim_account_batch("w1")
        b = await self.queue.claim_account_batch("w2")
        self.assertNotEqual(a[0], b[0])
        self.assertEqual(len(a[1]) + len(b[1]), 2)


class ExecutorTests(SchedBase):
    async def go(self, **kw):
        post, sid = await self.schedule(**kw)
        self.due_now()
        await self.queue.materialize_due()
        return post, sid

    async def test_success_sends_the_composed_text_with_exactly_one_footer(self):
        post, _ = await self.go()
        await self.run_all()
        job = self.only_job()
        self.assertEqual((job["status"], job["delivery_state"]), ("posted", "sent"))
        self.assertEqual(len(json.loads(job["telegram_message_ids"])), 1)
        (chat, text, has_image, _s), = self.world.sent
        self.assertEqual(chat, -1001)
        self.assertEqual(text.count(composer._footer_text_line()), 1)
        self.assertEqual(text, composer.compose("Hello everyone!", has_image=False).text)
        preview = (await self.env.ctx.posts.preview(self.user, post))["message"].text
        self.assertEqual(text, preview)                       # preview and the sent message are the same composition
        self.assertEqual(self.row(post)["status"], "sent")

    async def test_stored_text_has_no_footer_and_user_pasted_footer_is_not_doubled(self):
        pasted = "Hi\n\n" + composer.footer_block()
        post = await self.make_post(body=pasted, groups=(self.g_ok,))
        await self.env.ctx.schedules.create(self.user, ScheduleInput(post_id=post, group_ids={self.g_ok}, kind="once", once_at=ONCE))
        self.due_now()
        await self.queue.materialize_due()
        self.assertNotIn(composer._footer_text_line(), self.only_job()["text_snapshot"])
        await self.run_all()
        self.assertEqual(self.world.sent[0][1].count(composer._footer_text_line()), 1)

    async def test_photo_post_sends_an_image_with_the_caption_and_footer(self):
        post = await self.make_post(groups=(self.g_ok,), files={"image": ("a.png", png())})
        await self.env.ctx.schedules.create(self.user, ScheduleInput(post_id=post, group_ids={self.g_ok}, kind="once", once_at=ONCE))
        self.due_now()
        await self.queue.materialize_due()
        await self.run_all()
        (_c, text, has_image, _s), = self.world.sent
        self.assertTrue(has_image)
        self.assertEqual(text.count(composer._footer_text_line()), 1)
        self.assertLessEqual(composer.utf16_units(text), 1024)

    async def test_caption_limit_is_enforced_on_the_final_text_at_send_time(self):
        post = await self.make_post(groups=(self.g_ok,), files={"image": ("a.jpg", jpeg())})
        await self.env.ctx.schedules.create(self.user, ScheduleInput(post_id=post, group_ids={self.g_ok}, kind="once", once_at=ONCE))
        self.due_now()
        await self.queue.materialize_due()
        self.env.db.conn.execute("UPDATE posting_jobs SET text_snapshot = ?", ("x" * 1000,))  # body + footer > 1024
        self.env.db.conn.commit()
        await self.run_all()
        self.assertEqual(self.only_job()["error_code"], "message_invalid")
        self.assertEqual(self.world.sent, [])

    async def test_restricted_group_is_rechecked_live_and_fails_without_sending(self):
        await self.go()
        self.world.chats[111] = [mega(-1001, "My Marketing Group", member_send_banned=True), *self.world.chats[111][1:]]
        await self.run_all()
        job = self.only_job()
        self.assertEqual((job["status"], job["error_code"], job["delivery_state"]), ("failed", "no_permission", "not_sent"))
        self.assertEqual(self.world.sent, [])
        grp = self.env.db.conn.execute("SELECT permission_status, is_enabled FROM telegram_groups WHERE id=?", (self.g_ok,)).fetchone()
        self.assertEqual((grp[0], grp[1]), ("restricted", 0))

    async def test_group_that_vanished_fails_as_unavailable(self):
        await self.go()
        self.world.chats[111] = [c for c in self.world.chats[111] if c.chat_id != -1001]
        await self.run_all()
        self.assertEqual(self.only_job()["error_code"], "group_unavailable")

    async def test_revoked_session_stops_everything_and_asks_for_reconnect(self):
        await self.go(groups=None)
        self.world.revoke_everything()
        await self.run_all()
        self.assertEqual(self.only_job()["error_code"], "session_revoked")
        acc = self.env.db.conn.execute("SELECT status FROM telegram_accounts WHERE id=?", (self.acc_alice,)).fetchone()[0]
        self.assertEqual(acc, "session_expired")
        self.assertNotIn(("sign_in_code", ""), self.world.calls)   # the worker never tries to log in again

    async def test_disconnected_account_fails_jobs_without_touching_telegram(self):
        await self.go()
        await self.env.ctx.telegram_connect.disconnect(self.user, self.acc_alice)
        calls = len(self.world.calls)
        await self.run_all()
        self.assertEqual(self.only_job()["error_code"], "account_unavailable")
        self.assertEqual(len(self.world.calls), calls)

    async def test_floodwait_defers_without_using_an_attempt_and_blocks_the_account(self):
        await self.go()
        self.world.send_script = [e.FloodWait(600)]
        await self.run_all()
        job = self.only_job()
        self.assertEqual((job["status"], job["error_code"], job["attempts"]), ("waiting", "rate_limited", 0))
        self.assertGreaterEqual(str(job["next_attempt_at"]), str(self.queue.now() + timedelta(seconds=600))[:19].replace("T", " "))
        self.assertEqual(self.world.sent, [])
        self.assertIsNone(await self.queue.claim_account_batch("w9"))       # blocked until Telegram's wait is over
        self.env.clock.advance(700)
        await self.run_all()
        self.assertEqual(self.only_job()["status"], "posted")
        self.assertEqual(self.world.sent[0][1].count(composer._footer_text_line()), 1)   # footer once after the retry

    async def test_floodwait_defers_the_rest_of_the_batch_too(self):
        await self.go(groups=(self.g_ok, self.g_ok2))
        self.world.send_script = [e.FloodWait(300)]
        await self.run_all()
        self.assertEqual(sorted(j["status"] for j in self.jobs()), ["scheduled", "waiting"])
        self.assertEqual(self.world.sent, [])

    async def test_network_down_retries_with_backoff_then_gives_up_without_sending(self):
        await self.go()
        self.world.down = True
        for expected_attempts in range(1, 6):
            await self.run_all()
            job = self.only_job()
            self.assertEqual(job["attempts"], expected_attempts)
            if expected_attempts < 5:
                self.assertEqual(job["status"], "waiting")
                self.env.clock.advance(1000)
        self.assertEqual((job["status"], job["error_code"], job["delivery_state"]), ("failed", "network_error", "not_sent"))
        self.assertEqual(self.world.sent, [])

    async def test_ambiguous_send_is_uncertain_and_is_not_resent(self):
        await self.go()
        self.world.send_script = [("lost", e.DeliveryUncertain())]   # delivered, but the answer never arrived
        await self.run_all()
        job = self.only_job()
        self.assertEqual((job["status"], job["delivery_state"], job["error_code"]), ("failed", "uncertain", "delivery_uncertain"))
        self.assertEqual(len(self.world.sent), 1)
        self.env.clock.advance(7200)
        await self.run_all()
        self.assertEqual(len(self.world.sent), 1)      # no duplicate
        self.assertTrue(self.logs("uncertain"))

    async def test_connection_error_during_send_is_treated_as_uncertain(self):
        await self.go()
        self.world.send_script = [ConnectionResetError()]
        await self.run_all()
        self.assertEqual(self.only_job()["delivery_state"], "uncertain")

    async def test_partial_multi_target_success_is_tracked_per_target(self):
        await self.go(groups=(self.g_ok, self.g_ok2))
        self.world.send_script = [None, e.NoPostPermission()]
        await self.run_all()
        states = sorted((j["status"], j["error_code"]) for j in self.jobs())
        self.assertEqual(states, [("failed", "no_permission"), ("posted", None)])
        self.assertEqual(len(self.world.sent), 1)

    async def test_invalid_media_from_telegram_is_permanent(self):
        post = await self.make_post(groups=(self.g_ok,), files={"image": ("a.png", png())})
        await self.env.ctx.schedules.create(self.user, ScheduleInput(post_id=post, group_ids={self.g_ok}, kind="once", once_at=ONCE))
        self.due_now()
        await self.queue.materialize_due()
        self.world.send_script = [e.InvalidMedia()]
        await self.run_all()
        self.assertEqual(self.only_job()["error_code"], "invalid_media")

    async def test_missing_image_bytes_fail_before_sending(self):
        post = await self.make_post(groups=(self.g_ok,), files={"image": ("a.png", png())})
        await self.env.ctx.schedules.create(self.user, ScheduleInput(post_id=post, group_ids={self.g_ok}, kind="once", once_at=ONCE))
        self.due_now()
        await self.queue.materialize_due()
        self.env.db.conn.execute("DELETE FROM media_blobs")
        self.env.db.conn.commit()
        await self.run_all()
        self.assertEqual(self.only_job()["error_code"], "media_missing")

    async def test_cancelled_job_is_not_sent(self):
        _, sid = await self.go()
        _, jobs = await self.queue.claim_account_batch("w1")           # claimed, not yet sending
        await self.env.ctx.schedules.cancel(self.user, sid)
        self.assertFalse(await self.queue.begin_send(jobs[0], "w1"))   # the last gate refuses
        self.assertEqual(self.only_job()["status"], "cancelled")
        self.assertEqual(self.world.sent, [])

    async def test_graceful_shutdown_releases_unstarted_jobs(self):
        await self.go(groups=(self.g_ok, self.g_ok2))
        self.stop.set()
        await self.executor.run_once()
        jobs = self.jobs()
        self.assertEqual({j["status"] for j in jobs}, {"scheduled"})
        self.assertEqual({j["locked_by"] for j in jobs}, {None})
        self.assertEqual({j["attempts"] for j in jobs}, {0})
        self.assertIsNone(self.env.db.conn.execute("SELECT send_locked_by FROM telegram_accounts WHERE id=?", (self.acc_alice,)).fetchone()[0])

    async def test_hourly_cap_defers_instead_of_failing(self):
        self.env.settings = self.env.settings.__class__(**{**self.env.settings.__dict__, "max_posts_per_hour": 1})
        self.executor = self.make_executor("worker-1")
        self.executor._settings = self.env.settings
        await self.go(groups=(self.g_ok, self.g_ok2))
        await self.run_all()
        self.assertEqual(sorted(j["status"] for j in self.jobs()), ["posted", "scheduled"])
        self.assertEqual(len(self.world.sent), 1)

    async def test_pacing_sleep_between_sends_of_one_account(self):
        await self.go(groups=(self.g_ok, self.g_ok2))
        await self.run_all()
        self.assertEqual(self.sleeps, [self.env.settings.post_min_interval_seconds])

    async def test_outcome_write_failure_after_send_ends_uncertain_not_resent(self):
        await self.go()
        orig = JobQueue.mark_posted

        async def broken(self_, *a, **k):
            raise RuntimeError("database down")
        JobQueue.mark_posted = broken
        try:
            await self.run_all()
        finally:
            JobQueue.mark_posted = orig
        self.assertEqual(self.only_job()["delivery_state"], "sending")      # the send happened, the result was lost
        self.env.clock.advance(policy.LEASE_SECONDS + 1)
        await self.queue.recover_stale()
        self.assertEqual(self.only_job()["delivery_state"], "uncertain")
        await self.run_all()
        self.assertEqual(len(self.world.sent), 1)

    async def test_no_bot_api_is_used_anywhere_in_the_scheduler(self):
        import pathlib
        for path in pathlib.Path("app/scheduler").glob("*.py"):
            text = path.read_text()
            self.assertNotIn("api.telegram.org", text)
            self.assertNotRegex(text, r"(?i)bot_token|botapi")
        self.assertNotIn("api.telegram.org", pathlib.Path("app/telegram/service.py").read_text())
        await self.go()
        await self.run_all()
        self.assertTrue(all(c[0] in ("list_groups", "send_post", "current_profile", "send_code", "sign_in_code", "log_out")
                            for c in self.world.calls))

    async def test_user_resolution_of_uncertain_and_failed_jobs(self):
        await self.go()
        self.world.send_script = [("lost", e.DeliveryUncertain())]
        await self.run_all()
        jid = self.only_job()["id"]
        svc = self.env.ctx.schedules
        await svc.resolve_job(self.user, jid, "confirm_sent")
        self.assertEqual(self.only_job()["status"], "posted")
        with self.assertRaises(Exception):
            await svc.resolve_job(self.user, jid, "retry")       # already posted: conflict
        with self.assertRaises(ScheduleNotFound):
            await svc.resolve_job(self.env.db.conn.execute("SELECT user_id FROM telegram_accounts WHERE id=?",
                                  (self.acc_bob,)).fetchone()[0], jid, "retry")   # another user's job


class ValidationAndIsolationTests(SchedBase):
    async def test_cross_user_and_injected_ids_look_like_missing(self):
        post = await self.make_post(groups=(self.g_ok,))
        svc, bob = self.env.ctx.schedules, self.env.db.conn.execute("SELECT user_id FROM telegram_accounts WHERE id=?", (self.acc_bob,)).fetchone()[0]
        with self.assertRaises(Exception) as c1:
            await svc.create(bob, ScheduleInput(post_id=post, group_ids={self.g_ok}, kind="once", once_at=ONCE))
        self.assertEqual(type(c1.exception).__name__, "PostNotFound")
        with self.assertRaises(ScheduleNotFound):   # a group of another user is injected into Alice's own post
            await svc.create(self.user, ScheduleInput(post_id=post, group_ids={self.g_bob}, kind="once", once_at=ONCE))
        with self.assertRaises(ScheduleNotFound):
            await svc.create(self.user, ScheduleInput(post_id=post, group_ids={"not-a-uuid"}, kind="once", once_at=ONCE))
        _, sid = await self.schedule()
        for action in ("pause", "resume", "cancel"):
            with self.assertRaises(ScheduleNotFound):
                await getattr(svc, action)(bob, sid)
        with self.assertRaises(ScheduleNotFound):
            await svc.get(bob, sid)
        self.assertEqual(await svc.list_schedules(bob), [])

    async def test_validation_errors_are_collected_and_nothing_is_written(self):
        post = await self.make_post(groups=(self.g_ok,))
        with self.assertRaises(ScheduleInvalid) as ctx:
            await self.env.ctx.schedules.create(self.user, ScheduleInput(post_id=post, group_ids=set(), kind="once", once_at="2020-01-01T00:00"))
        self.assertGreaterEqual(len(ctx.exception.errors), 2)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 0)
        self.assertEqual(self.row(post)["status"], "draft")

    async def test_scheduling_locks_the_draft_and_cancelling_unlocks_it(self):
        post, sid = await self.schedule()
        self.assertEqual(self.row(post)["status"], "scheduled")
        r = await self.save_post(self.alice, post, body="changed", groups=(self.g_ok,))
        self.assertEqual(r.status, 409)
        with self.assertRaises(Exception):
            await self.env.ctx.schedules.create(self.user, ScheduleInput(post_id=post, group_ids={self.g_ok}, kind="once", once_at=ONCE))
        await self.env.ctx.schedules.cancel(self.user, sid)
        self.assertEqual(self.row(post)["status"], "draft")

    async def test_pause_resume_cancel_lifecycle(self):
        svc = self.env.ctx.schedules
        _, sid = await self.schedule(kind="daily", times="09:00")
        await svc.pause(self.user, sid)
        self.assertEqual((await svc.get(self.user, sid))["state"], "Paused")
        with self.assertRaises(Exception):
            await svc.pause(self.user, sid)
        self.env.clock.advance(3 * 24 * 3600)      # occurrences during the pause are skipped, not caught up
        await svc.resume(self.user, sid)
        nxt = (await svc.get(self.user, sid))["next_run_at"]
        self.assertGreater(str(nxt), "2027-01-18")
        self.assertEqual(await self.queue.materialize_due(), 0)
        self.assertEqual(await svc.cancel(self.user, sid), 0)
        self.assertEqual((await svc.get(self.user, sid))["state"], "Cancelled")

    async def test_cancel_cancels_pending_jobs_of_a_finished_once_schedule(self):
        svc = self.env.ctx.schedules
        _, sid = await self.schedule(groups=(self.g_ok, self.g_ok2))
        self.due_now()
        await self.queue.materialize_due()
        self.assertEqual(await svc.cancel(self.user, sid), 2)
        self.assertEqual({j["status"] for j in self.jobs()}, {"cancelled"})
        await self.run_all()
        self.assertEqual(self.world.sent, [])


if __name__ == "__main__":
    unittest.main()
