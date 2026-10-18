# Safety Monitoring Upgrade — Change Report

## Baseline and artifact integrity

- Original project ZIP: `telegram-auto-poster-premium-ui-0.1.3-logs-schedule-edit.zip`
- Original ZIP checksum was verified before changes; the original archive remains separately available.
- The original ZIP was not overwritten. Work was performed in a separate extracted working copy.
- File-by-file comparison: 8 files added, 5 files modified, 0 files deleted.
- Licensing code/files/docs: byte-for-byte unchanged in the comparison.

## Added files

1. `app/safety/__init__.py` — local safety package.
2. `app/safety/engine.py` — deterministic content and activity rules, notification persistence, language helpers and localized notification text.
3. `app/web/safety_routes.py` — authenticated Safety page, question handling, user-scoped notification acknowledgement.
4. `app/web/templates/safety.html` — responsive Safety Monitoring page.
5. `migrations/0007_safety_notifications.sql` — additive notification table and indexes.
6. `tests/test_safety_engine.py` — deterministic rule/language/refusal unit tests.
7. `docs/SAFETY_MONITORING_BN.md` — Bengali setup, notification, privacy and deployment guide.
8. `docs/SAFETY_MONITORING_CHANGE_REPORT.md` — this change report.

## Modified files and reasons

1. `app/web/routes.py` — register the new authenticated safety routes. Existing routes are retained.
2. `app/web/views.py` — add one navigation entry for Safety Monitoring.
3. `app/web/static/style.css` — add scoped responsive styles for the new page only.
4. `app/workers/main.py` — run best-effort safety monitoring at most every five minutes in the existing worker. Monitoring errors do not abort the posting cycle.
5. `tests/test_schema.py` — expect the new additive migration and validate its notification enum/check constraints.

No files were deleted. `requirements.txt`, Docker/hosting files, posting/scheduling business logic, authentication modules, and licensing components were not modified.

## Implemented monitoring rules

- Exact normalized duplicate text across recent jobs for the same destination within 24 hours.
- Near-duplicate heuristic using token-set similarity of at least 90%, only for content with at least eight tokens. This is not semantic understanding.
- Repeated URL signal across job snapshots for the same destination within 24 hours; the URL is not copied into the notification.
- Posting-frequency review when successful jobs in one destination/hour meet `SAFETY_POSTS_PER_DESTINATION_PER_HOUR` (default 5, clamped to 2–100). This is a local heuristic, not a Telegram limit.
- Repeated failure signal for at least three failed jobs in six hours for one destination.
- FloodWait-related error signal only when the stored job error code/message contains FloodWait wording.
- Potential schedule overlap when two active schedules target the same group and their stored `next_run_at` values are within 15 minutes.

Notifications store a rule, deduplication key, time bucket, severity, concise evidence and recommendation. Raw post text and URLs are not stored in notification rows. Identical notifications are deduplicated by user/rule-entity/time bucket. Read acknowledgement does not mark a finding resolved.

## Security and privacy controls

- All page/notification reads and acknowledgements are scoped to the authenticated user's ID.
- Route authorization uses the existing `dashboard.view` permission and existing CSRF pipeline.
- Assistant questions are limited to 500 characters and use the existing rate limiter (10 per 15-minute window).
- No external AI provider, API key, arbitrary SQL, shell execution, filesystem tool, or mutation tool was added.
- The assistant does not create/edit/delete posts or schedules and does not alter licensing.
- User questions use deterministic local responses. The system is not represented as generative AI.

## Tests actually run

- `python -m compileall -q app tests/test_safety_engine.py` — passed.
- Jinja template parse for `safety.html` — passed.
- Route registry inspection — the three expected `/safety` routes registered.
- Targeted suite: `tests/test_safety_engine.py`, `tests/test_schema.py`, `tests/test_security.py`, `tests/test_secret_exposure.py`, `tests/test_release_hardening.py`, `tests/test_customer_release.py`, `tests/test_postgres_scheduler.py`, `tests/test_fastapi_smoke.py`, `tests/test_web_csrf.py`, `tests/test_web_license.py`, `tests/test_license.py`, `tests/test_web_schedules.py`, `tests/test_worker_cycle.py` — **158 passed, 6 skipped, 6 subtests passed**.
- Full `pytest -q` did not complete within a 240-second timeout; the run stopped after partial progress without a final summary. A separate broad test batch also timed out. These runs are not counted as passes.

## Not tested / remaining verification

- No live PostgreSQL server was attached to this environment; real PostgreSQL migration execution and `gen_random_uuid()` insert behavior require staging verification.
- No real Telegram account, FloodWait event, production worker topology, or hosting dashboard was tested.
- No manual Android viewport test was performed.
- Cross-user route behavior is supported by user-scoped SQL and existing auth/CSRF middleware, but a dedicated end-to-end multi-user notification test against PostgreSQL was not executed.
- Full test suite completion remains outstanding.

## Compatibility and deployment

- New migration: `0007_safety_notifications.sql`. It is additive and preserves existing records. Normal migration execution depends on the project's existing `AUTO_MIGRATE`/release process.
- The existing worker must be running for automatic monitoring. Web requests do not trigger a scan. If a deployment runs only the web process, notification generation will not occur until the existing worker is deployed/started.
- Monitoring is best-effort and runs at most every five minutes per worker process. Database-level deduplication prevents identical notification rows, although multiple worker processes can still perform duplicate read scans.
- No new dependencies or paid infrastructure are required.
- Do not deploy directly to production before staging verification. Preserve the original ZIP as rollback baseline.

## Limitations

- Thresholds are heuristics, not official Telegram rate limits or a Telegram risk score.
- Content similarity is token-based and may have false positives/negatives.
- Schedule overlap examines stored next-run times, not every future recurrence.
- The assistant cannot inspect Telegram's internal moderation state, hosting dashboards, private server metrics, or data not recorded by the application.
- Customers who control their own hosting/source distribution can inspect server-side source; this implementation does not promise absolute source secrecy.
