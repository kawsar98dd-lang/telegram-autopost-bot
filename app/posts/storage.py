"""Physical storage of image bytes, separated from the post/media logic.

``MediaStorage`` is the whole contract. Two implementations exist:

* ``DatabaseMediaStorage`` (default): bytes in the ``media_blobs`` table of the PostgreSQL database. Needs no extra
  infrastructure and survives restarts on hosts with a non-persistent disk (Render's free web service).
* ``LocalMediaStorage``: files below MEDIA_DIR. Only sensible with a persistent disk/volume (docker-compose has one).

An object-storage backend (S3 and similar) only has to implement the same four methods; nothing else changes.

Keys are random (``new_key``), are checked against a strict pattern before use, and are never derived from a user's file
name, so there is no path traversal and no way to address another file. Local files are created exclusively (an existing
file is never overwritten), with owner-only permissions and without any extension or execute bit; they are never served
directly, only through the owner-checked page route.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from pathlib import Path
from typing import Any, Callable, Protocol

KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class StorageError(Exception):
    pass


def new_key() -> str:
    return secrets.token_hex(16)


def check_key(key: str) -> str:
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        raise StorageError("invalid storage key")
    return key


class MediaStorage(Protocol):
    backend: str

    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes | None: ...
    async def delete(self, key: str) -> None: ...
    async def purge_unreferenced(self, referenced: set[str], older_than_unix: int) -> int: ...


class DatabaseMediaStorage:
    backend = "database"

    def __init__(self, db: Any, clock: Callable[[], float]) -> None:
        self._db, self._clock = db, clock

    async def put(self, key: str, data: bytes) -> None:
        check_key(key)
        # a duplicate key (never expected with 128 random bits) fails on the primary key instead of overwriting
        await self._db.execute("INSERT INTO media_blobs (storage_key, data, created_unix) VALUES ($1, $2, $3)",
                               key, bytes(data), int(self._clock()))

    async def get(self, key: str) -> bytes | None:
        row = await self._db.fetchrow("SELECT data FROM media_blobs WHERE storage_key = $1", check_key(key))
        return bytes(row["data"]) if row else None

    async def delete(self, key: str) -> None:
        await self._db.execute("DELETE FROM media_blobs WHERE storage_key = $1", check_key(key))

    async def purge_unreferenced(self, referenced: set[str], older_than_unix: int) -> int:
        rows = await self._db.fetch("SELECT storage_key FROM media_blobs WHERE created_unix < $1", older_than_unix)
        stale = [r["storage_key"] for r in rows if r["storage_key"] not in referenced]
        for key in stale:
            await self.delete(key)
        return len(stale)


class LocalMediaStorage:
    backend = "local"

    def __init__(self, base_dir: Path | str) -> None:
        self._base = Path(base_dir).resolve()

    def _path(self, key: str) -> Path:
        check_key(key)
        path = (self._base / key[:2] / key).resolve()
        if self._base not in path.parents:  # cannot happen with a validated key; kept as a second barrier
            raise StorageError("path escapes the media directory")
        return path

    def _put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # O_EXCL: never overwrite
        except FileExistsError:
            raise StorageError("storage key already exists") from None
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

    def _get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def _delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass

    def _purge(self, referenced: set[str], older_than_unix: int) -> int:
        removed = 0
        if not self._base.is_dir():
            return 0
        for path in self._base.glob("??/*"):
            if path.is_file() and KEY_PATTERN.match(path.name) and path.name not in referenced \
                    and path.stat().st_mtime < older_than_unix:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put, key, bytes(data))

    async def get(self, key: str) -> bytes | None:
        return await asyncio.to_thread(self._get, key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete, key)

    async def purge_unreferenced(self, referenced: set[str], older_than_unix: int) -> int:
        return await asyncio.to_thread(self._purge, referenced, older_than_unix)


def make_storage(settings, db: Any, clock: Callable[[], float]) -> MediaStorage:
    if settings.media_storage == "local":
        return LocalMediaStorage(settings.media_dir)
    return DatabaseMediaStorage(db, clock)
