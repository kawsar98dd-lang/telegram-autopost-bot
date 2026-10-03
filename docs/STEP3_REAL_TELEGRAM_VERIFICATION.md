# Step 3 - Real Telegram verification (manual test on your own account)

This is a one-time check that the Telegram connection works with a **real** Telegram account. Automated tests only
use a simulated Telegram, so this manual test is the only proof for the real thing.

> ## NEVER share secrets
> Do **not** send anybody (not the developer, not an AI assistant, not in a GitHub issue or chat): your Telegram
> login code (OTP), your two-step-verification password, your API hash, your API ID, a session string/file, the
> contents of `.env`, or any encryption key. Do **not** commit `.env` to GitHub (it is git-ignored; keep it that way).
> Report results only as PASS / FAIL plus the *error text shown on screen*, never the values you typed.
> Do not forward or paste the login code into any Telegram chat: Telegram cancels codes that are shared.

---

## 1. Deployment status and the simplest path

* The project is deployable with `docker compose` (PostgreSQL + migration job + web + worker); CI builds and starts it.
* **A license server does not exist yet**, and production mode requires `https://` and a license. Therefore this test
  runs in **development mode** (`APP_ENV=development`, `LICENSE_ENFORCEMENT=false`). That mode exists only for testing.
* Development mode uses plain `http://`. Your login code and passwords then travel unencrypted, so use it **only on
  your own trusted home Wi-Fi**, never over the internet, and stop it when you are done (section 6).

**Simplest realistic setup: a PC or laptop (Windows / macOS / Linux) running Docker, and your Android phone on the
same Wi-Fi opening the PC's address in the phone browser.** Android itself cannot run this stack.

### 1.1 One-time installs on the PC
1. Docker Desktop (Windows/macOS) or Docker Engine + Compose plugin (Linux). Start it.
2. Python 3.12 from python.org, then in a terminal: `pip install cryptography`
   (only used to generate your random secrets).
3. Get the code: download the repository ZIP from GitHub (or `git clone`) and open a terminal **in the folder that
   contains `docker-compose.yml`**.

### 1.2 Create your private `.env`
```
python scripts/generate_keys.py --init-env
```
This creates `.env` with fresh random secrets. Open `.env` in a text editor and change **only** these lines:

| Line | Set it to |
|---|---|
| `APP_ENV` | `development` |
| `APP_URL` | `http://<PC-IP>:8000` (find `<PC-IP>`: Windows `ipconfig` -> "IPv4 Address"; macOS `ipconfig getifaddr en0`; Linux `hostname -I`), e.g. `http://192.168.1.50:8000` |
| `SETUP_TOKEN` | any random word you invent (it protects the first-run page) |
| *(add a new line)* `LICENSE_ENFORCEMENT` | `false` |

Leave `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` **empty**: you will type them in the browser, which is what is being tested.
Keep `.env` private. Back it up if you want to keep the connection, because losing `SESSION_ENCRYPTION_KEY` makes
stored sessions unreadable.

### 1.3 Start
```
docker compose up -d --build
docker compose ps
```
Wait until `web` and `worker` show `healthy` (about a minute). If Windows asks about firewall access for Docker,
allow it for **private** networks. Check on the PC: open `http://<PC-IP>:8000/health/ready`: it must show `{"status":"ready"}`.

> **Always use exactly the address you put in `APP_URL`** (including the IP and `:8000`), also on the PC. Another
> address (for example `localhost`) makes the app reject form submissions on purpose (CSRF origin check).

### 1.4 Get your own Telegram API ID / API hash (once)
On your phone or PC open https://my.telegram.org/apps, log in with your phone number, create an application
(any name), and keep the **api_id** (a number) and **api_hash** (32 characters) private.

---

## 2. Test procedure (Android browser) with PASS / FAIL

Use Chrome on the phone, connected to the same Wi-Fi. "App shows" is what you should see on the screen.

| # | You do (Android browser) | App should show | PASS if | FAIL if |
|---|---|---|---|---|
| 1 | Open `http://<PC-IP>:8000` | "Welcome / Create administrator" page with a **Setup token** field | Page loads | Page does not load, or shows an error page |
| 2 | Enter the setup token, an email, a password (12+ chars), repeat it; tap **Create administrator** | You land on the dashboard ("Dashboard", menu with *Telegram accounts*) | Dashboard appears | Error, loop back to setup, or 403 |
| 3 | Open the menu -> **Log out**, then sign in again with the same email/password | Login page, then dashboard again | Logout and login both work | Login impossible / wrong error |
| 4 | Menu -> **Telegram accounts** -> **Add Telegram Account** | Form with phone, API ID, API hash (hash field shows dots) | Form appears | Page missing / error |
| 5 | Type your phone number with country code (`+8801...`), your API ID and API hash; tap **Send login code** | Page "Verify Telegram login" showing your phone **masked** (like `+88•••••••••78`) | Verify page appears, number masked | An error message (note its text), or the full number is shown |
| 6 | Look at the Telegram app (other device) for the login code | A message from "Telegram" with a 5-digit code | Code arrives | No code within ~2 minutes (also try again once; Telegram may send an SMS instead) |
| 7 | Type the code into **Login code**; tap **Continue** | Either the account list (no 2FA) **or** a page asking for the two-step password | One of those two | "code is not correct" although typed correctly, or an unexpected error |
| 8 | *Only if asked:* type your Telegram cloud (2FA) password; tap **Connect** | Account list | Account list appears | "password is not correct" with the right password, or error |
| 9 | Check the account list | Green notice "Telegram account connected."; row with your name, `@username`, masked phone, your Telegram ID, status **Connected** | Row correct, status Connected | Row missing, or full phone/other secrets visible |
| 10 | In the Telegram app: Settings -> Devices | A session named **Telegram Auto Poster** (the name may include the app version) | Present | Absent |
| 11 | Tap **Check** on the row | "The session is valid." | Message shown | "no longer accepts" right after connecting |
| 12 | In the Telegram app: Settings -> Devices -> terminate **Telegram Auto Poster**. Back in the dashboard tap **Check** | "Telegram no longer accepts this session..." and status **Session expired** | Both shown | Still "Connected" |
| 13 | Tap **Add Telegram Account** again, repeat steps 5-8 for the same account | Same account row becomes **Connected** again (no duplicate row) | One row, Connected | Two rows for the same account, or error |
| 14 | Tap **Disconnect** | "Telegram account disconnected..."; status **Disconnected**; the session disappears from Telegram -> Devices | All three | Session still in Devices |
| 15 | Tap **Add Telegram Account**, enter a **wrong** code on purpose | "That code is not correct..." and "4 attempt(s) left" | Message shown, can retry | Crash/blank page |
| 16 | Tap **Remove** on the disconnected row | Row disappears | Row gone | Row stays |

