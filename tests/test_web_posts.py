"""Step 5 web tests: drafts, footer enforcement, targets, media, isolation, CSRF, XSS, secrets. Real ASGI pipeline."""

import io
import json
import logging
import re
import sqlite3
import tempfile
import uuid
from pathlib import Path

from app.security.redact import RedactingFormatter
from tests.fake_telegram import GOOD_API_HASH
from tests.posts_support import PostsBase, jpeg, png
from tests.web_support import Env

FOOTER_LINE = "🤖 Auto Posted by @YourService"


class DraftLifecycleTests(PostsBase):
    async def test_create_reopen_edit_preview_delete(self):
        post_id = await self.make_post(title="Launch", body="Hello\nsecond line", groups=[self.g_ok],
                                       files={"image": ("pic.png", png())})
        page = (await self.alice.get(f"/posts/{post_id}")).body
        self.assertIn("Hello\nsecond line", page)
        self.assertIn(f"/posts/{post_id}/image", page)
        self.assertRegex(page, rf'name="sel_{self.g_ok}"[^>]*checked')
        self.assertNotRegex(page, rf'name="sel_{self.g_ok2}"[^>]*checked')
        self.assertIn("Draft created.", (await self.alice.get(f"/posts/{post_id}", query="notice=created")).body)
        r = await self.save_post(self.alice, post_id, body="Edited", title="Launch 2", groups=[self.g_ok, self.g_ok2])
        self.assertEqual((r.status, r.location), (303, f"/posts/{post_id}?notice=saved"))
        row = self.row(post_id)
        self.assertEqual((row["body"], row["title"], row["status"]), ("Edited", "Launch 2", "draft"))
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM post_targets WHERE post_id=?", (post_id,)).fetchone()[0], 2)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM post_media WHERE post_id=?", (post_id,)).fetchone()[0], 1)  # kept
        listing = (await self.alice.get("/posts")).body
        self.assertIn("Launch 2", listing)
        token = await self.token(self.alice, f"/posts/{post_id}")
        r = await self.alice.post(f"/posts/{post_id}/delete", {"csrf_token": token})
        self.assertEqual(r.location, "/posts?notice=deleted")
        for table in ("posts", "post_targets", "post_media"):
            self.assertEqual(self.env.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0], 0)
        self.assertEqual((await self.alice.get(f"/posts/{post_id}")).status, 404)

    async def test_page_shows_account_target_counter_and_fixed_footer(self):
        page = (await self.alice.get("/posts/new")).body
        for needle in ("Alice", "My Marketing Group", FOOTER_LINE, "cannot be edited or removed", "data-selected-count"):
            self.assertIn(needle, page)
        self.assertNotIn("Announcements", page)  # not postable
        self.assertNotIn("Bob Only", page)
        self.assertEqual(len(re.findall(r"<(?:input|textarea)[^>]*footer", page, re.I)), 0)

    async def test_text_only_and_image_only_drafts(self):
        await self.make_post(body="text only")
        await self.make_post(body="", files={"image": ("a.jpg", jpeg())})
        r = await self.create(self.alice, self.acc_alice, body="  ")
        self.assertEqual(r.status, 400)
        self.assertIn("Write some text or add an image.", r.body)

    async def test_image_can_be_replaced_and_removed(self):
        pid = await self.make_post(files={"image": ("a.png", png())})
        old = self.env.db.conn.execute("SELECT storage_key FROM post_media").fetchone()[0]
        await self.save_post(self.alice, pid, files={"image": ("b.jpg", jpeg(20, 10))})
        row = self.env.db.conn.execute("SELECT * FROM post_media").fetchone()
        self.assertEqual((row["content_type"], row["width"]), ("image/jpeg", 20))
        self.assertNotEqual(row["storage_key"], old)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0], 1)
        await self.save_post(self.alice, pid, extra={"remove_image": "1"})
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM post_media").fetchone()[0], 0)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0], 0)

    async def test_failed_edit_keeps_what_the_user_typed_and_changes_nothing(self):
        pid = await self.make_post(body="original", groups=[self.g_ok])
        r = await self.save_post(self.alice, pid, body="new text " * 3, groups=[self.g_banned])
        self.assertEqual(r.status, 409)
        self.assertIn("new text new text", r.body)
        self.assertEqual(self.row(pid)["body"], "original")
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM post_targets").fetchone()[0], 1)

    async def test_too_long_text_is_rejected_with_a_message(self):
        r = await self.create(self.alice, self.acc_alice, body="a" * 4090)
        self.assertEqual(r.status, 400)
        self.assertRegex(r.body, r"too long\. Telegram allows 4096")
        r = await self.create(self.alice, self.acc_alice, body="a" * 1000, files={"image": ("a.png", png())})
        self.assertEqual(r.status, 400)
        self.assertIn("caption", r.body)

    async def test_non_draft_posts_are_locked(self):
        pid = await self.make_post()
        self.env.db.conn.execute("UPDATE posts SET status='scheduled' WHERE id=?", (pid,))
        self.env.db.conn.commit()
        self.assertEqual((await self.save_post(self.alice, pid)).status, 409)
        token = await self.token(self.alice, f"/posts/{pid}")
        self.assertEqual((await self.alice.post(f"/posts/{pid}/delete", {"csrf_token": token})).status, 409)


