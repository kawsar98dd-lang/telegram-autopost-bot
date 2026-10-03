# Step 3 - Real Telegram verification from an Android phone (temporary cloud server)

Use this when you only have an Android phone. The 16-step test table, the PASS/FAIL rules and the security checklist
are the same as in [STEP3_REAL_TELEGRAM_VERIFICATION.md](STEP3_REAL_TELEGRAM_VERIFICATION.md); only where the app runs changes.

> ## NEVER share secrets
> Do not send anybody (not the developer, not an AI assistant, not in a GitHub issue or chat) your Telegram login code
> (OTP), two-step password, API ID, API hash, session, the server's `.env`, any encryption key or the setup token.
> Report only PASS/FAIL and on-screen error texts. Never forward a login code in any Telegram chat.

---

## A. Recommended temporary deployment

One small cloud server (Ubuntu 24.04) created from the phone's browser. Docker runs PostgreSQL, the migration job, web,
worker and **Caddy** (`docker-compose.https.yml`), which gets a free HTTPS certificate for a name built from the server's
IP (`203-0-113-5.sslip.io` for `203.0.113.5`). Destroyed right after the test.

* **The setup text you paste into the provider contains no secret and needs no editing.** The repository is public, so
  no GitHub token is involved. Every secret (`APP_SECRET`, `SESSION_ENCRYPTION_KEY`, database password and the one-time
  **setup token**) is generated on the server from the operating system's random source.
* Your Telegram API ID/hash, phone, login code and 2FA password are typed **only** into the app's web form over HTTPS.
  They never appear in GitHub, the setup text, the Docker image, a URL or the server logs.
* Only TCP 80/443 are reachable from the Internet; PostgreSQL has no published port; the plain-http app port listens on
  `127.0.0.1` only; Caddy is the only entrance.
* Example provider: **Hetzner Cloud** (its documentation states cloud-init user data support, hourly billing, and that
  deleting the server stops billing). Any provider offering Ubuntu 24.04 with cloud-init user data should work; not tested.

**Honest limits:** this is a *test-mode* deployment (`APP_ENV=development`, `LICENSE_ENFORCEMENT=false`: no license server
exists yet). Nothing in this procedure has been run on a real server yet, and Let's Encrypt issuance for an `sslip.io`
name has **not** been tested. The real Telegram connection has never been run; Telegram may react to a login from a
data-centre IP. **Prefer a secondary/non-critical Telegram account.**

---

## B. Setup steps (Android browser only)

1. **Copy the setup text.** In Chrome open the repository file `deploy/cloud-init-https-test.sh`, tap **Raw**, select all,
   copy. Do **not** edit anything (the first line must stay `#!/bin/bash`).
2. **Firewall** (Hetzner Console -> Firewalls): inbound **TCP 80** and **TCP 443** from anywhere; no other rule (no SSH).
   Name it `poster-test`.
3. **Server** (Servers -> Add Server): any location; **Ubuntu 24.04**; at least **2 vCPU / 4 GB RAM**; IPv4 on; attach
   `poster-test`; no volumes/backups; paste the copied text into **Cloud config / User data**; name `poster-test`; Create.
   If the form requires an SSH key or sends a root password by e-mail, keep that password for step 6 (the console login).
4. Copy the server's **IPv4**. Your address is `https://` + IP with dots replaced by dashes + `.sslip.io`
   (e.g. `https://203-0-113-5.sslip.io`).
5. **Wait 5-10 minutes** (Docker install, build, certificate). A certificate warning in the first minutes is normal;
   reload after 1-2 minutes. `https://<host>/health/ready` must show `{"status":"ready"}`.
