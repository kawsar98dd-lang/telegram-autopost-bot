# Installation guide (for customers, beginner friendly)

Telegram Auto Poster posts your saved messages to Telegram groups **through your own Telegram user account**. You run it yourself. It
consists of a **web service** (the pages you click) and a **worker** (the program that really sends the posts at the scheduled time),
both using one **PostgreSQL** database. The web service alone never sends anything: **the worker must be running**.

## 1. What you need

* A computer or server that can run Docker (Docker Desktop on Windows/Mac, or Docker Engine + Compose on Linux). About 1 GB of free memory.
* A public address with HTTPS if you want to use it from your phone away from home (for example `https://poster.example.com`).
  In production the application insists on `https://`.
* Your Telegram API ID and API hash from https://my.telegram.org/apps (free, you create them with your own account; never share them).
* Your license file from the seller (see `docs/LICENSING.md`).
* The packaged ZIP and its `.sha256` file. Check the download: `sha256sum telegram-auto-poster-customer-*.zip` must print the same value.

## 2. Install (Docker Compose)

1. Unzip the package and open a terminal in the folder `telegram-auto-poster`.
2. Create your settings file with fresh random secrets: `python scripts/generate_keys.py --init-env`
   (needs Python 3 and `pip install cryptography`). This creates `.env` and fills in `APP_SECRET`, `SESSION_ENCRYPTION_KEY`, `SETUP_TOKEN`
   and `POSTGRES_PASSWORD`. **Back up `.env` right now** (see section 7): without `SESSION_ENCRYPTION_KEY` the stored Telegram sessions are unreadable.
3. Open `.env` in a text editor and set:
   * `APP_URL=https://poster.example.com` (the address you will open in the browser; `http://` is only accepted in development mode)
   * `APP_ENV=production`
   * optional: `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` (otherwise you type them when you connect an account)
   * your license: `LICENSE_FILE_CONTENT=<one line from the seller>` (or install the file later, see `docs/LICENSING.md`)
4. Start everything: `docker compose up -d --build`. This starts PostgreSQL, applies the database migrations once (the `migrate` service), then
   starts the `web` and the `worker`. Check: `docker compose ps` (all "running"/"healthy") and open `http://<server>:8000/health/ready`
   (it must answer `{"status":"ready"}`).
5. HTTPS: put any HTTPS reverse proxy in front, or use the included optional Caddy overlay (automatic certificate):
   set `APP_HOST=poster.example.com`, `WEB_BIND_ADDR=127.0.0.1`, `TRUST_PROXY_HEADERS=true` in `.env` and run
   `docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build`. Only turn `TRUST_PROXY_HEADERS` on when a proxy really sits in front.

## 3. First use

1. Open your `APP_URL`. A fresh installation shows **Create administrator**. In production this page is **locked until `SETUP_TOKEN` is set**
   (at least 16 random characters; `generate_keys.py` created one). Enter that token, your email and a strong password (12+ characters).
2. If the license is not installed yet you are taken to the License page: install it (`docs/LICENSING.md`).
3. **Telegram accounts -> Add account:** enter your phone number and your API ID/hash; Telegram sends a code to your Telegram app (and asks for
   your cloud password if you set one). The code and password are never stored.
4. **Groups -> Refresh:** the application lists your groups and checks where your account may post. Select the groups you want.
5. **Posts:** write a draft, add an optional JPG/PNG image, choose target groups, check the preview (it includes the automatic footer).
6. **Schedule this post:** choose one-time/daily/weekly/custom, the timezone (default `Asia/Dhaka`), review, save. **Posting jobs** shows the result.
7. After the administrator exists you may delete `SETUP_TOKEN` from `.env`; the setup page is gone for good.

## 4. Settings you may change (`.env`)

| Setting | Meaning |
|---|---|
| `APP_ENV` | `production` (default; enforces HTTPS, license, setup token) or `development` (local trials only) |
| `APP_URL` | public address with `https://`; cookies and license address binding use it |
| `DATABASE_URL` | set by `docker-compose.yml` for the bundled database; set it yourself only for an external PostgreSQL (`postgresql://user:pass@host/db`) |
| `AUTO_MIGRATE` | `true` runs migrations at start (default when you run without Compose); Compose sets `false` because it has a dedicated `migrate` service |
| `MEDIA_STORAGE` | `database` (default, images live in PostgreSQL and are backed up with it) or `local` (needs a volume shared by web and worker) |
| `WORKER_POLL_SECONDS` | how often the worker looks for due work (default 5) |
| `POST_MIN_INTERVAL_SECONDS` | pause between two sends of one account (default 5) |
| `MAX_POSTS_PER_HOUR` | cap per Telegram account per hour (default 60); more jobs simply wait |
| `LOG_LEVEL` | `INFO` (default) or `DEBUG` (more noise, still redacted) |

Do not run `web` and `worker` with different `SESSION_ENCRYPTION_KEY`, `APP_SECRET` or database; Compose guarantees they match.

## 5. Update to a new version