class FooterEnforcementTests(PostsBase):
    async def preview(self, pid, client=None):
        return (await (client or self.alice).get(f"/posts/{pid}/preview")).body

    def message_box(self, page):
        return re.search(r'<pre class="message-box">(.*?)</pre>', page, re.S).group(1)

    async def test_preview_shows_text_and_the_footer_exactly_once(self):
        pid = await self.make_post(body="Hello everyone!", groups=[self.g_ok])
        box = self.message_box(await self.preview(pid))
        self.assertEqual(box.count(FOOTER_LINE), 1)
        self.assertTrue(box.startswith("Hello everyone!\n\n"))

    async def test_the_footer_typed_by_the_user_is_not_duplicated(self):
        pid = await self.make_post(body=f"Hello\n\n{FOOTER_LINE}")
        self.assertEqual(self.message_box(await self.preview(pid)).count(FOOTER_LINE), 1)
        self.assertNotIn("YourService", self.row(pid)["body"])

    async def test_a_forged_form_cannot_remove_replace_or_disable_the_footer(self):
        pid = await self.make_post(body="Hello")
        tamper = {"final_message": "No footer here", "footer": "", "include_footer": "0", "no_footer": "1", "disable_footer": "true",
                  "footer_text": "x", "message": "evil", "rendered": "evil", "text": "evil", "caption": "evil", "footer_username": "@evil"}
        await self.save_post(self.alice, pid, body="Hello", extra=tamper)
        r = await self.create(self.alice, self.acc_alice, body="Hello2", extra=tamper)
        self.assertEqual(r.status, 303)
        for post in self.env.db.conn.execute("SELECT id, body FROM posts"):
            box = self.message_box(await self.preview(post["id"]))
            self.assertEqual(box.count(FOOTER_LINE), 1, post["body"])
            self.assertNotIn("evil", box)
            self.assertNotIn("No footer here", box)
        self.assertNotIn("evil", json.dumps([dict(r) for r in self.env.db.conn.execute("SELECT * FROM posts")]))

    async def test_query_string_and_json_payloads_cannot_change_the_footer(self):
        pid = await self.make_post(body="Hello")
        token = await self.token(self.alice, f"/posts/{pid}")
        r = await self.alice.request("POST", f"/posts/{pid}", query="footer=&final_message=evil&include_footer=0",
                                     headers={"content-type": "application/json", "x-csrf-token": token},
                                     raw_body=json.dumps({"final_message": "evil", "footer": None, "body": "json body"}).encode())
        self.assertIn(r.status, (303, 400, 409))
        self.assertEqual(self.row(pid)["body"], "Hello")
        box = self.message_box(await self.preview(pid))
        self.assertEqual((box.count(FOOTER_LINE), "evil" in box), (1, False))

    async def test_there_is_no_route_that_returns_or_accepts_a_final_message(self):
        pid = await self.make_post()
        cols = [r[1] for r in self.env.db.conn.execute("PRAGMA table_info(posts)")]
        self.assertFalse([c for c in cols if "final" in c or "footer" in c or "rendered" in c])
        for method, path in (("GET", f"/posts/{pid}/final"), ("POST", f"/posts/{pid}/footer"), ("POST", f"/posts/{pid}/message")):
            token = await self.token(self.alice)
            r = await self.alice.request(method, path, form={"csrf_token": token, "footer": ""} if method == "POST" else None)
            self.assertEqual(r.status, 404, path)

    async def test_editing_regenerates_the_final_message(self):
        pid = await self.make_post(body="first")
        await self.save_post(self.alice, pid, body="second")
        box = self.message_box(await self.preview(pid))
        self.assertIn("second", box)
        self.assertNotIn("first", box)
        self.assertEqual(box.count(FOOTER_LINE), 1)

    async def test_a_branding_change_reaches_stored_drafts(self):
        from app import branding
        pid = await self.make_post(body="old draft")
        old = branding.FOOTER_USERNAME
        try:
            branding.FOOTER_USERNAME = "@NewBrand"
            box = self.message_box(await self.preview(pid))
        finally:
            branding.FOOTER_USERNAME = old
        self.assertIn("@NewBrand", box)
        self.assertNotIn("@YourService", box)

    async def test_preview_and_sender_use_the_same_function(self):
        from app.posts import composer
        src = Path("app/posts/service.py").read_text(encoding="utf-8")
        self.assertIn("composer.compose(post[\"body\"]", src)
        pid = await self.make_post(body="same")
        self.assertEqual(self.message_box(await self.preview(pid)), composer.compose("same", has_image=False).text)


