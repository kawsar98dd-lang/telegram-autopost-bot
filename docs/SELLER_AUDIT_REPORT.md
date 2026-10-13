# Audit report: deploy.zip + main.py (SELLER ONLY)

Inputs (unchanged, SHA-256): `deploy.zip` 9966db29...cadd4, `main.py` c9776f21...848e9. Evidence levels: **[SRC]** read in the source, **[TEST]** shown by an
automated test that was actually run, **[LIVE]** seen on a real deployment (none in this audit), **[NOT VERIFIED]**.

## 1. Executive summary
`deploy.zip` is the previously delivered project (identical to my last delivered tree except `app/licensing/constants.py`, which now holds your public key).
The code is a sound base, but the package **as it stands is not safe to sell**: it still contains the placeholder footer, seller-only material,
no `.gitignore`/`.github` (the ZIP lacks them), and the licensing check can be disabled with environment variables. `main.py` (your own wrapper, not part of
the ZIP) has four real defects. Nothing in this audit touched Telegram, Render, Wispbyte or any private key.

## 2. Archive inventory
183 files, no top-level folder. Application `app/` (auth, db, licensing, posts, scheduler, security, telegram, web, workers), 6 migrations, Dockerfile,
docker-compose.yml (+ https overlay), render.yaml, `deploy/` test scripts, `docs/`, `license_server/` (seller tools), `scripts/`, `tests/` (about 50 files).
Not present: `.gitignore`, `.github/`, `main.py`, `.env`, session files, databases, private keys, issued licenses (scan: none found; the only "PRIVATE KEY" text is a
test fixture string). `app/web/static/vendor/` holds only a README: the pinned htmx file is downloaded when the Docker image is built (needs internet) [SRC].

## 3. Confirmed findings
| # | Finding | Evidence |
|---|---|---|
| 1 | `main.py:16` forces `APP_ENV="development"` (overrides `.env` and the host's variables) | [SRC] |
| 2 | `main.py:7-14` own `.env` reader: it OVERWRITES real environment variables with `.env` values, does not handle inline comments (`KEY=1 # note` keeps the comment). The app already reads `.env` correctly itself (`app/config.py load_settings`, real env wins) | [SRC] |
| 3 | `main.py:20` `check=False`: failed migrations are ignored and the server starts on a missing/old schema | [SRC] |
| 4 | `main.py:24` worker via bare `Popen`: never restarted if it dies, not stopped when the web process stops (orphan), exit code unchecked | [SRC] |
| 5 | `main.py:27,29` port 10926 hard-coded; the Docker image uses 8000 (`Dockerfile` CMD) and `WEB_PORT` | [SRC] |
| 6 | Consequence of 1: development mode accepts `http://`, turns the setup-token lock OFF (`config.py setup_locked`, before this change) and allows `LICENSE_ENFORCEMENT=false`. So the wrapper makes the first-run page claimable by the first visitor and allows an unlicensed run | [SRC]+[TEST] |
| 7 | `app/branding.py:16` footer was still the seller placeholder username (not the official channel) | [SRC] |
| 8 | Licensing could be disabled in any customer copy by `APP_ENV=development` + `LICENSE_ENFORCEMENT=false` (no source edit needed) | [SRC]+[TEST] |
| 9 | `license_type` (`standard`/`commercial`/`trial`) is a label only; a perpetual "trial" was accepted | [SRC] |
| 10 | The ZIP lacks `.gitignore` and `.github/`: 15 existing tests fail on the unchanged tree (`FileNotFoundError .gitignore / ci.yml`). Whether the GitHub repository itself has them is **not verified** | [TEST] |
| 11 | The public key in the project (`constants.py:5`) is a valid 43-character Ed25519 key. The value quoted in your request (42 characters, missing one `l`) is NOT valid (decodes to 31 bytes) and would never verify a license | [TEST] |
| 12 | `requirements.txt` uses version ranges, no lock file: installs are not reproducible | [SRC] |

## 4. Risks needing more evidence [NOT VERIFIED]
Real Telegram posting and rate limits; the worker on a real container; whether the Wispbyte server runs the source in this ZIP; whether `git` history ever contained
secrets (no `.git` supplied); whether the private key exists/backup restores (not accessed, not requested); Telegram rendering of `@AutoPosterHub` as a tappable mention
(no message may be sent here).

## 5. Release blockers (P0)
1. Footer placeholder (fixed in the working copy). 2. Licensing disable-by-environment (fixed: release build flag). 3. Seller-only files in the package (fixed: customer builder).
4. No real post ever sent by the real worker [NOT VERIFIED]. 5. No EULA / sales terms / refund policy (business TODO).

## 6. High
`main.py` defects 1-6 (replaced by `start_single_host.py`); missing `.gitignore` in the ZIP (a missing `.gitignore` in the repo could let `.env` be committed: check on GitHub);
no license revocation or install counting possible offline (limit, documented); requirements not pinned.

## 7. Medium / low
Logs page placeholder; licence page shows the license file path to every logged-in user (path only); the Telegram risk notice needs a lawyer's wording; one schedule per draft;
free hosting cannot run the worker (hosting limitation, not an application defect).

## 8. Existing tests (original tree, actual run)
`python -m unittest discover -s tests -t .`: 730 run, 15 errors (all caused by the missing `.gitignore`/`.github`), 23 skipped (PostgreSQL, FastAPI, htmx file: environment limits). Results after the changes are in the release report.

## 9. Fix choices
Footer constant; `RELEASE_BUILD` flag + builder; trial must expire; `start_single_host.py` (not a patched `main.py`: the Docker design uses separate web/worker containers, so the wrapper
is only for panel hosts); sales register; docs; release manifest. Safe without a live test: footer, flag, trial rule, register, builder, docs. **Needs a live test:** `start_single_host.py` on a real panel host.

## 10. Is the original package safe to distribute as-is?
**No.** (placeholder footer, unlicensed-run switch, seller material, wrong main.py design, no sales/legal material).

## Honest limits
Offline licensing cannot stop source edits, copying a license file, or clock changes. A release flag in editable source is a speed bump, not a lock.
