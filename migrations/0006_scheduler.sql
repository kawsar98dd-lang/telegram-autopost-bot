-- Step 6: scheduler + background worker.
-- schedules, schedule_targets, posting_jobs and posting_logs already exist (migration 0001) and are EXTENDED here, never
-- recreated. Written in the portable SQL subset (PostgreSQL + SQLite) like all other migrations. Only additive changes:
-- no existing row is touched and no existing constraint is dropped.

-- A cancelled schedule keeps status 'completed' (the status vocabulary of 0001 is unchanged) and records WHEN it was
-- cancelled, so the pages can show "Cancelled" and "Completed" as different things.
ALTER TABLE schedules ADD COLUMN cancelled_at TIMESTAMPTZ;
CREATE INDEX schedules_user_idx ON schedules (user_id, created_at DESC);
-- The scheduler's own due-schedule query (status = 'active' AND next_run_at <= now) is served by schedules_next_run_idx
-- from 0001.

-- Per-target delivery bookkeeping. One posting_jobs row is ONE (schedule occurrence, group) pair, so progress of a
-- multi-group occurrence is persisted target by target.
--   not_sent  nothing was handed to Telegram (also after a confirmed, definitive failure)
--   sending   the request is being / was handed to Telegram and no answer has been recorded yet
--   sent      Telegram confirmed the message
--   uncertain the request may or may not have reached the group; the worker NEVER resends such a job by itself
ALTER TABLE posting_jobs ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'not_sent'
    CHECK (delivery_state IN ('not_sent', 'sending', 'sent', 'uncertain'));
ALTER TABLE posting_jobs ADD COLUMN post_id UUID REFERENCES posts (id) ON DELETE SET NULL;
ALTER TABLE posting_jobs ADD COLUMN finished_at TIMESTAMPTZ;
CREATE INDEX posting_jobs_schedule_idx ON posting_jobs (schedule_id, scheduled_for DESC);
CREATE INDEX posting_jobs_account_due_idx ON posting_jobs (account_id, next_attempt_at)
    WHERE status IN ('scheduled', 'waiting');

-- One worker at a time talks to one Telegram account: a short-lived lock on the account row (taken with a single atomic
-- UPDATE) serialises sends per account while different accounts proceed in parallel. send_blocked_until remembers a
-- FloodWait so that no job of that account is attempted before Telegram's wait has passed.
ALTER TABLE telegram_accounts ADD COLUMN send_locked_by TEXT;
ALTER TABLE telegram_accounts ADD COLUMN send_lock_expires_at TIMESTAMPTZ;
ALTER TABLE telegram_accounts ADD COLUMN send_blocked_until TIMESTAMPTZ;
