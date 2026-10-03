"""Where could a secret leak to? Static and dynamic checks for the setup token and Telegram material."""

import io
import logging
import re
import unittest
from pathlib import Path

from app.security.redact import RedactingFormatter
from tests.fake_telegram import GOOD_API_HASH, GOOD_API_ID
from tests.web_support import PASSWORD, Env

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ABCDE-FGHJK-MNPQR-STVWX"


class SetupTokenExposureTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_never_appears_in_any_response_header_body_or_log(self):
        env = await Env().start(env_overrides={"SETUP_TOKEN": TOKEN})
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(name)s %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            c = env.client()
            page = await c.get("/setup")
            self.assertIn('name="setup_token"', page.body)  # the input exists ...
            token = c.csrf(page.body)
            form = {"csrf_token": token, "email": "a@example.com", "password": PASSWORD, "password_confirm": PASSWORD}
            wrong = await c.post("/setup", {**form, "setup_token": TOKEN[:-1] + "Y"})  # a nearly-right guess
            right = await c.post("/setup", {**form, "setup_token": TOKEN})
            self.assertEqual((wrong.status, right.status), (400, 303))
            await c.get("/")
            await c.get("/setup")  # 404 afterwards
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        for reply in c.log:  # ... but its value is never rendered anywhere
            self.assertNotIn(TOKEN, reply.body)
            self.assertNotIn(TOKEN[:-1], reply.body)
            self.assertNotIn(TOKEN, str(reply.headers) + str(reply.set_cookies))
        self.assertNotIn(TOKEN, stream.getvalue())
        self.assertNotIn(TOKEN, repr(env.settings))

    async def test_setup_page_is_permanently_gone_so_the_token_cannot_be_reused(self):
        env = await Env().start(env_overrides={"SETUP_TOKEN": TOKEN})
        c = env.client()
        token = c.csrf((await c.get("/setup")).body)
        await c.post("/setup", {"csrf_token": token, "email": "a@example.com", "password": PASSWORD,
                                "password_confirm": PASSWORD, "setup_token": TOKEN})
        other = env.client()
        for method in ("GET", "POST"):
            r = await other.request(method, "/setup", form={"setup_token": TOKEN} if method == "POST" else None)
            self.assertEqual(r.status, 404)
        self.assertEqual(env.db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_no_template_renders_the_setup_token(self):
        for path in (ROOT / "app" / "web" / "templates").rglob("*.html"):
            for expr in re.findall(r"\{\{.*?\}\}", path.read_text(encoding="utf-8")):
                self.assertNotIn("setup_token", expr, path.name)
                self.assertNotIn("settings", expr, path.name)


class TelegramSecretPlacementTests(unittest.TestCase):
    def test_telegram_credentials_never_travel_in_urls(self):
        allowed = {"notice", "gone", "next"}
        used = set()
        for path in (ROOT / "app" / "web").glob("*.py"):
            used |= set(re.findall(r'request\.query\.get\("([a-z_]+)"', path.read_text(encoding="utf-8")))
        self.assertTrue(used <= allowed, used - allowed)
        for template in (ROOT / "app" / "web" / "templates").rglob("*.html"):
            text = template.read_text(encoding="utf-8")
            for form in re.findall(r"<form[^>]*>", text):
                self.assertNotIn('method="get"', form.lower(), template.name)  # secrets are only ever POSTed
        add = (ROOT / "app" / "web" / "templates" / "telegram_add.html").read_text(encoding="utf-8")
        self.assertIn('method="post"', add)
        self.assertIn('type="password" name="api_hash"', add)

    def test_deployment_files_never_set_or_embed_telegram_values(self):
        files = [ROOT / "Dockerfile", ROOT / "docker-compose.yml", ROOT / "docker-compose.https.yml",
                 ROOT / ".github" / "workflows" / "ci.yml", *(ROOT / "deploy").glob("*")]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"TELEGRAM_API_(ID|HASH)\s*[:=]\s*\S", path.name)
            self.assertNotRegex(text, r"(?i)api_hash\s*[:=]\s*[\"']?[0-9a-f]{16,}", path.name)
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertRegex(env_example, r"(?m)^TELEGRAM_API_ID=$")
        self.assertRegex(env_example, r"(?m)^TELEGRAM_API_HASH=$")

    def test_the_docker_image_copies_no_environment_or_deploy_material(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        copies = re.findall(r"(?m)^COPY\s+(\S+)", dockerfile)
        self.assertEqual(sorted(copies), ["app", "migrations", "requirements.txt", "scripts/vendor_htmx.py"])
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        for entry in (".env", ".env.*", "tests", "license_server", "data"):
            self.assertIn(entry, ignore)

    def test_the_bootstrap_paste_mentions_no_telegram_values_and_asks_for_none(self):
        text = (ROOT / "deploy" / "cloud-init-https-test.sh").read_text(encoding="utf-8")
        text = text.replace("telegram-autopost-bot", "")  # the (public) repository name is the only allowed mention
        self.assertNotRegex(text, r"(?i)telegram|api_hash|api_id|otp|session")
        docs = (ROOT / "docs" / "STEP3_REAL_TELEGRAM_VERIFICATION_ANDROID_CLOUD.md").read_text(encoding="utf-8")
        self.assertIn("only** into the app's web form", docs)

    def test_documents_contain_no_real_looking_secret_values(self):
        for path in list((ROOT / "docs").glob("*.md")) + list((ROOT / "deploy").glob("*")) + [ROOT / "README.md"]:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"[0-9a-f]{32}", path.name)
            self.assertNotRegex(text, r"gAAAA[A-Za-z0-9_-]{20,}", path.name)
            self.assertNotRegex(text, r"\b[A-HJ-NP-Z2-9]{5}(-[A-HJ-NP-Z2-9]{5}){3}\b", path.name) if path.name != "README.md" else None


if __name__ == "__main__":
    unittest.main()
