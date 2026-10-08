"""Shared helpers of the Step 5 tests: tiny valid images and a ready-made installation with groups."""

import re
import struct
import zlib

from tests.test_web_groups import Base, mega


def png(width=8, height=8) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg(width=8, height=8) -> bytes:
    return (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\xff\xc0\x00\x0b\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00"
            + b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00" + b"\x12\x34" + b"\xff\xd9")


class PostsBase(Base):
    """Alice (admin) and Bob each own a connected account; Alice's groups are loaded."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.refresh(self.alice, self.acc_alice)
        await self.refresh(self.bob, self.acc_bob)
        self.g_ok = self.gid(self.acc_alice, "My Marketing Group")
        self.g_ok2 = self.gid(self.acc_alice, "Private Community")
        self.g_banned = self.gid(self.acc_alice, "Announcements")
        self.g_bob = self.gid(self.acc_bob, "Bob Only")

    async def token(self, client, path="/posts"):
        return client.csrf((await client.get(path)).body)

    async def create(self, client, account_id, *, body="Hello everyone!", title="T", groups=(), files=None, extra=None):
        token = await self.token(client)
        form = {"csrf_token": token, "account_id": account_id, "title": title, "body": body,
                **{f"sel_{g}": "1" for g in groups}, **(extra or {})}
        return await client.post_multipart("/posts", form, files)

    async def make_post(self, client=None, account_id=None, **kw):
        client, account_id = client or self.alice, account_id or self.acc_alice
        r = await self.create(client, account_id, **kw)
        assert r.status == 303, (r.status, r.body[:300])
        return re.search(r"/posts/([0-9a-f-]{36})", r.location).group(1)

    async def save_post(self, client, post_id, *, body="Hello everyone!", title="T", groups=(), files=None, extra=None):
        token = await self.token(client)
        form = {"csrf_token": token, "title": title, "body": body, **{f"sel_{g}": "1" for g in groups}, **(extra or {})}
        return await client.post_multipart(f"/posts/{post_id}", form, files)

    def row(self, post_id):
        return self.env.db.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