6. **Get the one-time setup token.** It is generated on the server and is **not** in the setup text. Open the server in the
   provider console and use its web console (**Console** button). When setup finishes the screen shows
   `POSTER TEST SERVER READY ... One-time setup token ...` (shown only on the screen, never written to a log).
   If you cannot see it: log in on that console as `root` (password from the provider's e-mail or its "reset root
   password" action) and type `poster-token`. The token is four groups of five letters/digits separated by dashes (20 characters in total).
7. The token file and the `.env` token line are **deleted automatically** a few seconds after the first administrator is
   created (and the setup page is permanently gone then: it returns 404). If you want to be sure, run `poster-token` again
   later: it says the token no longer exists.

### If something fails (SSH is intentionally closed)
Everything can be diagnosed in the provider's **web console** without opening any port:
log in as `root`, then `poster-diagnose` (non-secret output: setup log, containers, listening ports, health, Caddy
certificate messages) or `tail -n 40 /var/log/poster-bootstrap.log`. The server is disposable: if it cannot be fixed,
destroy it (section E) and create a new one.

---

## C. Android browser steps

Open the `https://...sslip.io` address in Chrome and perform **steps 1-16** of the table in
[STEP3_REAL_TELEGRAM_VERIFICATION.md](STEP3_REAL_TELEGRAM_VERIFICATION.md) (section 2). Differences: step 1 asks for the
setup token from B6; always use exactly this HTTPS address (the app checks the origin on every form).
Do the page-source checks of its security checklist on this address, e.g. `view-source:https://<host>/telegram` then
Chrome menu -> **Find in page** for the first 6 characters of your API hash, the last 4 digits of your phone and `gAAAA`
(expected: no matches; repeat on the verify page).

**Optional server-side checks** (need the console login): `cd "$(cat /etc/poster-project-dir)" && bash deploy/check-secrets.sh`.
Typed values are hidden and kept off every command line; only match counts are printed (all must be `0`).
**Network check:** `poster-diagnose` lists listening sockets: expected `0.0.0.0:80`, `0.0.0.0:443`, `127.0.0.1:8000`, and nothing on 5432.

---

## D. HTTPS and security considerations

* **Encrypted connection** with a publicly trusted certificate; cookies are `Secure`, `HttpOnly`, `SameSite=Lax` with the
  `__Host-` prefix; HTTP is redirected to HTTPS by Caddy; requests for the bare IP are not served (Caddy answers only for the host name).
* **Nothing secret in the setup text.** It is visible in the provider's settings and on the server's metadata service
  (reachable by any process on the machine), so it deliberately contains none.
* **The first-run page is public** until the administrator exists; the server-generated token protects it. Create the
  administrator as soon as the server is up.
* **Files:** `.env` and the token file are mode `0600` (root only); the bootstrap log is `0600` and holds no secrets.
  The setup token is shown only on the console screen.
* **Remaining exposure (inherent):** the app's secrets must exist as environment variables of its containers, so root on the
  server can read them (`docker inspect`). Anyone with provider-console access to your server can reach root. Do not share
  the console session. Deleting the server removes everything, but the controls above do not rely on that.
* **Public source code:** the repository is public, so the product source is public as well. Keep license private keys out of it.
* **sslip.io** is a shared service: certificate issuance can fail or be rate-limited. Then wait and retry or use your own domain
  (set `APP_HOST`/`APP_URL`). Untested.
* **Telegram side:** a login from a data-centre IP may trigger Telegram security notices.

---

## E. Destroy everything after testing

1. In the app: **Disconnect**, then **Remove** the account (steps 14 and 16 of the table).
2. Telegram app -> Settings -> Devices: **Telegram Auto Poster** must be gone (terminate it if not).
3. Hetzner Console -> Servers -> `poster-test` -> **Delete** (powering off does **not** stop billing). This erases the disk
   (database, encrypted sessions, `.env`, certificates).
4. Firewalls -> delete `poster-test`.
5. **Primary IPs:** delete any IPv4/IPv6 left over (billed separately); also check Volumes, Snapshots, Backups: all empty.
6. Optionally delete the console project; check the billing page once.

---

## F. PASS / FAIL evidence to report (no secrets)

| Item | Result |
|---|---|
| Valid HTTPS padlock and `/health/ready` ready (B5) | |
| Setup token was obtainable from the console (B6) | |
| Steps 1-16 of the main table (one line each) | |
| Page-source checks: no match for hash prefix, phone digits, `gAAAA` | |
| Telegram Devices: session present after connect / gone after disconnect | |
| Revoked-session check showed "Session expired" (step 12) | |
| `poster-token` after admin creation says the token no longer exists | |
| Optional: `poster-diagnose` sockets as expected; `check-secrets.sh` all `0` (or "not run") | |
| Cleanup: server, firewall, Primary IPs deleted | |

**PASS** = every row PASS (optional row may be "not run"). **FAIL** = any row fails: report the row and the exact on-screen
text only. Never send screenshots showing your phone number, API ID, a code or the setup token.