class TargetValidationTests(PostsBase):
    async def test_only_postable_groups_of_the_own_account_are_accepted(self):
        r = await self.create(self.alice, self.acc_alice, groups=[self.g_banned])
        self.assertEqual(r.status, 409)
        r = await self.create(self.alice, self.acc_alice, groups=[self.g_bob])
        self.assertEqual(r.status, 404)  # another user's group looks like a missing one
        r = await self.create(self.alice, self.acc_alice, groups=[str(uuid.uuid4())])
        self.assertEqual(r.status, 404)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)

    async def test_unavailable_and_restricted_groups_are_rejected(self):
        for status in ("unavailable", "restricted", "no_permission", "unknown"):
            self.env.db.conn.execute("UPDATE telegram_groups SET permission_status=? WHERE id=?", (status, self.g_ok))
            self.env.db.conn.commit()
            self.assertEqual((await self.create(self.alice, self.acc_alice, groups=[self.g_ok])).status, 409, status)
        self.env.db.conn.execute("UPDATE telegram_groups SET permission_status='ok', is_present=0 WHERE id=?", (self.g_ok,))
        self.env.db.conn.commit()
        self.assertEqual((await self.create(self.alice, self.acc_alice, groups=[self.g_ok])).status, 409)

    async def test_forged_permission_state_is_ignored(self):
        r = await self.create(self.alice, self.acc_alice, groups=[self.g_banned],
                              extra={"permission_status": "ok", f"perm_{self.g_banned}": "ok", "status": "ok"})
        self.assertEqual(r.status, 409)

    async def test_malformed_ids_do_not_crash(self):
        for bad in ("x", "1 OR 1=1", "../../etc", "%00", "a" * 150, "-1", "00000000-0000-0000-0000-00000000000g", "' OR '1'='1"):
            token = await self.token(self.alice, f"/posts/new/{self.acc_alice}")
            r = await self.alice.post_multipart("/posts", {"csrf_token": token, "account_id": self.acc_alice, "body": "x",
                                                           f"sel_{bad}": "1"})
            self.assertEqual(r.status, 404, bad)
        for path in ("/posts/not-a-uuid", "/posts/1/preview", "/posts/../groups", f"/posts/{self.acc_alice}x/image", "/posts/new/zzz"):
            self.assertEqual((await self.alice.get(path)).status, 404, path)
        r = await self.create(self.alice, "not-an-account")
        self.assertEqual(r.status, 404)

    async def test_absurd_field_names_are_a_clean_400(self):
        token = await self.token(self.alice)
        r = await self.alice.post_multipart("/posts", {"csrf_token": token, "account_id": self.acc_alice, "body": "x", "sel_" + "a" * 500: "1"})
        self.assertEqual(r.status, 400)

    async def test_at_most_200_targets(self):
        r = await self.create(self.alice, self.acc_alice, groups=[str(uuid.uuid4()) for _ in range(201)])
        self.assertEqual(r.status, 409)
        self.assertIn("at most 200", r.body)

    async def test_200_real_targets_are_accepted_and_201_not(self):
        conn = self.env.db.conn
        ids = []
        for n in range(205):
            gid = str(uuid.uuid4())
            conn.execute("INSERT INTO telegram_groups (id,user_id,account_id,tg_chat_id,title,chat_type,permission_status) "
                         "VALUES (?,?,?,?,?, 'supergroup','ok')", (gid, self.env.admin_id, self.acc_alice, 5000 + n, f"G{n}"))
            ids.append(gid)
        conn.commit()
        self.assertEqual((await self.create(self.alice, self.acc_alice, groups=ids[:200])).status, 303)
        self.assertEqual((await self.create(self.alice, self.acc_alice, groups=ids[:201])).status, 409)

    async def test_groups_that_stop_being_postable_are_flagged_not_silently_sent(self):
        pid = await self.make_post(groups=[self.g_ok, self.g_ok2])
        self.env.db.conn.execute("UPDATE telegram_groups SET permission_status='no_permission' WHERE id=?", (self.g_ok2,))
        self.env.db.conn.commit()
        page = (await self.alice.get(f"/posts/{pid}/preview")).body
        self.assertIn("not postable now", page)
        self.assertIn("1 selected group can no longer be posted to", page)
        editor = (await self.alice.get(f"/posts/{pid}")).body
        self.assertIn("can no longer be posted to", editor)

    async def test_account_is_fixed_after_creation(self):
        pid = await self.make_post(groups=[self.g_ok])
        await self.save_post(self.alice, pid, extra={"account_id": self.acc_bob, "user_id": self.env.admin_id})
        row = self.row(pid)
        self.assertEqual((row["account_id"], row["user_id"]), (self.acc_alice, self.env.admin_id))


