-- Additive, user-scoped safety notification storage. Existing application data is untouched.
CREATE TABLE safety_notifications (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    rule_key TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    window_bucket BIGINT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'low', 'review', 'high')),
    evidence TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'read', 'resolved')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, dedupe_key, window_bucket)
);
CREATE INDEX safety_notifications_user_created_idx ON safety_notifications (user_id, created_at DESC);
CREATE INDEX safety_notifications_user_status_idx ON safety_notifications (user_id, status, created_at DESC);
