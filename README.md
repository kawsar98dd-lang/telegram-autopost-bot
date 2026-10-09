# Telegram Auto Poster

Schedules marketing posts to Telegram groups **through your own Telegram user account (MTProto)**.
It does not use the Telegram Bot API and never needs a bot in your groups.

> **Status: Step 6 of the build - scheduler and background worker (Step 5: drafts, Steps 3-4: Telegram connection and groups).** Telegram login, group discovery, the post
> composer and the actual posting engine are added in the next steps. This README grows with them.

## What exists in this step

| Area | Where | Notes |
|---|---|---|
| Configuration | `app/config.py`, `.env.example` | Validates everything at start-up and lists all problems at once |
| Encryption | `app/security/crypto.py` | Fernet, key rotation, ciphertext bound to a context (per-user isolation) |
| Passwords | `app/security/passwords.py` | scrypt |
| Log redaction | `app/security/redact.py` | Safety net that masks secrets in logs |
| Database schema | `migrations/0001_initial.sql` | PostgreSQL; composite foreign keys enforce cross-user isolation |
| Migrations | `app/db/migrate.py` | Forward-only, checksummed, advisory-locked |
| License client | `app/licensing/` | Signed activation, offline grace, revocation, tamper checks |
| License server parts | `license_server/` | **Seller only - never ship to customers** (signing, keygen, schema, protocol) |
| Web / worker | `app/web`, `app/workers` | Health endpoints, license gate; job processing comes later |
| Docker | `Dockerfile`, `docker-compose.yml` | web + worker + PostgreSQL |

## First run (Step 2)

1. Start the app and open `APP_URL`. With no administrator yet, every page leads to **Create administrator**
   (set `SETUP_TOKEN` in `.env` first so nobody else can claim a fresh installation). The page disappears
   for good once the account exists.
2. You are signed in and taken to **License**: enter your license key. The dashboard stays locked until it is active.
3. Dashboard sections for Telegram accounts, groups, schedules, jobs and logs are placeholders until later steps.

**Sign-in security:** scrypt password hashing, server-side sessions (only a hash of the cookie token is stored),
idle and absolute expiry, new session on every login, `Secure`/`HttpOnly`/`SameSite` cookies, CSRF tokens on every
state-changing request (forms and HTMX), Origin checks, database-backed login rate limiting, open-redirect protection,
strict Content-Security-Policy, and error pages that never show internals.

**HTMX** is served from `app/web/static/vendor/htmx.min.js`, never from a CDN. Fetch it once with
`python scripts/vendor_htmx.py` (the Docker build does this automatically if missing). Pages work without it.

## Connecting your Telegram account (Step 3)

Dashboard -> **Telegram accounts** -> **Add Telegram Account**:
1. Enter your phone number (international format) and your own API ID / API hash from https://my.telegram.org/apps
   (or leave both empty if `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` are set in `.env`).
2. Telegram sends a login code to your Telegram app. Enter it. If the account has two-step verification you are asked for
   the cloud password.
3. The resulting session is stored **Fernet-encrypted** and bound to your user and that account record.

Safety rules that are built in: the login code and the 2FA password are never stored or logged; the API hash and session
never reach the browser; a stored session cannot be decrypted under another user or account; **Disconnect** logs the
session out on Telegram and deletes the local copy and API credentials; **Check** detects sessions ended from the Telegram
app. Wrong codes are limited to 5 per login, code requests to 5 per hour per user. FloodWait answers are shown, never retried.

This release only connects accounts. It does not post, list groups or schedule anything yet.

## Deploy for the Step 3 real-account test

There is no license server yet, so the real-account test runs in development mode on a trusted home network
(PC with Docker + Android browser on the same Wi-Fi). Exact steps, PASS/FAIL table and security checklist:
**[docs/STEP3_REAL_TELEGRAM_VERIFICATION.md](docs/STEP3_REAL_TELEGRAM_VERIFICATION.md)**.
**Render (Blueprint, Free tier, web + Postgres only):**
**[docs/STEP3_REAL_TELEGRAM_VERIFICATION_RENDER.md](docs/STEP3_REAL_TELEGRAM_VERIFICATION_RENDER.md)** (`render.yaml`).

**Android only?** Use the temporary HTTPS cloud-server variant:
**[docs/STEP3_REAL_TELEGRAM_VERIFICATION_ANDROID_CLOUD.md](docs/STEP3_REAL_TELEGRAM_VERIFICATION_ANDROID_CLOUD.md)**
(`deploy/cloud-init-https-test.sh`, `docker-compose.https.yml`, automatic HTTPS, destroyed afterwards).
Production deployments need `https://` and a license, and are documented when the license server exists.

## Groups (Step 4)

After connecting a Telegram account, **Groups** lists the groups that account belongs to, shows where Telegram allows it to post
and lets you select posting targets. See **[docs/STEP4_GROUPS.md](docs/STEP4_GROUPS.md)** (how permissions are determined, refresh, limits,
and the manual verification steps). Posting and scheduling come in later steps.

## Posts (Step 5)

Drafts with text, one image and target groups, previewed with the mandatory footer; nothing is sent yet. See [docs/STEP5_POSTS.md](docs/STEP5_POSTS.md).

## Scheduler and worker (Step 6)

Schedules (one-time, daily, weekly, custom) and a separate worker that sends posts through your own Telegram account. See **[docs/STEP6_SCHEDULER.md](docs/STEP6_SCHEDULER.md)** (architecture, job states, retry and FloodWait policy, duplicate prevention and its limits, deployment, verification). The web service alone never sends; the worker service must run.

## Run the tests

```bash
python -m unittest discover -s tests -v
```
`tests/test_postgres_integration.py` needs a real database and is skipped unless `TEST_DATABASE_URL`
is set. The included GitHub Actions workflow (`.github/workflows/ci.yml`) sets it automatically.

## Configure

```bash
python scripts/generate_keys.py --init-env   # creates .env with fresh secrets
```
Then edit `.env` (`APP_URL`, Telegram API ID/hash). **Back up `SESSION_ENCRYPTION_KEY`**: without it,
stored Telegram sessions cannot be decrypted. It may be a Fernet key (what `generate_keys.py` prints) or any random secret of at least
32 characters, for example one generated by a hosting platform; the latter is converted with HKDF-SHA256, deterministically.

## Licensing (how it works, honestly)

The seller runs a license server. On first start the customer enters a license key; the installation
sends the key, its random `installation_id` and its address (`APP_URL`) to the license server, which
answers with a payload **signed with the seller's private Ed25519 key**. The installation verifies the
signature with the embedded public key, stores the result (license key encrypted at rest) and
re-verifies periodically. If the server is unreachable there is an offline grace period (set by the
server, default 7 days). Revocation, disabling, activation limits, reset and transfer are decided
server-side. Unsigned answers can never switch an existing customer off.

**Limitation:** whoever holds the source code can edit it, including the license check. This system
stops casual sharing and re-selling and makes a bypass a deliberate act; it cannot make source code
tamper-proof. It is not, and is not claimed to be, unbreakable protection.

## Footer limitation

The footer is appended and enforced by the application before sending, but Telegram does not provide
a mechanism that makes part of a personal user's message permanently immutable against that user's own
edit/delete permissions.

## Database notes

* Works with any PostgreSQL 13+. Behind a pooler (Supabase/PgBouncer) prefer a direct connection URL
  for migrations; the app already disables prepared-statement caching for pooler compatibility.
* A `?pgbouncer=true` suffix on `DATABASE_URL` is accepted and removed automatically.
