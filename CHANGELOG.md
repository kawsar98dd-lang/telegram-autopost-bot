# Changelog

## Step 6 - Scheduler and background worker
* Migration `0006_scheduler.sql` (additive): `schedules.cancelled_at`, `posting_jobs.delivery_state/post_id/finished_at`, account send lock and FloodWait block columns, indexes.
* `app/scheduler/`: recurrence maths (once, daily, weekly, custom; IANA timezones, DST), policy, PostgreSQL queue (SKIP LOCKED, leases, recovery), executor, schedule service.
* `app/telegram/`: `PostingSession`, `send_post` (Telethon, no parse mode), send error classification (`map_send_exception`).
* `app/workers/main.py`: worker loop (recover, materialise, claim, send, graceful shutdown).
* Web: `/schedules`, `/schedules/new/{post}`, `/schedules/{id}` (pause/resume/cancel), `/jobs` (history, uncertain-job resolution); "Schedule this post" on the preview.
* Tests: recurrence, policy, queue/executor, web, PostgreSQL integration; CI minimum counts in `scripts/ci_verify.py`.
* `requirements.txt`: `tzdata`. Docs: `docs/STEP6_SCHEDULER.md`, `docs/STEP6_GITHUB_ANDROID_GUIDE_BN.md`.
* Test support: `SqliteDb.dialect`, datetime adapter; `FakeTelegram.send_script/sent`; migration lists in two existing tests.
