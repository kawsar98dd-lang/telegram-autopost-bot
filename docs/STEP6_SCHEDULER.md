# Step 6 - Scheduler and background worker

Posts that were saved as drafts in Step 5 can now be scheduled. A separate **worker process** sends them through the
connected Telegram **user account (MTProto / Telethon)** at the chosen time. The Telegram Bot API is not used anywhere.

## Architecture

```
web (FastAPI)                         PostgreSQL (single source of truth)                 worker
 /schedules, /jobs  --writes-->  schedules, schedule_targets, posting_jobs, posting_logs  <--claims--  python -m app.workers.main
 never talks to Telegram                                                                       talks to Telegram (Telethon)
```

* **Web**: creates, lists, pauses, resumes and cancels schedules; shows jobs. It cannot send anything.
* **Worker** (every `WORKER_POLL_SECONDS`, default 5): (1) license check, (2) `recover_stale`, (3) `materialize_due`
  (due schedules become jobs), (4) claims jobs account by account and sends them.
* No Redis, no in-memory timers: stop/restart anything at any time; the next loop continues from PostgreSQL.
* Files: `app/scheduler/{recurrence,policy,queue,executor,service}.py`, `app/workers/main.py`, `app/web/schedule_routes.py`,
  migration `0006_scheduler.sql`.

### Vocabulary (what the user sees)

| Word | Meaning |
|---|---|
| Draft | A saved post (Step 5). Editable. Nothing is planned. |
| Schedule | "Send this draft at these times to these groups". The draft becomes read-only (status `scheduled`). |
| Job | One post for ONE group for ONE run time (table `posting_jobs`). |
| Queued / Waiting to retry | Job waiting for its time or for a retry. |
| Sending now | A worker has started the request to Telegram. |
| Posted / Failed / Cancelled | Final states. A failed job with "Uncertain" needs a decision (see below). |

## Schedule types and their exact meaning

* **One time**: a local date and time in the chosen timezone; must be in the future (at most about 3 years ahead). Runs once.
* **Every day**: 1 to 24 local `HH:MM` times per day.
* **Selected weekdays**: chosen weekdays (Mon..Sun) and local times.
* **Custom**: first run = start time, then every N minutes of *elapsed* time (N x 60 s, minimum 15 minutes, maximum 366 days;
  entered as minutes, hours or days), optionally until an end time. No expressions, no cron syntax: only these numbers.
* **Timezone**: an IANA name (default `Asia/Dhaka`, editable on every schedule). All instants are stored in UTC. Daily/weekly
  times follow the local wall clock including daylight saving: a local time that does not exist (clock jumps forward) runs at the first
  moment after the jump; a local time that exists twice runs once, at its first occurrence. Bangladesh has no daylight saving.
* Occurrences that fall into a pause are skipped on resume, not caught up.
* A post can have one schedule at a time. Cancelling a schedule makes the draft editable again (if nothing was sent). To send
  a finished post again, create a new draft.

## Job life cycle

```
scheduled --claim--> processing --begin_send--> (sending) --Telegram answered OK--> posted
    ^                    |                           |--definitive refusal--------> failed (not_sent)
    |                    |--before sending: retry--> waiting --time passes--> scheduled
    |                    |--FloodWait: deferred------^
    +--lease expired, send never started (recovered)
                                                     |--no answer / unknown error--> failed (uncertain)
 scheduled|waiting|processing(not sending) --user cancels--> cancelled
```

`posting_jobs.delivery_state`: `not_sent`, `sending`, `sent`, `uncertain`. A job that expired while `sending` becomes `failed + uncertain`.

## Duplicate prevention and its limits

Telegram has **no idempotency key** for sending, and a database commit cannot be made atomic with a network request.
The design is therefore *at-least-once job processing with the strongest duplicate protection that is achievable*, not exactly-once:

1. **No duplicate occurrences**: `idempotency_key = schedule:run-time:group` is UNIQUE; the schedule row is locked
   (`FOR UPDATE SKIP LOCKED`) while its jobs are created and `next_run_at` is advanced with a compare-and-set.
