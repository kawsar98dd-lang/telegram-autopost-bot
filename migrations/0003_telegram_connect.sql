-- Step 3: Telegram account connection.
-- Everything secret is stored Fernet-encrypted (see app/telegram/connect.py for the binding contexts).

-- Per-account Telegram API credentials (needed later by the worker); never stored in plain text.
ALTER TABLE telegram_accounts ADD COLUMN api_id_enc TEXT;
ALTER TABLE telegram_accounts ADD COLUMN api_hash_enc TEXT;

-- A login that is in progress (code sent, maybe waiting for the 2FA password).
-- The one-time code and the 2FA password are NEVER stored: they exist only in the request
-- that submits them. Rows are deleted on success, cancel, too many failures, or expiry.
CREATE TABLE telegram_login_attempts (
    id                   UUID PRIMARY KEY,
    user_id              UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    state                TEXT NOT NULL DEFAULT 'code_sent' CHECK (state IN ('code_sent', 'password_needed')),
    api_id_enc           TEXT NOT NULL,
    api_hash_enc         TEXT NOT NULL,
    phone_enc            TEXT NOT NULL,
    phone_masked         TEXT NOT NULL,
    phone_code_hash_enc  TEXT NOT NULL,
    pending_session_enc  TEXT NOT NULL,
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    created_at           BIGINT NOT NULL,
    expires_at           BIGINT NOT NULL
);
CREATE INDEX telegram_login_attempts_user_idx ON telegram_login_attempts (user_id);
CREATE INDEX telegram_login_attempts_expiry_idx ON telegram_login_attempts (expires_at);
