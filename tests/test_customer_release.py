"""The customer ZIP builder: what ships, what never ships, and that it refuses unsafe builds."""

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import build_customer_zip as bcz
from tests.support import signing

REPO = Path(__file__).resolve().parent.parent


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        shutil.copytree(REPO, self.root, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "data", "test-report.json"))
        self.private, self.public = signing.generate_keypair()
        consts = self.root / "app" / "licensing" / "constants.py"
        text = consts.read_text()
        import re
        consts.write_text(re.sub(r'LICENSE_PUBLIC_KEY = ".*?"', f'LICENSE_PUBLIC_KEY = "{self.public}"', text))
        self.out = Path(self.tmp.name) / "release"

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, **kw):
        return bcz.build(self.root, self.out, **kw)

    def names(self, target):
        with zipfile.ZipFile(target) as zf:
            return zf.namelist()

    def test_zip_contains_the_application_and_nothing_seller_only(self):
        target, digest = self.build()
        names = self.names(target)
        for needed in ("app/web/main.py", "app/workers/main.py", "app/licensing/offline.py", "app/licensing/constants.py", "migrations/0006_scheduler.sql",
                       "Dockerfile", "docker-compose.yml", "docker-compose.https.yml", "deploy/Caddyfile", "requirements.txt", ".env.example",
                       "README.md", "docs/LICENSING.md", "docs/INSTALLATION_GUIDE.md", "docs/STEP6_SCHEDULER.md"):
            self.assertIn("telegram-auto-poster/" + needed, names)
        for banned in ("license_server", "tests/", ".github", "render.yaml", "cloud-init", "diagnose.sh", "check-secrets", "token-cleanup", "ci_verify",
                       "STEP6_GITHUB_ANDROID_GUIDE_BN", "SELLER_GUIDE", "STEP3_", "build_customer_zip"):
            self.assertFalse([n for n in names if banned in n], banned)
        self.assertTrue(all(n.startswith("telegram-auto-poster/") for n in names))

    def test_no_secrets_sessions_databases_or_keys_in_the_zip(self):
        target, _ = self.build(private_key_file=self._key_file())
        with zipfile.ZipFile(target) as zf:
            for name in zf.namelist():
                self.assertIsNone(bcz.FORBIDDEN_NAME.search(name) if name.rsplit("/", 1)[-1] != ".env.example" else None, name)
                data = zf.read(name)
                self.assertNotIn(self.private.encode(), data, name)
                self.assertNotIn(b"PRIVATE KEY-----", data, name)

    def _key_file(self):
        path = Path(self.tmp.name) / "k.key"
        path.write_text(self.private + "\n")
        return path

    def test_public_key_is_embedded_and_checksum_is_correct(self):
        import hashlib
        target, digest = self.build()
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), digest)
        self.assertEqual(target.with_name(target.name + ".sha256").read_text().split()[0], digest)
        with zipfile.ZipFile(target) as zf:
            self.assertIn(self.public, zf.read("telegram-auto-poster/app/licensing/constants.py").decode())

    def test_build_is_refused_without_a_public_key(self):
        consts = self.root / "app" / "licensing" / "constants.py"
        consts.write_text(consts.read_text().replace(self.public, ""))
        with self.assertRaises(bcz.ReleaseError):
            self.build()
        self.build(allow_empty_public_key=True)       # explicit non-release override

    def test_build_is_refused_when_forbidden_files_or_secrets_would_be_packed(self):
        for rel, content in ((("app/leftover.session"), b"x"), ("app/.env", b"APP_SECRET=x"), ("app/license.json", b"{}"),
                             ("docs/key.pem", b"x"), ("app/data.db", b"x")):
            path = self.root / rel
            path.write_bytes(content)
            with self.assertRaises(bcz.ReleaseError, msg=rel):
                self.build()
            path.unlink()
        leak = self.root / "app" / "oops.py"
        leak.write_text("KEY = '''-----BEGIN PRIVATE KEY-----\nabc'''\n")
        with self.assertRaises(bcz.ReleaseError):
            self.build()
        leak.write_text(f'KEY = "{self.private}"\n')
        with self.assertRaises(bcz.ReleaseError):
            self.build(private_key_file=self._key_file())      # the actual private key text anywhere in the package
        leak.write_text("from license_server import signing\n")
        with self.assertRaises(bcz.ReleaseError):
            self.build()                                       # packed code must not need excluded modules
        leak.write_text("BOT = '123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'\n")
        with self.assertRaises(bcz.ReleaseError):
            self.build()

    def test_example_env_is_allowed_and_real_env_is_not_packed(self):
        (self.root / ".env").write_text("APP_SECRET=real\n")   # a real .env in the project root is simply never listed
        target, _ = self.build()
        names = self.names(target)
        self.assertIn("telegram-auto-poster/.env.example", names)
        self.assertNotIn("telegram-auto-poster/.env", names)

    def test_refuses_to_overwrite_an_existing_release(self):
        self.build()
        with self.assertRaises(bcz.ReleaseError):
            self.build()


if __name__ == "__main__":
    unittest.main()