class IsolationTests(PostsBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pid = await self.make_post(body="Alice secret draft", groups=[self.g_ok], files={"image": ("a.png", png())})
        self.bob_pid = await self.make_post(self.bob, self.acc_bob, body="Bob draft", title="Bobs title", groups=[self.g_bob])

    async def test_foreign_post_cannot_be_read_edited_previewed_deleted_or_its_image_fetched(self):
        for path in (f"/posts/{self.pid}", f"/posts/{self.pid}/preview", f"/posts/{self.pid}/image"):
            r = await self.bob.get(path)
            self.assertEqual(r.status, 404, path)
            self.assertNotIn("Alice secret", r.body)
        self.assertEqual((await self.save_post(self.bob, self.pid, body="hacked")).status, 404)
        token = await self.token(self.bob, f"/posts/{self.bob_pid}")
        self.assertEqual((await self.bob.post(f"/posts/{self.pid}/delete", {"csrf_token": token})).status, 404)
        row = self.row(self.pid)
        self.assertEqual(row["body"], "Alice secret draft")
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM post_media WHERE post_id=?", (self.pid,)).fetchone()[0], 1)

    async def test_foreign_post_is_absent_from_the_list(self):
        self.assertNotIn("Alice secret", (await self.bob.get("/posts")).body)
        self.assertNotIn(self.pid, (await self.bob.get("/posts")).body)
        self.assertIn("Bobs title", (await self.bob.get("/posts")).body)

    async def test_cannot_create_a_post_for_a_foreign_account_or_with_foreign_groups(self):
        self.assertEqual((await self.create(self.bob, self.acc_alice)).status, 404)
        self.assertEqual((await self.create(self.bob, self.acc_bob, groups=[self.g_ok])).status, 404)
        self.assertEqual((await self.save_post(self.bob, self.bob_pid, groups=[self.g_ok])).status, 404)
        self.assertEqual((await self.alice.get(f"/posts/new/{self.acc_bob}")).status, 404)

    async def test_cross_account_group_injection_is_rejected_for_the_same_user(self):
        conn = self.env.db.conn
        acc2, g2 = str(uuid.uuid4()), str(uuid.uuid4())
        conn.execute("INSERT INTO telegram_accounts (id,user_id,tg_user_id,status) VALUES (?,?,999,'connected')", (acc2, self.env.admin_id))
        conn.execute("INSERT INTO telegram_groups (id,user_id,account_id,tg_chat_id,title,chat_type,permission_status) "
                     "VALUES (?,?,?,77,'Second account group','supergroup','ok')", (g2, self.env.admin_id, acc2))
        conn.commit()
        self.assertEqual((await self.save_post(self.alice, self.pid, groups=[g2])).status, 404)
        self.assertEqual((await self.create(self.alice, self.acc_alice, groups=[g2])).status, 404)
        self.assertEqual((await self.create(self.alice, acc2, groups=[g2])).status, 303)  # its own account works

    async def test_anonymous_visitors_get_nothing(self):
        anon = self.env.client()
        for path in ("/posts", "/posts/new", f"/posts/{self.pid}", f"/posts/{self.pid}/preview", f"/posts/{self.pid}/image"):
            r = await anon.get(path)
            self.assertEqual((r.status, r.location.split("?")[0]), (303, "/login"), path)
            self.assertNotIn("Alice", r.body)
        for path in ("/posts", f"/posts/{self.pid}", f"/posts/{self.pid}/delete"):
            self.assertEqual((await anon.post(path, {})).status, 303, path)

    async def test_image_response_is_safe(self):
        r = await self.alice.get(f"/posts/{self.pid}/image")
        self.assertEqual((r.status, r.headers["content-type"]), (200, "image/png"))
        self.assertEqual(r.raw, png())
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertIn("no-store", r.headers["cache-control"])
        self.assertEqual(r.headers["content-disposition"], "inline")

    async def test_cascade_and_database_constraints(self):
        c = self.env.db.conn
        with self.assertRaises(sqlite3.IntegrityError):  # a target group of another user
            c.execute("INSERT INTO post_targets (post_id, group_id, account_id, user_id) VALUES (?,?,?,?)",
                      (self.pid, self.g_bob, self.acc_alice, self.env.admin_id))
        with self.assertRaises(sqlite3.IntegrityError):  # another account's group under this post
            c.execute("INSERT INTO post_targets (post_id, group_id, account_id, user_id) VALUES (?,?,?,?)",
                      (self.pid, self.g_bob, self.acc_bob, self.env.admin_id))
        with self.assertRaises(sqlite3.IntegrityError):  # media row pointing at another user's post
            c.execute("INSERT INTO post_media (id,user_id,post_id,content_type,size_bytes,width,height,sha256,storage_backend,storage_key)"
                      " VALUES (?,?,?,'image/png',1,1,1,'x','database',?)", (str(uuid.uuid4()), self.member_id, self.pid, "k" * 32))
        with self.assertRaises(sqlite3.IntegrityError):  # two images on one post
            c.execute("INSERT INTO post_media (id,user_id,post_id,content_type,size_bytes,width,height,sha256,storage_backend,storage_key)"
                      " VALUES (?,?,?,'image/png',1,1,1,'x','database',?)", (str(uuid.uuid4()), self.env.admin_id, self.pid, "j" * 32))
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("UPDATE posts SET status='banana' WHERE id=?", (self.pid,))
        c.execute("DELETE FROM users WHERE id=?", (self.env.admin_id,))
        for t in ("posts", "post_targets", "post_media"):
            self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (self.env.admin_id,)).fetchone()[0], 0, t)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM posts WHERE user_id=?", (self.member_id,)).fetchone()[0], 1)


