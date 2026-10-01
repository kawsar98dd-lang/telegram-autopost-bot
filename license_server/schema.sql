-- License server database (SELLER ONLY: separate database, never shipped to customers).
-- Portable subset of SQL (PostgreSQL / SQLite).

CREATE TABLE customers (
    id         UUID PRIMARY KEY,
    name       TEXT NOT NULL,
    email      TEXT,
    notes      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The plaintext license key is shown to the seller once at creation; only its
-- SHA-256 hash and a short hint are stored.
CREATE TABLE licenses (
    id                UUID PRIMARY KEY,
    key_hash          TEXT NOT NULL UNIQUE,
    key_hint          TEXT NOT NULL,
    customer_id       UUID NOT NULL REFERENCES customers (id),
    product           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'revoked')),
    max_activations   INTEGER NOT NULL DEFAULT 1 CHECK (max_activations >= 1),
    expires_at        TIMESTAMPTZ,
    verify_interval_seconds INTEGER NOT NULL DEFAULT 86400,
    offline_grace_seconds   INTEGER NOT NULL DEFAULT 604800,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    activated_at      TIMESTAMPTZ,
    revoked_at        TIMESTAMPTZ,
    notes             TEXT
);

-- activation_count = number of rows here with released_at IS NULL.
CREATE TABLE activations (
    id              UUID PRIMARY KEY,
    license_id      UUID NOT NULL REFERENCES licenses (id) ON DELETE CASCADE,
    installation_id TEXT NOT NULL,
    host            TEXT NOT NULL DEFAULT '',
    app_version     TEXT,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_ip         TEXT,
    released_at     TIMESTAMPTZ,
    UNIQUE (license_id, installation_id)
);

CREATE TABLE audit_log (
    id         UUID PRIMARY KEY,
    license_id UUID REFERENCES licenses (id) ON DELETE SET NULL,
    event      TEXT NOT NULL,
    detail     TEXT,
    ip         TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX audit_log_license_idx ON audit_log (license_id, created_at DESC);
