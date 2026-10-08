# Step 5 - Posts, image and the mandatory footer

Step 5 prepares posts. It does **not** send anything to Telegram and does **not** schedule anything: that is Step 6.

## What exists

* A **Posts** section: create, edit, save, reopen, preview and delete drafts.
* A draft = plain text + one optional image + the target groups it is meant for.
* Every draft belongs to one authenticated user **and** one connected Telegram account (chosen when the draft is created; it cannot be changed afterwards).

## Post lifecycle

`posts.status` has the values `draft, ready, scheduled, sending, sent, failed, cancelled` (the full list is already in the database so Step 6 needs no schema change). Step 5 only creates and edits `draft` posts. Only drafts can be edited or deleted; any other status answers "conflict".

## Mandatory footer

* The footer text lives in exactly one place: `app/branding.py` (`FOOTER_SEPARATOR`, `FOOTER_TEXT`, `FOOTER_USERNAME`). It is source code owned by the seller, not an `.env` value and not a dashboard setting. Active footer: `🤖 Auto Posted by @YourService` (preceded by the separator line already defined in branding).
* `app/posts/composer.py` is the only place that builds the final message: `compose(body, has_image)`. The preview uses it today; the Step 6 worker must call the same function when it sends.
* The stored draft never contains the footer, and the browser never supplies the final text. Any footer typed or pasted by the user (end, middle, different case or spacing, even split/nested) is removed and exactly one footer is appended. Unknown form fields, JSON bodies and query parameters are ignored.
* Because only the text is stored, changing the branding later changes every existing draft's preview and every future send without touching the posting code.
* The footer counts against the Telegram length limit.
* Honest limitation: Telegram cannot make part of a message immutable for the owner of a personal account. The footer is enforced by this application only.

## Text and Telegram limits

Plain text only. Nothing is parsed as Markdown or HTML and (from Step 6) messages are to be sent without a parse mode, so no markup can be injected. Line breaks are kept; control characters and bidirectional-override characters are removed; trailing blank lines are normalised before the footer is added.

All limits are in `app/posts/limits.py`: 4096 characters for a text message, 1024 for a photo caption (both counted in UTF-16 units, footer included; Premium accounts may have more, the lower value works for everybody), photo at most 10 MB and width+height at most 10000 pixels with ratio at most 20:1. The configured `MAX_UPLOAD_MB` can only lower the image limit.

## Image handling

* One image per post, JPG or PNG only. No other media types and **no download from URLs** (no SSRF surface).
* The browser's MIME type is never used. The content must start with a real PNG/JPEG signature, have a sound header (PNG: IHDR with correct CRC and IEND at the end; JPEG: frame header and end marker), acceptable dimensions, and the extension must agree with the content.
* The uploaded file name is only checked (no path characters, no `..`, no control characters, allowed extension) and then discarded. The stored object gets a random 128-bit key. The original name is not stored.
* The image is never decoded or re-encoded by the server and never executed.
* Images are served only by `GET /posts/{id}/image`, which checks login and ownership and answers 404 for anything else; `nosniff` and `no-store` are sent.
* Limits: request body for upload routes is capped (and only for requests that carry a session cookie), per-user image storage is capped, a user can keep at most 500 posts.

## Storage abstraction

`app/posts/storage.py` defines `MediaStorage` (`put`, `get`, `delete`, `purge_unreferenced`). Post and media logic never touch files or tables of the storage directly.

* `database` (default, `MEDIA_STORAGE=database`): bytes in PostgreSQL (`media_blobs`). Works on Render's free web service, whose disk is not persistent, with no extra infrastructure.
* `local` (`MEDIA_STORAGE=local`): files below `MEDIA_DIR`, created exclusively (never overwritten), owner-only permissions, no extension. Use only with a persistent disk/volume.
* A future object-storage backend implements the same four methods.

Orphaned objects (for example after a crash between storing bytes and saving the row) are removed by an hourly housekeeping step after a one-hour grace period.

## Target groups

A draft can target groups of **its own account** that currently have permission status "Can post" and are still present. The server checks, on every save: the account belongs to the user, every group id is a well-formed id of a group of that user and that account, every group is currently postable, and at most 200 are chosen. Foreign, unknown or malformed ids answer 404 (indistinguishable from missing), a restricted/unavailable group answers 409. The permission state sent by the browser is ignored.

The database adds a second barrier: `post_targets` references posts and groups through composite foreign keys that include both `user_id` and `account_id`, so a group of another user or account cannot be linked even by buggy code. (`posts.account_id` is a plain foreign key because a composite key cannot be added to an existing table in the portable migration subset; the pairing is enforced by the service and by `post_targets`.)

A group may stop being postable after the draft was saved. Preview marks such targets and Step 6 **must** re-check every target (and regenerate the message) at send time.

## Database (migration `0005_posts`)

Extends `posts` (adds `account_id`, `status`), adds `post_targets`, `post_media`, `media_blobs` and two unique indexes used as composite foreign-key targets. Nothing from Step 1-4 is recreated or changed.

## Security model

Per-user and per-account isolation on every query; CSRF on every state-changing request (also for multipart); output escaped by Jinja autoescape, no inline scripts; no secrets or post text in logs (only ids and counts); rate limit on saves; uploads limited before and after parsing; no Telegram call of any kind in Step 5.

## Tests

`tests/test_posts_units.py` (footer, limits, image validation, storage, multipart, settings), `tests/test_web_posts.py` (lifecycle, footer tampering, targets, isolation, media, CSRF, XSS, logs), plus a PostgreSQL test and a real-FastAPI test that CI must execute (no skips allowed; `scripts/ci_verify.py` raises the required counts).

## Known limitations

* Plain text only: no bold/italic/links formatting.
* One image per post, JPG/PNG only.
* Text with an image is limited to the 1024-character caption limit.
* The footer cannot be technically protected against the owner of the Telegram account.
* A multipart file whose bytes contain the form boundary string breaks its own upload (it is rejected with an error).
* Nothing is sent; no single-group test send exists in this step.

## Manual verification on the Render test deployment

Use the already connected Telegram account. Never send codes, hashes, phone numbers or keys to anyone.

1. Open **Posts** in the left menu, then press **New draft**.
2. Type a short harmless text, for example `Test draft 1`.
3. Tick exactly ONE group that was verified as "Can post" in Step 4.
4. Choose a small harmless JPG or PNG image.
5. Press **Save draft**.
6. Open the draft again from the Posts list: the text, the image and the one ticked group must still be there.
7. Press **Preview**: the text, the image, "1 target group" and the group name are shown, with the footer exactly once.
8. Check the editor: the footer is shown only as read-only text; there is no field to change or remove it.
9. Reload the page (and wait a few minutes, then reload again): the draft and image are still there.
10. Check that no message arrived in any Telegram group, and that the Render logs show no codes, hashes, phone numbers or keys.
