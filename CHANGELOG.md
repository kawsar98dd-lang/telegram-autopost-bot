# Changelog

## 0.1.3 — audit logs and safe schedule editing
* Implemented the authenticated, user-scoped posting Logs page with date, level, event and job-status filters, pagination, safe structured details, and related job/schedule navigation.
* Added in-place editing for paused schedules. The schedule ID and existing history are retained; post content is not editable in this workflow.
* Saving an edit cancels pending/queued jobs tied to the old settings and records the action. Resume computes the next valid occurrence, skipping occurrences missed during the pause.
* Added schedule-created/paused/resumed/edited audit events and a final send gate that checks pause/cancel state while serialized with schedule state changes.
* Added user-facing warnings about requests already in flight; those requests cannot be recalled reliably.


## 0.1.1 — working-copy alignment
* Set the application version to `0.1.1` so the app, customer ZIP filename, and generated release manifest agree.
* Synchronized the seller working copy's embedded Ed25519 public key with the current customer release key. The seller tree remains `RELEASE_BUILD = False`; the release builder sets it to `True` only in the customer package.
* Updated the seller guide's example release filename and the manifest-version regression test.

## Commercial release preparation
* Footer username is now `@AutoPosterHub` (official channel); footer tests added.
* `RELEASE_BUILD` flag: the customer package cannot disable license enforcement by environment variable and always requires a `SETUP_TOKEN`.
* Trial licenses must expire; `issue_license.py --register-file` keeps a non-sensitive sales record.
* `start_single_host.py`: safe optional launcher (replaces the ad-hoc wrapper: no forced development mode, migration failure stops the start, supervised worker, configurable port).
* Customer ZIP now includes `RELEASE_MANIFEST.json`; every `docs/SELLER_*` file stays seller-only.

## Production setup lock
* Production never offers the first-run page without a `SETUP_TOKEN` of at least 16 characters (locked page, HTTP 403, nothing written); short/placeholder tokens are refused by configuration. `generate_keys.py` creates one.
* Worker loop body extracted (`work_cycle`) so license enforcement is testable; the first stale-job recovery no longer waits after a fresh boot.
* Real-FastAPI smoke tests updated for the token requirement (`test_first_run_setup_flow_through_real_fastapi`) and a locked-setup smoke test added; CI now requires 8 smoke tests.

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