class MediaSecurityTests(PostsBase):
    async def reject(self, name, data, status=400):
        r = await self.create(self.alice, self.acc_alice, files={"image": (name, data)})
        self.assertEqual(r.status, status, (name, r.body[:200]))
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0], 0)
        return r

    async def test_invalid_content_is_rejected_whatever_the_name(self):
        for name, data in (("a.png", b"<script>alert(1)</script>"), ("a.jpg", b"GIF89a....."), ("a.png", b"<?php system($_GET[0]);"),
                           ("a.jpg", png()), ("a.png", jpeg()), ("a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 50), ("a.png", b"x")):
            await self.reject(name, data)

    async def test_unsafe_names_and_path_traversal_are_rejected(self):
        for name in ("../../app/config.py.png", "..%2f..%2fx.png", "a/../../b.png", "/etc/passwd", "x.png\x00.php", ".png", "a.png.php",
                     "a.php", "C:\\Windows\\x.png"):
            await self.reject(name, png())

    async def test_oversized_images_are_rejected(self):
        env = await Env().start(activated=True, with_admin=True, env_overrides={"MAX_UPLOAD_MB": "1"})
        self.assertEqual(env.ctx.posts.max_image_bytes, 1024 * 1024)
        big = png() + b"\x00" * (1024 * 1024)
        import tests.test_web_groups as tg
        r = await env.client().get("/login")
        self.assertEqual(r.status, 200)
        # same ASGI app, same routes, 1 MB limit
        world = env.world
        world.add_account("+8801700000001", 4, code=tg.CODE, first_name="Zed")
        world.chats[4] = [tg.mega(-5, "Zed Group")]
        client = env.client()
        await env.login(client)
        attempt = await env.ctx.telegram_connect.start(env.admin_id, "+8801700000001", tg.API_ID, GOOD_API_HASH)
        acc = (await env.ctx.telegram_connect.submit_code(env.admin_id, attempt, tg.CODE)).account_id
        token = await client.token_from(f"/posts/new/{acc}")
        r = await client.post_multipart("/posts", {"csrf_token": token, "account_id": acc, "body": "x"}, {"image": ("a.png", big)})
        self.assertEqual(r.status, 400)
        self.assertIn("too large", r.body)

    async def test_request_bodies_over_the_hard_limit_get_413(self):
        token = await self.token(self.alice, f"/posts/new/{self.acc_alice}")
        huge = b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024)
        r = await self.alice.post_multipart("/posts", {"csrf_token": token, "account_id": self.acc_alice, "body": "x"}, {"image": ("a.png", huge)})
        self.assertEqual(r.status, 413)

    async def test_anonymous_requests_cannot_send_large_bodies(self):
        anon = self.env.client()
        r = await anon.post_multipart("/posts", {"body": "x"}, {"image": ("a.png", b"\x00" * 200_000)})
        self.assertEqual(r.status, 413)

    async def test_malformed_multipart_is_a_clean_400(self):
        token = await self.token(self.alice)
        r = await self.alice.request("POST", "/posts", headers={"content-type": "multipart/form-data; boundary=zzz", "x-csrf-token": token},
                                     raw_body=b"this is not multipart")
        self.assertEqual(r.status, 400)

    async def test_stored_file_has_random_key_and_the_original_name_is_not_kept(self):
        pid = await self.make_post(files={"image": ("holiday-secret-name.png", png())})
        dump = json.dumps([dict(r) for r in self.env.db.conn.execute("SELECT * FROM post_media")])
        self.assertNotIn("holiday", dump)
        self.assertRegex(self.env.db.conn.execute("SELECT storage_key FROM post_media").fetchone()[0], r"^[0-9a-f]{32}$")
        for path in (f"/posts/{pid}", f"/posts/{pid}/preview", "/posts"):
            page = (await self.alice.get(path)).body
            self.assertNotIn("holiday", page)
            self.assertNotRegex(page, r"[0-9a-f]{32}")  # storage key never rendered
            self.assertNotIn("media_blobs", page)
            self.assertNotIn("/data/media", page)

    async def test_two_uploads_never_share_a_key(self):
        a = await self.make_post(files={"image": ("same.png", png())})
        b = await self.make_post(files={"image": ("same.png", png())})
        keys = [r[0] for r in self.env.db.conn.execute("SELECT storage_key FROM post_media")]
        self.assertEqual(len(set(keys)), 2)
        self.assertNotEqual(a, b)

    async def test_orphaned_blobs_are_collected_but_referenced_ones_are_kept(self):
        await self.make_post(files={"image": ("a.png", png())})
        self.env.db.conn.execute("INSERT INTO media_blobs VALUES (?, ?, ?)", ("f" * 32, b"orphan", int(self.env.clock()) - 7200))
        self.env.db.conn.execute("INSERT INTO media_blobs VALUES (?, ?, ?)", ("e" * 32, b"young", int(self.env.clock())))
        self.env.db.conn.commit()
        self.assertEqual(await self.env.ctx.posts.purge_orphans(), 1)
        left = {r[0] for r in self.env.db.conn.execute("SELECT storage_key FROM media_blobs")}
        self.assertIn("e" * 32, left)
        self.assertNotIn("f" * 32, left)
        self.assertEqual(len(left), 2)

    async def test_failed_database_write_leaves_no_blob_behind(self):
        real = self.env.ctx.posts._write_targets

        async def boom(*a, **k):
            raise RuntimeError("db exploded")
        self.env.ctx.posts._write_targets = boom
        try:
            r = await self.create(self.alice, self.acc_alice, files={"image": ("a.png", png())})
        finally:
            self.env.ctx.posts._write_targets = real
        self.assertEqual(r.status, 500)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0], 0)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)

    async def test_per_user_media_budget(self):
        from app.posts import service
        old = service.MAX_MEDIA_BYTES_PER_USER
        service.MAX_MEDIA_BYTES_PER_USER = len(png()) + 10
        try:
            await self.make_post(files={"image": ("a.png", png())})
            r = await self.create(self.alice, self.acc_alice, files={"image": ("b.png", png())})
        finally:
            service.MAX_MEDIA_BYTES_PER_USER = old
        self.assertEqual(r.status, 400)
        self.assertIn("storage limit", r.body)