2. **No parallel execution**: jobs are claimed with one atomic `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`;
   an account is served by one worker at a time (atomic account lock).
3. **Send intent is persisted before the request** (`delivery_state = 'sending'`, committed), the result right after it.
4. **Never resent on doubt**: if the outcome is unknown (connection lost / timeout after sending, unknown error, worker crash while
   `sending`, database failure while recording the result) the job becomes `failed` + `uncertain` and is NOT retried automatically.
   The user checks the group and chooses **It was delivered** or **Send again**.
5. Retries only happen when it is certain that nothing was delivered (Telegram refused with a definitive answer, or the
   connection failed before the request was sent).
6. The footer is not stored in the job; every send rebuilds the text with `composer.compose()` (idempotent), so retries cannot double it.

**Residual risk**: if the user chooses *Send again* for an uncertain job and the first message did arrive, the group gets two
copies. A message can also exist twice if Telegram itself processes a request twice. Nothing here can remove that risk entirely.

## Retry and FloodWait policy

| Situation | Action | Counts as attempt |
|---|---|---|
| FloodWait / slow-mode wait N s | Job `waiting` until now + N + 0..5 s jitter; the account is blocked until then; other jobs of the account are released. The worker does not sleep. | no |
| Network failure before the request is sent | Retry after 30 s, 60 s, 120 s ... (cap 15 min), +-20 % jitter | yes |
| Network failure at attempt 5 (`max_attempts`) | `failed` (`network_error`, confirmed not sent) | - |
| No permission, group gone, invalid image, message invalid, unknown refusal | `failed` immediately (needs attention) | - |
| Session revoked | `failed` (`session_revoked`), account marked "session expired", remaining jobs of the account failed; the worker never asks for a code/password | - |
| Account disconnected | `failed` (`account_unavailable`) | - |
| Unknown error or lost answer during a send | `failed` + `uncertain` | - |
| Occurrence not started within 6 h of its time (worker was down) | `failed` (`missed`), not sent late; only the first missed occurrence of a schedule is recorded | - |
| More than `MAX_POSTS_PER_HOUR` posts for one account | remaining jobs wait until the hour has passed | no |
| Job abandoned by a crashed worker 5 times | `failed` (`worker_crashed`) | - |

Between two sends of one account the worker waits `POST_MIN_INTERVAL_SECONDS`. FloodWaits and bans cannot be prevented
completely; the settings only reduce the risk.

## Per-job checks right before sending

Account is connected and owned by the job's user; the session decrypts; the group belongs to the same user AND account and is still a
target of the schedule; Telegram's **current** view of the chat is assessed with the Step 4 rules (a stored permission is never trusted;
a failed live check also marks the group not postable in Step 4); the final text is composed by `composer.compose()` and checked
(caption limit for photos); the image is read from storage and validated again; the job must still be ours and not cancelled.
The worker never joins/leaves chats and never changes rights.

## Deployment

* **Docker Compose** (`docker-compose.yml`): `db`, `migrate` (one-off), `web`, `worker` already exist; `docker compose up -d --build` runs everything. Migration `0006` is applied by the `migrate` service.
* **Render**: the Step 3-5 test blueprint (`render.yaml`) declares **only a web service** (free plan). **A web service alone never sends anything.**
  To really send you need a second service of type *Background Worker* (Render charges for it), same Docker image, start command
  `python -m app.workers.main`, with the same `DATABASE_URL`, `SESSION_ENCRYPTION_KEY`, `APP_SECRET`, `APP_ENV`, `LICENSE_ENFORCEMENT`
  values as the web service. Do not run worker and web with different `SESSION_ENCRYPTION_KEY` values. The blueprint file was
  deliberately not changed in this step so the existing deployment keeps working.
* Several worker processes may run at once; they never claim the same job.
* New environment variables: **none**. Existing ones now used: `WORKER_POLL_SECONDS`, `POST_MIN_INTERVAL_SECONDS`, `MAX_POSTS_PER_HOUR`.
* New dependency: `tzdata` (timezone database, pure Python data package).

