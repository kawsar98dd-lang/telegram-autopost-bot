# License (for customers)

This product is licensed with a **signed license file** that you receive from the seller after your purchase. Verification happens inside
your own installation, offline: no license server, no internet connection and no recurring fee are involved.

## Install your license

1. You receive a file named like `order-1042.license.json` (and sometimes a second file ending in `.b64`, the same license as one line of text).
2. Pick **one** way:
   * **Easiest (recommended):** open the `.b64` file (or ask the seller for it), copy its single line and put it into your `.env` file:
     `LICENSE_FILE_CONTENT=<the line>`; then restart: `docker compose up -d` (web and worker both pick it up).
   * **Or as a file:** `chmod 644 order-1042.license.json`, then `docker compose cp order-1042.license.json web:/srv/data/license.json`
     and `docker compose cp order-1042.license.json worker:/srv/data/license.json` (both services use the same data volume, so the first copy
     is enough if the volume is shared; copying to both is harmless). Without Docker: put the file where `LICENSE_FILE` points (default `./data/license.json`).
3. Sign in and open the **License** page: it shows the license ID and the expiry date (or "perpetual" licenses show none).

`LICENSE_FILE_CONTENT` wins over the file if both exist. Never post your license text publicly: anyone with the file can use it.

## Replace or renew

Install the new file or text the same way. A running installation notices a changed file within about a minute; on the License page the
**Check now** button re-reads it immediately. If you changed `.env`, run `docker compose up -d` again.

## What the application does

* On start and about every 30 seconds it re-reads the license and verifies the seller's signature, the product, the optional bound
  address (`APP_URL`), the issue date and the expiry date.
* **Valid:** everything works.
* **Missing, modified, expired, for another product, or signed by someone else:** the web pages lock and redirect to the License page, which
  tells you what to do; the worker sends nothing. Health checks stay reachable. No data is deleted. Jobs that were already queued stay
  queued and are sent after a valid license is installed (jobs that are more than 6 hours late are recorded as "missed" instead).
* Messages you may see: *No license file found* (nothing at the path / no text), *signature is not valid* (file edited or not from the
  seller), *different product*, *different address* (the license is bound to another `APP_URL` host), *expired* (install the renewal),
  *system clock is behind the license issue date* (fix the server's clock).

## Honest limits

* A license file can be copied; offline software cannot count installations or recall a license. The seller relies on the sales terms.
* You receive the source code. Anyone who edits the licensing code can bypass the check; this is not "unbreakable" protection and the seller
  does not claim it is. Editing the code can also void support.
* The seller does **not** collect a machine fingerprint; the optional address binding is the only installation rule.

## License types

`standard`, `commercial` and `trial` are labels agreed with the seller and shown on the License page; the program does not unlock or lock
different features per type. A **trial** license always has an end date; a perpetual license never expires. A license may be bound to your
public address (`APP_URL` host) if the seller and you agreed that.

## Production versus development

`APP_ENV=production` always enforces the license. In the **customer package** (a release build) `LICENSE_ENFORCEMENT=false` is rejected in every mode and
the first-run page always needs a `SETUP_TOKEN`; `APP_ENV=development` there only allows plain `http://`. The check can be switched off only
in the seller's own development tree. Someone who edits the source code can still change this (see the limits above).