class LocalStorageWebTests(PostsBase):
    async def test_local_backend_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = await Env().start(activated=True, with_admin=True, env_overrides={"MEDIA_STORAGE": "local", "MEDIA_DIR": tmp})
            self.assertEqual(env.ctx.posts._storage.backend, "local")
            import tests.test_web_groups as tg
            env.world.add_account("+8801700000002", 5, code=tg.CODE, first_name="Loc")
            env.world.chats[5] = [tg.mega(-7, "Local Group")]
            client = env.client()
            await env.login(client)
            attempt = await env.ctx.telegram_connect.start(env.admin_id, "+8801700000002", tg.API_ID, GOOD_API_HASH)
            acc = (await env.ctx.telegram_connect.submit_code(env.admin_id, attempt, tg.CODE)).account_id
            tok = await client.token_from(f"/groups/accounts/{acc}")
            await client.post(f"/groups/accounts/{acc}/refresh", {"csrf_token": tok})
            token = await client.token_from(f"/posts/new/{acc}")
            r = await client.post_multipart("/posts", {"csrf_token": token, "account_id": acc, "body": "local"}, {"image": ("a.png", png())})
            self.assertEqual(r.status, 303)
            pid = re.search(r"/posts/([0-9a-f-]{36})", r.location).group(1)
            files = [p for p in Path(tmp).rglob("*") if p.is_file()]
            self.assertEqual(len(files), 1)
            self.assertEqual((await client.get(f"/posts/{pid}/image")).status, 200)
            self.assertEqual(env.db.conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0], 0)
            tok = await client.token_from(f"/posts/{pid}")
            await client.post(f"/posts/{pid}/delete", {"csrf_token": tok})
            self.assertEqual([p for p in Path(tmp).rglob("*") if p.is_file()], [])