## Operations: diagnosing problems (SQL, read-only)

```sql
-- stuck: claimed but lease expired (the worker recovers these within ~30 s of running)
SELECT id, locked_by, lease_expires_at FROM posting_jobs WHERE status='processing' AND lease_expires_at < now();
-- uncertain outcomes
SELECT id, group_title, send_started_at FROM posting_jobs WHERE delivery_state='uncertain';
-- failures by reason
SELECT error_code, count(*) FROM posting_jobs WHERE status='failed' GROUP BY 1;
-- FloodWait / blocked accounts
SELECT id, send_blocked_until FROM telegram_accounts WHERE send_blocked_until > now();
-- audit trail of one job
SELECT created_at, level, event, message, details FROM posting_logs WHERE job_id='...' ORDER BY created_at;
```

Invalid sessions: account status `session_expired` on the Telegram accounts page, reconnect there. Missing permissions: job error
`no_permission`, check the account's rights in the group, refresh Groups. Audit rows are never deleted automatically and contain ids and
codes only, never message text, sessions or credentials. The worker log is redacted by the existing formatter.

## Tests

`tests/test_recurrence.py` (maths, DST, validation), `test_scheduler_policy.py` (classification), `test_scheduler_queue.py` (materialisation,
claims, recovery, executor with a fake Telegram network, isolation), `test_web_schedules.py` (pages, CSRF, ownership),
`test_postgres_scheduler.py` (real PostgreSQL: migration on an existing database, real SKIP LOCKED contention, constraints, recovery).
No test sends a real message. The offline tests run the same SQL on SQLite (the `FOR UPDATE SKIP LOCKED` clause exists only on PostgreSQL).

## Manual verification on Render (needs the worker service, see above)

1. Open the Render dashboard, confirm the web service is *Live* and the worker service is *Live* with no errors in its log.
2. Log in; `/schedules` and `/jobs` open (no "upcoming release" text).
3. Open a draft preview and press **Schedule this post**; the footer is visible in the preview box.
4. Choose a test group you own, "One time", 3 minutes ahead, press **Review**, check the shown times, press **Save schedule**.
5. The schedule page says *Active*; the draft cannot be edited any more.
6. Pause it, resume it, then cancel it; each message appears. Confirm the draft is editable again.
7. Check that `/jobs` shows no secrets or error traces.

## Manual Telegram acceptance test (separate from CI, uses your own account and a test group)

1. Make a private test group where your account may post. Connect the account, refresh groups, select the group, create a draft.
2. Schedule a one-time post 3 minutes ahead. Expect exactly one message with the footer once, after the time, in the group.
3. Repeat with a JPG: the image appears with the caption and the footer once.
4. Schedule a daily post 5 minutes ahead and cancel it before the time: nothing is posted.
5. Remove your account's right to write in the test group and let a post run: the job fails with "does not allow this account to post".
6. Restart the worker while a post is waiting: it is still sent once afterwards.

## Known limitations

* NOT VERIFIED against live Telegram (only a fake network was available): entity resolution after a fresh login, FloodWait values and
  error class names in real life. Run the acceptance test.
* Telegram offers no idempotent send: see the residual duplicate risk above.
* A Telethon error class that is not in `app/telegram/errors.py` is treated as "uncertain" (safe, but needs a manual decision).
* One schedule per draft; targets of a schedule cannot be edited afterwards (cancel and create a new schedule).
* The database does not enforce "the group belongs to the same account as the schedule" as a foreign key (the existing tables cannot
  receive such a composite key portably); the service layer and the worker check it on every operation.
* Image bytes must be in the storage backend the worker is configured for (`MEDIA_STORAGE`); `local` storage requires a shared volume.
* Free Render plans have no background workers and sleep when idle; use a paid worker or the Docker deployment for real posting.
* The pages `Logs` is still a placeholder; history is shown on `/jobs` and on each schedule.