1. Back up (section 7). 2. Unzip the new package over a new folder, copy your `.env` into it. 3. `docker compose up -d --build`.
Migrations only move forward and are checksummed; never edit files in `migrations/`.

## 6. Database setup without Docker (advanced)

Create an empty PostgreSQL 14+ database and a user, set `DATABASE_URL`, install `requirements.txt` (Python 3.12), then run
`python -m app.db.migrate`, start the web service `uvicorn --factory app.web.main:create_app --host 0.0.0.0 --port 8000`, and the worker
`python -m app.workers.main` (two separate processes; the worker needs the same `.env`). Several workers may run at once.

## 7. Backups

* **`.env`** (contains `SESSION_ENCRYPTION_KEY`, `APP_SECRET`): keep an offline copy in a safe place. Without the encryption key a database
  backup cannot be used to restore Telegram sessions (you would reconnect your accounts).
* **Database:** `docker compose exec -T db pg_dump -U poster poster > backup-$(date +%F).sql` (daily is sensible). It contains your posts, schedules,
  history and the *encrypted* sessions; protect the file like a password.
* **Restore:** start a clean installation, then `docker compose exec -T db psql -U poster poster < backup.sql`, then use the same `.env`.
* **Images** are inside the database by default. With `MEDIA_STORAGE=local` also back up the Docker volume `app_data`.
* Your **license file** and the original package ZIP.

## 8. Troubleshooting

Look at the logs first: `docker compose logs --tail=100 web`, `... worker`, `... migrate`.

| Symptom | Likely cause and fix |
|---|---|
| Containers stop right away, log says "Configuration problems" | read the list; every line names the `.env` setting to fix (for example `APP_URL must start with https://`) |
| "This build contains no valid license public key" | your package is incomplete or modified; get a fresh package from the seller |
| Page says **Setup is locked** | set `SETUP_TOKEN` (16+ characters, `python scripts/generate_keys.py` prints one) in `.env`, `docker compose up -d` |
| Everything redirects to the License page | no/invalid/expired license; read the message on that page (`docs/LICENSING.md`) |
| Posts stay "Queued" | the worker is not running (`docker compose ps`), the license is invalid (worker log: `license state:`), or the time has not come yet (check the timezone) |
| Job "Failed: does not allow this account to post" | your account lost the right to write in that group; fix it in Telegram, refresh Groups |
| Job "Failed: ... Reconnect the account" | Telegram ended the session (or you removed it); **Telegram accounts -> Reconnect**, then **Retry** the job |
| Job "Waiting to retry" | Telegram asked to slow down (FloodWait) or the network was down; it retries by itself, nothing to do |
| Job "Uncertain - check the group" | see section 10 |
| "migration ... checksum" error | someone edited a file in `migrations/`; restore the original file |
| Login keeps failing | too many wrong attempts are rate-limited for a while; wait 15 minutes |

## 9. Telegram permissions and rate limits

* Only groups where your account **may write** can be selected. The application checks this when you refresh groups **and again right before
  every send**; if the right is gone the job fails instead of trying.
* It never joins or leaves groups and never changes rights. It never uses the Bot API.
* Telegram limits how fast an account may send. The worker waits `POST_MIN_INTERVAL_SECONDS` between messages, limits each account to
  `MAX_POSTS_PER_HOUR`, and obeys every FloodWait Telegram announces. These settings **reduce** the risk; they cannot remove it.
* **Risk you carry:** automated posting from a personal account can be treated as spam by Telegram and may lead to limits or a ban of that
  account. Post only where you are allowed to, keep volumes low, and consider using a dedicated account. The seller cannot influence Telegram's decisions.
* Every post automatically ends with a footer line crediting the application; it is part of the product and cannot be switched off in settings.

## 10. Delivery and duplicate-post risks (please read)

Telegram offers no way to send "exactly once". The application is built to **prefer a missing post to a duplicate**:

* It records "about to send" before the request and the result right after. If the connection breaks or the process dies **after** the request
  left, nobody can know whether Telegram delivered the message. The job is then shown as **Uncertain**. It is never resent automatically. Look in
  the group: press **It was delivered**, or **Send again** if the post is missing (if it was there after all, you now have a duplicate).
* Posts that could not start more than 6 hours after their time (for example the worker was off) are marked **missed** and not sent late.
* Definite refusals (no permission, invalid image, ended session) are never retried blindly; temporary network errors are retried up to 5 times.
* Details: `docs/STEP6_SCHEDULER.md`.

## 11. Known limitations

* Real Telegram behaviour (rate-limit values, error names, login-dependent details) cannot be fully simulated; test with one post to one test group
  first (`docs/STEP6_SCHEDULER.md`, "Manual Telegram acceptance test").
* One schedule per draft; a schedule's target groups cannot be edited afterwards (cancel and create a new one).
* The Logs page is a placeholder; history is shown under **Posting jobs** and in each schedule.
* Offline licensing cannot count installations or recall a license (`docs/LICENSING.md`).
* Hosting that sleeps when idle, or has no background workers, is not suitable for the worker.