class CsrfXssSecretTests(PostsBase):
    async def test_csrf_is_enforced_on_every_unsafe_post_route(self):
        pid = await self.make_post(body="keep")
        for path, form in (("/posts", {"account_id": self.acc_alice, "body": "x"}), (f"/posts/{pid}", {"body": "changed"}),
                           (f"/posts/{pid}/delete", {})):
            for extra in ({}, {"csrf_token": "forged"}):
                r = await self.alice.post_multipart(path, {**form, **extra})
                self.assertEqual(r.status, 403, (path, extra))
        bad_origin = await self.alice.post_multipart(f"/posts/{pid}", {"body": "changed", "csrf_token": await self.token(self.alice)},
                                                     origin="https://evil.example")
        self.assertEqual(bad_origin.status, 403)
        self.assertEqual(self.row(pid)["body"], "keep")
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 1)

    async def test_another_users_csrf_token_does_not_work(self):
        pid = await self.make_post(body="keep")
        bob_token = await self.token(self.bob)
        r = await self.alice.post_multipart(f"/posts/{pid}", {"body": "changed", "csrf_token": bob_token})
        self.assertEqual(r.status, 403)

    async def test_xss_is_escaped_everywhere(self):
        payload = '<script>alert(1)</script><img src=x onerror=alert(2)>'
        self.env.db.conn.execute("UPDATE telegram_groups SET title=? WHERE id=?", (payload, self.g_ok))
        self.env.db.conn.commit()
        pid = await self.make_post(title=payload, body=payload + '"><b>', groups=[self.g_ok])
        for path in (f"/posts/{pid}", f"/posts/{pid}/preview", "/posts", "/posts/new"):
            page = (await self.alice.get(path)).body
            self.assertNotIn("<script>alert(1)", page, path)
            self.assertNotIn("<img src=x", page, path)
            self.assertNotIn("<b>", page.replace("<br>", ""), path)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", (await self.alice.get(f"/posts/{pid}/preview")).body)
        # a template expression in the text is just text
        pid2 = await self.make_post(body="{{ 7*7 }} {% raw %} ${7*7}")
        page = (await self.alice.get(f"/posts/{pid2}/preview")).body
        self.assertIn("{{ 7*7 }}", page)
        self.assertNotIn("49", page.split("message-box")[1].split("</pre>")[0])

    async def test_no_inline_script_or_handler_is_introduced(self):
        pid = await self.make_post(body="x")
        for path in (f"/posts/{pid}", f"/posts/{pid}/preview", "/posts"):
            page = (await self.alice.get(path)).body
            self.assertNotRegex(page, r"(?i)<script(?![^>]*\bsrc=)")
            self.assertNotRegex(page, r"(?i)\son\w+\s*=")
            self.assertNotIn("javascript:", page.lower())

    async def test_logs_never_contain_post_text_or_secrets(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(name)s %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            pid = await self.make_post(body="private marketing text 12345", groups=[self.g_ok], files={"image": ("secretname.png", png())})
            await self.save_post(self.alice, pid, body="private marketing text 12345 v2")
            await self.alice.get(f"/posts/{pid}/preview")
            await self.create(self.alice, self.acc_alice, body="x", files={"image": ("bad.png", b"junk")})
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        out = stream.getvalue()
        for secret in ("private marketing text", "secretname", GOOD_API_HASH, "+8801712345678", self.env.settings.app_secret,
                       self.env.settings.session_encryption_key):
            self.assertNotIn(secret, out)
        self.assertIn("post created", out)

    async def test_pages_never_contain_telegram_credentials_or_app_secrets(self):
        pid = await self.make_post(groups=[self.g_ok], files={"image": ("a.png", png())})
        for path in (f"/posts/{pid}", f"/posts/{pid}/preview", "/posts", "/posts/new"):
            page = (await self.alice.get(path)).body
            for secret in (GOOD_API_HASH, "+8801712345678", self.env.settings.app_secret, self.env.settings.session_encryption_key, "api_hash"):
                self.assertNotIn(secret, page)

    async def test_nothing_is_sent_to_telegram_by_any_post_action(self):
        before = len(getattr(self.world, "sent", []))
        pid = await self.make_post(groups=[self.g_ok], files={"image": ("a.png", png())})
        await self.save_post(self.alice, pid, groups=[self.g_ok, self.g_ok2])
        await self.alice.get(f"/posts/{pid}/preview")
        self.assertEqual(len(getattr(self.world, "sent", [])), before)
        src = "".join(p.read_text(encoding="utf-8") for p in Path("app/posts").glob("*.py")) + Path("app/web/post_routes.py").read_text(encoding="utf-8")
        for forbidden in ("send_message", "send_file", "TelegramClientService", "import telethon", "requests.", "urlopen", "httpx", "aiohttp"):
            self.assertNotIn(forbidden, src)

    async def test_no_remote_url_media_feature(self):
        page = (await self.alice.get("/posts/new")).body
        self.assertNotRegex(page, r'(?i)name="(image_url|media_url|url)"')
        pid = await self.make_post(extra={"image_url": "http://169.254.169.254/latest/meta-data", "media_url": "http://x"})
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM post_media WHERE post_id=?", (pid,)).fetchone()[0], 0)

    async def test_permissions_and_navigation(self):
        self.assertIn('href="/posts"', (await self.alice.get("/")).body)
        self.assertIn("posts.manage", __import__("app.auth.permissions", fromlist=["x"]).ROLE_PERMISSIONS["user"])
