# Seller guide (SELLER ONLY - never ship this file)

Everything here is for the person who sells the software. The customer package built by `scripts/build_customer_zip.py` leaves this
file, `license_server/` and the test/CI material out.

## Which files are seller-only and which belong to the customer

| Seller only (NEVER in the customer ZIP) | Why |
|---|---|
| `license_server/` (`keygen.py`, `issue_license.py`, `signing.py`, `safety.py`, `schema.sql`, `PROTOCOL.md`) | signing tools; the PRIVATE key is used with them (the key itself lives outside the project) |
| the private key file (e.g. `~/secrets/license_private.key`) | the only thing that can create valid licenses |
| issued license files (`*.license.json`) | belong to individual buyers |
| `docs/SELLER_GUIDE.md`, `docs/STEP3_*`, `docs/STEP6_GITHUB_ANDROID_GUIDE_BN.md` | seller procedures and test guides |
| `tests/`, `.github/`, `scripts/ci_verify.py`, `scripts/build_customer_zip.py` | your development and release machinery |
| `render.yaml`, `deploy/cloud-init-https-test.sh`, `deploy/diagnose.sh`, `deploy/check-secrets.sh`, `deploy/poster-token-cleanup.sh` | your own test deployments |

| Customer package (built for you by the script) | |
|---|---|
| `app/`, `migrations/`, `Dockerfile`, `docker-compose.yml`, `docker-compose.https.yml`, `deploy/Caddyfile`, `requirements.txt`, `.env.example`, `.gitignore`, `.dockerignore` | the product |
| `scripts/generate_keys.py`, `scripts/vendor_htmx.py` | customer helpers |
| `README.md`, `CHANGELOG.md`, `docs/INSTALLATION_GUIDE.md`, `docs/LICENSING.md`, `docs/STEP4_GROUPS.md`, `docs/STEP5_POSTS.md`, `docs/STEP6_SCHEDULER.md` | documentation |

## One time: create the signing key (on your own computer)

```
python license_server/keygen.py --private-out ~/secrets/license_private.key --write-constants app/licensing/constants.py
```

* Creates the private key (owner-only, mode 600, never printed, never overwritten) and writes only the PUBLIC key into `app/licensing/constants.py`.
  The tool refuses a key path inside the project folder. Commit the changed `constants.py` (public key only).
* Back it up straight away: at least two offline copies (for example an encrypted USB drive and an encrypted password-manager attachment).
  Test a restored copy by issuing a license and checking that your own installation accepts it.
* Lost key = you can no longer issue licenses for builds that contain the old public key. Leaked key = anyone can forge licenses: generate a
  new pair, release a new build with the new public key, re-issue licenses to paying customers (old builds cannot be revoked offline).

## For every sale: issue a license

```
python license_server/issue_license.py --private-key-file ~/secrets/license_private.key \
    --customer-ref "order-1042" --license-type standard --perpetual \
    --out ~/licenses/order-1042.license.json --also-base64
```

* Choose exactly one of `--perpetual` or `--expires 2027-12-31` (last valid day, UTC). `--host poster.example.com` optionally binds the
  license to the customer's public address.
* The tool prints a summary and asks you to type the license ID before signing. The key file must be owner-only and outside the project;
  the output must be outside the project and must not exist. The key is never printed or put into an error message.
* Send the `.license.json` (and the `.b64` one-line text) to the customer through a normal channel and keep your own copy.

## Build the customer package

```
python scripts/build_customer_zip.py --out ~/release --private-key-file ~/secrets/license_private.key
```

The build is refused if: a forbidden file would be packed (`.env`, sessions, databases, keys, license files), a private-key marker, your private
key's text or a Telegram token pattern appears in any packed file, packed code imports `license_server`/`tests`, or the build has no
public key. It writes `telegram-auto-poster-customer-<version>.zip` and `<zip>.sha256`; send both to the customer.

## Release checklist

1. `python -m unittest discover -s tests -t .` passes and the GitHub CI checks are green on the commit you release.
2. `app/licensing/constants.py` contains your PUBLIC key; `LICENSE_SERVER_URL` is empty (offline mode).
3. Real test done on a test installation: real worker, one post to a test group (see `docs/STEP6_SCHEDULER.md`).
4. Build the customer ZIP with the script; unzip it in an empty folder and read the file list; no `license_server`, no `tests`, no secrets.
5. Install it on a clean machine following `docs/INSTALLATION_GUIDE.md` only, with a freshly issued test license; first run, license, one post.
6. Send ZIP + checksum + the buyer's license file separately.
7. Keep: sales terms, the Telegram-risk notice (see the installation guide), and your backups of the private key.

## What offline licensing cannot do (do not promise it)

No activation counting, no revocation, no protection against clock changes or source-code edits, no machine locking. It prevents forged,
edited or extended license files and casual copying of the software without a license.
