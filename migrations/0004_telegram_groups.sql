-- Step 4: Telegram groups discovery and group selection.
-- The telegram_groups table (migration 0001) already isolates rows per user and per Telegram account through the
-- composite foreign key (account_id, user_id) -> telegram_accounts(id, user_id) and UNIQUE (account_id, tg_chat_id).
-- This migration only adds the bookkeeping that a re-synchronisation needs.

-- FALSE once the connected account no longer sees the group (left, removed, banned, deleted). Rows are kept,
-- not deleted, so history and later schedule targets stay consistent; such rows can never be selected.
ALTER TABLE telegram_groups ADD COLUMN is_present BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE telegram_groups ADD COLUMN last_synced_at TIMESTAMPTZ;

-- When the account's group list was last loaded from Telegram (NULL = never), and, after a FloodWait answer, the
-- unix time before which no new request may be sent for this account.
ALTER TABLE telegram_accounts ADD COLUMN groups_synced_at TIMESTAMPTZ;
ALTER TABLE telegram_accounts ADD COLUMN groups_blocked_until BIGINT;

-- Fast lookup of the selected targets of a user (used by the scheduler in later steps).
CREATE INDEX telegram_groups_selected_idx ON telegram_groups (user_id, account_id) WHERE is_enabled;
CREATE INDEX telegram_groups_account_idx ON telegram_groups (account_id, user_id, title);