Steps 1-16 passing = the Telegram connection works with a real account.

---

## 3. What counts as PASS or FAIL overall

* **PASS:** all of steps 1-16 PASS **and** every item in sections 4 and 5 below passes.
* **FAIL:** any single step or check fails. Note the step number and the exact on-screen error text (nothing else).
  Do not retry endlessly: Telegram limits login attempts (the app shows a wait time if Telegram asks you to wait).

---

## 4. Checks on the data and logs (on the PC, in the terminal)

Run these after step 9 (connected) and again after step 14 (disconnected).

**a) Stored data is encrypted / removed** (nothing here prints a secret)
```
docker compose exec db psql -U poster -d poster -c "select status, api_id_enc is null as no_id, api_hash_enc is null as no_hash from telegram_accounts;"
docker compose exec db psql -U poster -d poster -c "select count(*) as sessions, min(left(session_enc,5)) as starts_with from telegram_sessions;"
docker compose exec db psql -U poster -d poster -c "select count(*) as pending_logins from telegram_login_attempts;"
```
| After step | Expected |
|---|---|
| 9 (connected) | `connected`, `no_id`=f, `no_hash`=f (stored encrypted); sessions = 1 with `starts_with` = `gAAAA` (encrypted token, not readable text); `pending_logins` = 0 |
| 14 (disconnected) | `disconnected`, `no_id`=t, `no_hash`=t; sessions = 0; `pending_logins` = 0 |

**b) None of your secrets is anywhere in the database** (type the values only into your own terminal; `read -rs` hides them).
On macOS/Linux/WSL/Git Bash:
```
read -rs SECRET && docker compose exec -T db pg_dump -U poster poster | grep -cF "$SECRET"; unset SECRET
```
Run it once each for: your API hash, your login code (while still fresh in memory), your 2FA password, your full phone
number digits. Expected output for every one: `0`.
(Your API **hash** is stored only encrypted, so it must not appear as plain text either.)

**c) Nothing sensitive in the logs**
```
docker compose logs web worker > logs.txt
read -rs SECRET && grep -cF "$SECRET" logs.txt; unset SECRET     # repeat for hash, code, 2FA password, phone
rm logs.txt
```
Expected: `0` each time. You should only see lines like `telegram account connected (account_id=...)`,
`telegram operation failed (invalid_code)`: reasons, never values.

---

## 5. Security checklist for this real test

Tick each one:

- [ ] On the form, the **API hash** and the **2FA password** fields show dots, not text.
- [ ] After a failed submit (e.g. wrong API hash) the form comes back **without** the API ID/hash filled in.
- [ ] Page source contains none of your secrets. In Chrome on Android type `view-source:http://<PC-IP>:8000/telegram`
      in the address bar, then menu -> **Find in page**, and search for: the first 6 characters of your API hash,
      the last 4 digits of your phone, and `gAAAA`. Expected: **no matches** for all three. Repeat for the verify page
      (`/telegram/verify/...`) and after the account is connected.
- [ ] Only the masked phone, Telegram ID, name and username are shown for the account.
- [ ] Section 4b and 4c report `0` for every secret.
- [ ] `docker compose logs` shows no code, password, hash, session or phone number.
- [ ] After **Disconnect**: sessions table empty, API credential columns empty, session gone from Telegram -> Devices.
- [ ] `.env` is not in `git status` (run `git status`; `.env` must not be listed), and you never uploaded it anywhere.
- [ ] You never sent any code, password, hash, session or `.env` content to anyone.

---

## 6. Clean up when finished

```
docker compose down -v        # stops everything and DELETES the database (all test data, encrypted sessions)
```
Then delete `.env`. In the Telegram app confirm that **Telegram Auto Poster** is not listed under Devices.

## 7. What to report back (safe)

For each step number 1-16 and each box in section 5: PASS or FAIL, plus the visible error text for failures.
**No** codes, passwords, hashes, phone numbers, sessions, keys or `.env` content. Screenshots must hide your phone
number, API ID and any code.

## 8. Known limitations of this test setup

* It runs in development mode over `http://` on a trusted network. Real customer deployments will use `https://`
  and a license (the license server is a later step).
* Telegram itself may delay or refuse login codes for new servers/IPs, or ask you to wait (FloodWait). The app shows the
  wait time and never retries automatically.
* This test covers account connection only. Groups, posting and scheduling are not implemented yet.
