"""Post drafts: create / edit / delete / preview, with strict per-user and per-account isolation.

Rules for every method:
* all SQL is scoped by BOTH user_id and (where it exists) account_id; ids from the browser are never trusted;
* a foreign or unknown post/account/group looks exactly like a missing one (PostNotFound / GroupNotFound);
* the post's account is fixed when it is created and is never read from an edit form;
* targets must be groups of the post's account that are CURRENTLY postable (permission 'ok' and still present);
* the final message is never accepted from the client: it is composed from the stored body (app/posts/composer.py).
Nothing here talks to Telegram.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..db.models import new_id
from ..telegram.group_sync import MAX_SELECTED_GROUPS, GroupNotFound, NotSelectable, TooManySelected, is_uuid
from . import composer
from .limits import (MAX_MEDIA_BYTES_PER_USER, MAX_POSTS_PER_USER, MAX_TARGETS_PER_POST, MAX_TITLE_CHARS,
                     ORPHAN_GRACE_SECONDS, max_image_bytes)
from .media import MediaError, validate_image
from .storage import MediaStorage, new_key

log = logging.getLogger(__name__)
assert MAX_TARGETS_PER_POST == MAX_SELECTED_GROUPS  # one ceiling for Step 4 and Step 5


class PostNotFound(Exception):
    pass


class PostLocked(Exception):
    """Only drafts can be changed or deleted."""


class PostInvalid(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class Upload:
    filename: str
    data: bytes


def clean_title(raw: str) -> str:
    text = "".join(ch for ch in (raw or "") if ch.isprintable())
    return text.strip()


class PostService:
    def __init__(self, db: Any, connect: Any, storage: MediaStorage, settings: Any, clock: Callable[[], float]) -> None:
        self._db, self._connect, self._storage, self._settings, self._clock = db, connect, storage, settings, clock

    @property
    def max_image_bytes(self) -> int:
        return max_image_bytes(self._settings.max_upload_mb)

    # ---- accounts / groups ------------------------------------------------------------------------------
    async def connected_accounts(self, user_id: str) -> list[dict]:
        return [a for a in await self._connect.list_accounts(user_id) if a["status"] == "connected"]

    async def account(self, user_id: str, account_id: str) -> dict:
        for acc in await self.connected_accounts(user_id):
            if acc["id"].lower() == (account_id or "").lower():
                return acc
        raise PostNotFound()

    async def eligible_groups(self, user_id: str, account_id: str) -> list[dict]:
        rows = await self._db.fetch(
            "SELECT id, tg_chat_id, title, username, chat_type, is_enabled FROM telegram_groups "
            "WHERE user_id = $1 AND account_id = $2 AND permission_status = 'ok' AND is_present = TRUE "
            "ORDER BY lower(title), id", user_id, account_id)
        return [dict(r) | {"id": str(r["id"]), "is_enabled": bool(r["is_enabled"])} for r in rows]

    # ---- reading ------------------------------------------------------------------------------------------
    async def list_posts(self, user_id: str) -> list[dict]:
        rows = await self._db.fetch(
            "SELECT p.id, p.title, p.body, p.status, p.updated_at, a.first_name, a.username, "
            "(SELECT COUNT(*) FROM post_targets t WHERE t.post_id = p.id AND t.user_id = p.user_id) AS target_count, "
            "(SELECT COUNT(*) FROM post_media m WHERE m.post_id = p.id AND m.user_id = p.user_id) AS media_count "
            "FROM posts p JOIN telegram_accounts a ON a.id = p.account_id AND a.user_id = p.user_id "
            "WHERE p.user_id = $1 AND p.account_id IS NOT NULL ORDER BY p.updated_at DESC, p.id LIMIT 200", user_id)
        return [dict(r) | {"id": str(r["id"]), "target_count": int(r["target_count"]), "has_image": int(r["media_count"]) > 0}
                for r in rows]

    async def get(self, user_id: str, post_id: str) -> dict:
        if not is_uuid(post_id):
            raise PostNotFound()
        row = await self._db.fetchrow(
            "SELECT id, account_id, title, body, status, created_at, updated_at FROM posts "
            "WHERE id = $1 AND user_id = $2 AND account_id IS NOT NULL", post_id, user_id)
        if row is None:
            raise PostNotFound()
        post = dict(row) | {"id": str(row["id"]), "account_id": str(row["account_id"])}
        media = await self._db.fetchrow(
            "SELECT id, content_type, size_bytes, width, height FROM post_media WHERE post_id = $1 AND user_id = $2",
            post["id"], user_id)
        post["media"] = (dict(media) | {"id": str(media["id"])}) if media else None
        trows = await self._db.fetch(
            "SELECT g.id, g.title, g.username, g.chat_type, g.permission_status, g.is_present FROM post_targets t "
            "JOIN telegram_groups g ON g.id = t.group_id AND g.user_id = t.user_id AND g.account_id = t.account_id "
            "WHERE t.post_id = $1 AND t.user_id = $2 AND t.account_id = $3 ORDER BY lower(g.title), g.id",
            post["id"], user_id, post["account_id"])
        post["targets"] = [dict(r) | {"id": str(r["id"]),
                                      "eligible": r["permission_status"] == "ok" and bool(r["is_present"])} for r in trows]
        return post

    async def preview(self, user_id: str, post_id: str) -> dict:
        post = await self.get(user_id, post_id)
        message = composer.compose(post["body"], has_image=post["media"] is not None)  # same path the sender will use
        problems: list[str] = []
        if not post["targets"]:
            problems.append("No target group is selected.")
        stale = sum(1 for t in post["targets"] if not t["eligible"])
        if stale:
            problems.append(f"{stale} selected group{'s' if stale != 1 else ''} can no longer be posted to and would be skipped.")
        try:
            composer.check(message)
        except composer.MessageError as exc:
            problems.append(exc.message)
        return {"post": post, "message": message, "problems": problems,
                "eligible_targets": len(post["targets"]) - stale}

    async def image(self, user_id: str, post_id: str) -> tuple[str, bytes]:
        post = await self.get(user_id, post_id)
        meta = post["media"]
        if meta is None:
            raise PostNotFound()
        row = await self._db.fetchrow(
            "SELECT storage_key FROM post_media WHERE id = $1 AND user_id = $2 AND post_id = $3",
            meta["id"], user_id, post["id"])
        data = await self._storage.get(row["storage_key"]) if row else None
        if data is None:
            raise PostNotFound()
        return meta["content_type"], data

    # ---- validation helpers ---------------------------------------------------------------------------------
    def _text_errors(self, title_raw: str, body_raw: str, has_image: bool) -> tuple[str, str, list[str]]:
        errors: list[str] = []
        title = clean_title(title_raw)
        if len(title) > MAX_TITLE_CHARS:
            errors.append(f"The title may have at most {MAX_TITLE_CHARS} characters.")
        body = ""
        try:
            body = composer.normalize_body(body_raw)
            composer.check(composer.compose(body, has_image=has_image))
        except composer.MessageError as exc:
            errors.append(exc.message)
        if not body and not has_image:
            errors.append("Write some text or add an image.")
        return title, body, errors

    async def _validated_targets(self, user_id: str, account_id: str, group_ids: set[str]) -> set[str]:
        if len(group_ids) > MAX_TARGETS_PER_POST:
            raise TooManySelected()
        if not all(is_uuid(g) for g in group_ids):
            raise GroupNotFound()
        owned = {str(r["id"]).lower(): r for r in await self._db.fetch(
            "SELECT id, permission_status, is_present FROM telegram_groups WHERE user_id = $1 AND account_id = $2",
            user_id, account_id)}
        wanted = {g.lower() for g in group_ids}
        if not wanted <= set(owned):
            raise GroupNotFound()  # unknown id, or a group of another account/user: the same answer
        if any(owned[g]["permission_status"] != "ok" or not bool(owned[g]["is_present"]) for g in wanted):
            raise NotSelectable()
        return {str(owned[g]["id"]) for g in wanted}  # canonical ids from the database

    def _check_upload(self, upload: Upload | None):
        if upload is None:
            return None
        try:
            return validate_image(upload.filename, upload.data, self.max_image_bytes)
        except MediaError as exc:
            raise PostInvalid([exc.message]) from None

    async def _media_budget_ok(self, user_id: str, new_bytes: int, replacing_post: str | None) -> bool:
        row = await self._db.fetchrow(
            "SELECT COALESCE(SUM(size_bytes), 0) AS used FROM post_media WHERE user_id = $1 AND post_id <> $2",
            user_id, replacing_post or new_id())
        return int(row["used"]) + new_bytes <= MAX_MEDIA_BYTES_PER_USER

    # ---- writing --------------------------------------------------------------------------------------------
    async def create(self, user_id: str, account_id: str, title_raw: str, body_raw: str, group_ids: set[str],
                     upload: Upload | None) -> str:
        account = await self.account(user_id, account_id)  # PostNotFound for foreign / unknown / disconnected accounts
        image = self._check_upload(upload)
        title, body, errors = self._text_errors(title_raw, body_raw, image is not None)
        if errors:
            raise PostInvalid(errors)
        count = await self._db.fetchrow("SELECT COUNT(*) AS n FROM posts WHERE user_id = $1", user_id)
        if int(count["n"]) >= MAX_POSTS_PER_USER:
            raise PostInvalid([f"You can keep at most {MAX_POSTS_PER_USER} posts. Delete old ones first."])
        targets = await self._validated_targets(user_id, account["id"], group_ids)
        if image and not await self._media_budget_ok(user_id, image.size, None):
            raise PostInvalid(["The storage limit for your images is reached. Delete old posts with images first."])
        post_id = new_id()
        key = await self._store_blob(image)
        try:
            async with self._db.transaction() as tx:
                await tx.execute(
                    "INSERT INTO posts (id, user_id, account_id, title, body, status) VALUES ($1, $2, $3, $4, $5, 'draft')",
                    post_id, user_id, account["id"], title or None, body)
                await self._write_targets(tx, post_id, user_id, account["id"], targets)
                if image:
                    await self._insert_media(tx, post_id, user_id, image, key)
        except BaseException:
            await self._drop_blob(key)
            raise
        log.info("post created (post_id=%s targets=%d image=%s)", post_id, len(targets), bool(image))
        return post_id

    async def update(self, user_id: str, post_id: str, title_raw: str, body_raw: str, group_ids: set[str],
                     upload: Upload | None, remove_image: bool) -> None:
        post = await self.get(user_id, post_id)  # PostNotFound for foreign posts
        if post["status"] != "draft":
            raise PostLocked()
        account_id = post["account_id"]  # fixed at creation; never taken from the request
        image = self._check_upload(upload)
        keeps_image = image is not None or (post["media"] is not None and not remove_image)
        title, body, errors = self._text_errors(title_raw, body_raw, keeps_image)
        if errors:
            raise PostInvalid(errors)
        targets = await self._validated_targets(user_id, account_id, group_ids)
        if image and not await self._media_budget_ok(user_id, image.size, post["id"]):
            raise PostInvalid(["The storage limit for your images is reached. Delete old posts with images first."])
        old_key = None
        if post["media"] is not None and (image is not None or remove_image):
            row = await self._db.fetchrow("SELECT storage_key FROM post_media WHERE id = $1 AND user_id = $2",
                                          post["media"]["id"], user_id)
            old_key = row["storage_key"] if row else None
        key = await self._store_blob(image)
        try:
            async with self._db.transaction() as tx:
                await tx.execute(
                    "UPDATE posts SET title = $3, body = $4, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = $1 AND user_id = $2 AND status = 'draft'", post["id"], user_id, title or None, body)
                await tx.execute("DELETE FROM post_targets WHERE post_id = $1 AND user_id = $2", post["id"], user_id)
                await self._write_targets(tx, post["id"], user_id, account_id, targets)
                if old_key is not None:
                    await tx.execute("DELETE FROM post_media WHERE post_id = $1 AND user_id = $2", post["id"], user_id)
                if image:
                    await self._insert_media(tx, post["id"], user_id, image, key)
        except BaseException:
            await self._drop_blob(key)
            raise
        await self._drop_blob(old_key)
        log.info("post updated (post_id=%s targets=%d image=%s)", post["id"], len(targets), keeps_image)

    async def delete(self, user_id: str, post_id: str) -> None:
        post = await self.get(user_id, post_id)
        if post["status"] != "draft":
            raise PostLocked()
        row = await self._db.fetchrow("SELECT storage_key FROM post_media WHERE post_id = $1 AND user_id = $2",
                                      post["id"], user_id)
        await self._db.execute("DELETE FROM posts WHERE id = $1 AND user_id = $2 AND status = 'draft'", post["id"], user_id)
        await self._drop_blob(row["storage_key"] if row else None)
        log.info("post deleted (post_id=%s)", post["id"])

    async def purge_orphans(self) -> int:
        """Delete stored image bytes that no post_media row refers to (left by a crash between the two steps)."""
        rows = await self._db.fetch("SELECT storage_key FROM post_media WHERE storage_backend = $1", self._storage.backend)
        referenced = {r["storage_key"] for r in rows}
        return await self._storage.purge_unreferenced(referenced, int(self._clock()) - ORPHAN_GRACE_SECONDS)

    # ---- internals ----------------------------------------------------------------------------------------------
    async def _store_blob(self, image) -> str | None:
        if image is None:
            return None
        key = new_key()
        await self._storage.put(key, image.data)
        return key

    async def _drop_blob(self, key: str | None) -> None:
        if key:
            try:
                await self._storage.delete(key)
            except Exception:  # noqa: BLE001 - best effort; purge_orphans() collects what is left
                log.warning("could not delete a stored image file")

    async def _insert_media(self, tx, post_id: str, user_id: str, image, key: str) -> None:
        await tx.execute(
            "INSERT INTO post_media (id, user_id, post_id, kind, content_type, size_bytes, width, height, sha256, "
            "storage_backend, storage_key) VALUES ($1, $2, $3, 'image', $4, $5, $6, $7, $8, $9, $10)",
            new_id(), user_id, post_id, image.content_type, image.size, image.width, image.height, image.sha256,
            self._storage.backend, key)

    async def _write_targets(self, tx, post_id: str, user_id: str, account_id: str, targets: set[str]) -> None:
        for gid in sorted(targets):
            await tx.execute("INSERT INTO post_targets (post_id, group_id, account_id, user_id) VALUES ($1, $2, $3, $4)",
                             post_id, gid, account_id, user_id)
        # re-check inside the transaction that every target is still postable; otherwise everything is rolled back
        row = await tx.fetchrow(
            "SELECT COUNT(*) AS n FROM post_targets t JOIN telegram_groups g ON g.id = t.group_id AND g.user_id = t.user_id "
            "AND g.account_id = t.account_id WHERE t.post_id = $1 AND t.user_id = $2 AND g.permission_status = 'ok' "
            "AND g.is_present = TRUE", post_id, user_id)
        if int(row["n"]) != len(targets):
            raise NotSelectable()
