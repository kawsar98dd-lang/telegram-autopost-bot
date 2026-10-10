# Changelog

## Offline signed licensing
* New `app/licensing/offline.py` (Ed25519 license file, public key only), selected by `app/licensing/setup.py`; fixes the production start-up blocker (empty license server URL/public key made every production start fail). Production still enforces; a build without a valid public key refuses to start with an actionable message.
* Settings `LICENSE_FILE`, `LICENSE_FILE_CONTENT`; `/activate` explains where to put the file.
* Seller tools: `license_server/keygen.py` (key outside project, mode 600), `license_server/issue_license.py`, `scripts/build_customer_zip.py` (safe customer ZIP + SHA-256).
* Docs: `docs/LICENSING.md`. Tests: `test_offline_license.py`, `test_customer_release.py`.

## Step 6 - Scheduler and background worker
* Migration `0006_scheduler.sql` (additive): `schedules.cancelled_at`, `posting_jobs.delivery_state/post_id/finished_at`, account send lock and FloodWait block columns, indexes.
* `app/scheduler/`: recurrence maths (once, daily, weekly, custom; IANA timezones, DST), policy, PostgreSQL queue (SKIP LOCKED, leases, recovery), executor, schedule service.
* `app/telegram/`: `PostingSession`, `send_post` (Telethon, no parse mode), send error classification (`map_send_exception`).
* `app/workers/main.py`: worker loop (recover, materialise, claim, send, graceful shutdown).
* Web: `/schedules`, `/schedules/new/{post}`, `/schedules/{id}` (pause/resume/cancel), `/jobs` (history, uncertain-job resolution); "Schedule this post" on the preview.
* Tests: recurrence, policy, queue/executor, web, PostgreSQL integration; CI minimum counts in `scripts/ci_verify.py`.
* `requirements.txt`: `tzdata`. Docs: `docs/STEP6_SCHEDULER.md`, `docs/STEP6_GITHUB_ANDROID_GUIDE_BN.md`.
* Test support: `SqliteDb.dialect`, datetime adapter; `FakeTelegram.send_script/sent`; migration lists in two existing tests.
