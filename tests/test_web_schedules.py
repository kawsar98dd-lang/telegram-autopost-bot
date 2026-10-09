"""Schedule and job pages: full flow through the real request pipeline (CSRF, authentication, ownership)."""

import re
import unittest

from tests.posts_support import PostsBase

ONCE = "2027-01-16T09:00"


class WebSchedBase(PostsBase):
    async def form(self, client, post_id, **extra):
        token = await self.token(client, f"/schedules/new/{post_id}")
        return {"csrf_token": token, "kind": "once", "timezone": "Asia/Dhaka", "once_at": ONCE, "times": "09:00",
                "every": "6", "unit": "hours", "starts": "", "ends": "", **extra}

    async def post_form(self, client, post_id, action, groups=(), **extra):
        form = await self.form(client, post_id, action=action, **extra)
        form.update({f"sel_{g}": "1" for g in groups})
        return await client.post(f"/schedules/new/{post_id}", form)

    async def make_schedule(self, groups=None):
        groups = groups or (self.g_ok,)
        post = await self.make_post(groups=groups)
        r = await self.post_form(self.alice, post, "save", groups)
        self.assertEqual(r.status, 303, r.body[:400])
        return post, re.search(r"/schedules/([0-9a-f-]{36})", r.location).group(1)


class ScheduleFlowTests(WebSchedBase):
    async def test_pages_require_login(self):
        anon = self.env.client()
        for path in ("/schedules", "/jobs", "/schedules/new/" + "0" * 8 + "-0000-0000-0000-" + "0" * 12):
            r = await anon.get(path)
            self.assertEqual(r.status, 303, path)
            self.assertTrue(r.location.startswith("/login"))

    async def test_form_shows_preview_with_footer_and_targets(self):
        post = await self.make_post(groups=(self.g_ok, self.g_ok2))
        r = await self.alice.get(f"/schedules/new/{post}")
        self.assertEqual(r.status, 200)
        self.assertIn("Hello everyone!", r.body)
        self.assertIn("Auto Posted by", r.body)
        self.assertEqual(r.body.count("My Marketing Group"), 1)
        self.assertIn("Asia/Dhaka", r.body)
        self.assertIn("datetime-local", r.body)

    async def test_review_does_not_save_and_save_does(self):
        post = await self.make_post(groups=(self.g_ok,))
        r = await self.post_form(self.alice, post, "review", (self.g_ok,))
        self.assertEqual(r.status, 200)
        self.assertIn("Save schedule", r.body)
        self.assertIn("2027-01-16 09:00 (Asia/Dhaka)", r.body)
        self.assertIn("2027-01-16 03:00 UTC", r.body)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 0)
        r = await self.post_form(self.alice, post, "save", (self.g_ok,))
        self.assertEqual(r.status, 303)
        path = "/" + r.location.split("://", 1)[-1].split("/", 1)[-1]   # the redirect may be absolute
        page = await self.alice.get(path.split("?")[0], query=path.partition("?")[2])
        self.assertIn("Active", page.body)
        self.assertIn("Schedule saved", page.body)
        listing = await self.alice.get("/schedules")
        self.assertIn("once", listing.body)
        self.assertIn("/schedules/", listing.body)
        self.assertEqual((await self.alice.get(f"/posts/{post}")).status, 200)

    async def test_invalid_input_shows_clear_errors_and_saves_nothing(self):
        post = await self.make_post(groups=(self.g_ok,))
        for extra, text in (({"once_at": "2020-01-01T10:00"}, "in the past"), ({"timezone": "Mars/Base"}, "Unknown timezone"),
                            ({"kind": "daily", "times": "25:99"}, "not a valid time"),
                            ({"kind": "custom", "every": "3", "unit": "minutes", "starts": ONCE}, "at least 15 minutes"),
                            ({"kind": "weekly", "times": "09:00"}, "weekday")):
            r = await self.post_form(self.alice, post, "save", (self.g_ok,), **extra)
            self.assertEqual(r.status, 400, extra)
            self.assertIn(text, r.body)
        r = await self.post_form(self.alice, post, "save", ())
        self.assertEqual(r.status, 400)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 0)

    async def test_weekly_and_daily_and_custom_can_be_saved(self):
        for extra in ({"kind": "daily", "times": "09:00, 18:00"}, {"kind": "weekly", "times": "10:00", "wd_0": "1", "wd_4": "1"},
                      {"kind": "custom", "every": "2", "unit": "hours", "starts": ONCE}):
            post = await self.make_post(groups=(self.g_ok,))
            r = await self.post_form(self.alice, post, "save", (self.g_ok,), **extra)
            self.assertEqual(r.status, 303, extra)

    async def test_pause_resume_cancel_via_pages(self):
        post, sid = await self.make_schedule()
        for action, word in (("pause", "Paused"), ("resume", "Active"), ("cancel", "Cancelled")):
            token = (await self.alice.get(f"/schedules/{sid}")).body
            r = await self.alice.post(f"/schedules/{sid}/{action}", {"csrf_token": self.alice.csrf(token)})
            self.assertEqual(r.status, 303, action)
            self.assertIn(word, (await self.alice.get(f"/schedules/{sid}")).body)
        again = await self.alice.post(f"/schedules/{sid}/pause", {"csrf_token": self.alice.csrf((await self.alice.get(f"/schedules/{sid}")).body)})
        self.assertEqual(again.status, 409)
        r = await self.save_post(self.alice, post, body="edited", groups=(self.g_ok,))
        self.assertEqual(r.status, 303)                      # draft again after cancelling

    async def test_other_users_get_404_everywhere(self):
        post, sid = await self.make_schedule()
        bob_token = await self.token(self.bob)
        self.assertEqual((await self.bob.get(f"/schedules/{sid}")).status, 404)
        self.assertEqual((await self.bob.get(f"/schedules/new/{post}")).status, 404)
        for action in ("pause", "resume", "cancel"):
            self.assertEqual((await self.bob.post(f"/schedules/{sid}/{action}", {"csrf_token": bob_token})).status, 404, action)
        r = await self.bob.post(f"/schedules/new/{post}", {"csrf_token": bob_token, "action": "save", "kind": "once", "once_at": ONCE,
                                                           "timezone": "UTC", f"sel_{self.g_ok}": "1"})
        self.assertEqual(r.status, 404)
        self.assertNotIn(sid, (await self.bob.get("/schedules")).body)
        self.assertEqual((await self.bob.get(f"/schedules/{'z' * 36}")).status, 404)

    async def test_group_id_injection_is_rejected(self):
        post = await self.make_post(groups=(self.g_ok,))
        r = await self.post_form(self.alice, post, "save", (self.g_bob,))               # another user's group
        self.assertEqual(r.status, 404)
        r = await self.post_form(self.alice, post, "save", (self.g_ok2,))               # own group, but not a target of this post
        self.assertEqual(r.status, 404)
        r = await self.post_form(self.alice, post, "save", (self.g_banned,))
        self.assertEqual(r.status, 404)
        self.assertEqual(self.env.db.conn.execute("SELECT COUNT(*) FROM schedule_targets").fetchone()[0], 0)

    async def test_csrf_is_enforced_on_every_write(self):
        post, sid = await self.make_schedule()
        for path in (f"/schedules/{sid}/pause", f"/schedules/{sid}/cancel", f"/schedules/new/{post}", "/jobs/" + sid + "/resolve"):
            self.assertEqual((await self.alice.post(path, {})).status, 403, path)
            self.assertEqual((await self.alice.post(path, {"csrf_token": "forged"})).status, 403, path)
        r = await self.alice.post(f"/schedules/{sid}/pause", {"csrf_token": await self.token(self.alice)}, origin="https://evil.example")
        self.assertEqual(r.status, 403)
        self.assertEqual(self.env.db.conn.execute("SELECT status FROM schedules").fetchone()[0], "active")

    async def test_jobs_page_shows_states_and_resolution_buttons_without_secrets(self):
        post, sid = await self.make_schedule()
        db = self.env.db.conn
        jid = "11111111-1111-4111-8111-111111111111"
        db.execute("INSERT INTO posting_jobs (id, user_id, schedule_id, post_id, account_id, group_id, group_title, text_snapshot, "
                   "scheduled_for, next_attempt_at, idempotency_key, status, delivery_state, error_code, error_message) "
                   "SELECT ?, user_id, id, post_id, account_id, NULL, 'Some Group', 't', '2027-01-16 03:00:00.000000', "
                   "'2027-01-16 03:00:00.000000', 'k-web-1', 'failed', 'uncertain', 'delivery_uncertain', 'Not certain.' "
                   "FROM schedules", (jid,))
        db.commit()
        page = await self.alice.get("/jobs")
        self.assertIn("Uncertain", page.body)
        self.assertIn("It was delivered", page.body)
        self.assertIn("Send again", page.body)
        self.assertNotRegex(page.body, r"(?i)api_hash|session_enc|traceback")
        self.assertNotIn(jid, (await self.bob.get("/jobs")).body)
        r = await self.alice.post(f"/jobs/{jid}/resolve", {"csrf_token": self.alice.csrf(page.body), "action": "confirm_sent"})
        self.assertEqual(r.status, 303)
        self.assertIn("Posted", (await self.alice.get("/jobs")).body)
        self.assertEqual((await self.bob.post(f"/jobs/{jid}/resolve", {"csrf_token": await self.token(self.bob), "action": "retry"})).status, 404)

    async def test_preview_page_links_to_scheduling_only_for_drafts(self):
        post, sid = await self.make_schedule()
        self.assertNotIn("/schedules/new/", (await self.alice.get(f"/posts/{post}/preview")).body)
        draft = await self.make_post(groups=(self.g_ok,))
        self.assertIn(f"/schedules/new/{draft}", (await self.alice.get(f"/posts/{draft}/preview")).body)
        self.assertEqual((await self.alice.get(f"/schedules/new/{post}")).status, 409)   # already scheduled


if __name__ == "__main__":
    unittest.main()
