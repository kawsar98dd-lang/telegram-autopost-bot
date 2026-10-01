-- Step 2: dashboard authentication (server-side sessions) and rate limiting.

-- Only the SHA-256 hash of the session token is stored: a database leak does not
-- reveal usable session cookies. Times are unix seconds.
CREATE TABLE auth_sessions (
    id           UUID PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,
    user_id      UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at   BIGINT NOT NULL,
    last_seen_at BIGINT NOT NULL,
    user_agent   TEXT
);
CREATE INDEX auth_sessions_user_idx ON auth_sessions (user_id);

-- Fixed-window counters shared by all processes (keys are hashed identifiers).
CREATE TABLE rate_limits (
    key          TEXT PRIMARY KEY,
    window_start BIGINT NOT NULL,
    count        INTEGER NOT NULL
);
