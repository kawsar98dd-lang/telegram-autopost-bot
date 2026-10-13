"""Customer release build: licensing cannot be switched off by environment variables; trial rules; sales register; shipped constants."""

import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.config import ConfigError, settings_from_env
from app.licensing import constants
from app.licensing.manager import LicenseState
from app.licensing.offline import LicenseFileError, verify_license_text
from app.licensing.protocol import b64url_decode
from app.licensing.setup import LicenseSetupError, build_license_manager
from app.security.crypto import Cipher, generate_key
from scripts import build_customer_zip as bcz
from tests.support import signing
from tests.test_offline_license import HOST, NOW, PRODUCT, make_license, prod_env

ROOT = Path(__file__).resolve().parent.parent
GOOD_TOKEN = "a-proper-random-setup-token-123"


def dev_env(**extra):
    return prod_env(APP_ENV="development", APP_URL="http://localhost:8000", **extra)


class ShippedConstantsTests(unittest.TestCase):
    def test_the_shipped_public_key_is_a_valid_ed25519_public_key_and_offline_mode_is_selected(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        self.assertEqual(len(constants.LICENSE_PUBLIC_KEY), 43)
        self.assertEqual(len(b64url_decode(constants.LICENSE_PUBLIC_KEY)), 32)
        Ed25519PublicKey.from_public_bytes(b64url_decode(constants.LICENSE_PUBLIC_KEY))   # raises if it is not a valid key
        self.assertEqual(constants.PRODUCT_ID, "telegram-auto-poster")
        self.assertEqual(constants.LICENSE_SERVER_URL, "")                                 # offline: no license server
        self.assertFalse(constants.RELEASE_BUILD)                                          # the seller tree; only the package sets True


class ReleaseBuildEnforcementTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(constants, "RELEASE_BUILD", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_license_enforcement_cannot_be_disabled_in_any_mode(self):
        for env in (prod_env(LICENSE_ENFORCEMENT="false"), dev_env(LICENSE_ENFORCEMENT="false")):
            with self.assertRaises(ConfigError) as ctx:
                settings_from_env(env)
            self.assertIn("LICENSE_ENFORCEMENT", str(ctx.exception))

    def test_builder_also_refuses_if_validation_were_bypassed(self):
        settings = settings_from_env(dev_env())
        object.__setattr__(settings, "license_enforcement", False)
        with self.assertRaises(LicenseSetupError):
            asyncio.run(build_license_manager(settings, None, Cipher(settings.session_encryption_key)))

    def test_development_mode_alone_does_not_open_the_setup_page_or_skip_the_license(self):
        settings = settings_from_env(dev_env())
        self.assertTrue(settings.setup_locked)                      # no SETUP_TOKEN -> locked even with APP_ENV=development
        self.assertFalse(settings_from_env(dev_env(SETUP_TOKEN=GOOD_TOKEN)).setup_locked)
        with self.assertRaises(ConfigError):
            settings_from_env(dev_env(SETUP_TOKEN="short"))
        with mock.patch.object(constants, "LICENSE_PUBLIC_KEY", signing.generate_keypair()[1]):
            manager = asyncio.run(build_license_manager(settings, None, Cipher(settings.session_encryption_key)))
        self.assertFalse(manager.status().enabled)                  # no license installed -> locked, not open
        self.assertEqual(manager.status().state, LicenseState.NOT_ACTIVATED)


class SellerTreeStillAllowsDevelopmentTests(unittest.TestCase):
    def test_explicit_development_mode_works_in_the_seller_tree_only(self):
        self.assertFalse(constants.RELEASE_BUILD)
        settings = settings_from_env(dev_env(LICENSE_ENFORCEMENT="false"))
        self.assertFalse(settings.license_enforcement)
        self.assertFalse(settings.setup_locked)
        with self.assertRaises(ConfigError):                       # production still refuses it
            settings_from_env(prod_env(LICENSE_ENFORCEMENT="false"))


class TrialLicenseTests(unittest.TestCase):
    def setUp(self):
        self.private, self.public = signing.generate_keypair()

    def verify(self, text):
        return verify_license_text(text, public_key=self.public, product=PRODUCT, host=HOST, now=NOW)

    def test_trial_must_expire_and_has_no_other_special_behaviour(self):
        self.assertEqual(self.verify(make_license(self.private, ltype="trial", expires=NOW + 86400)).license_type, "trial")
        with self.assertRaises(LicenseFileError):
            self.verify(make_license(self.private, ltype="trial"))                 # perpetual trial is inconsistent
        self.assertTrue(self.verify(make_license(self.private, ltype="commercial")).perpetual)   # paid licenses may be perpetual


class SellerToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.private, self.public = signing.generate_keypair()
        self.key = self.dir / "k.key"
        self.key.write_text(self.private + "\n")
        os.chmod(self.key, 0o600)
        from license_server import issue_license
        self.issue = issue_license.main

    def tearDown(self):
        self.tmp.cleanup()

    def run_issue(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.issue(["--private-key-file", str(self.key), "--customer-ref", "order-9", "--out", str(self.dir / "l.json"),
                                   "--yes", *args], now=NOW)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_trial_cannot_be_issued_as_perpetual(self):
        code, _, err = self.run_issue("--license-type", "trial", "--perpetual")
        self.assertEqual(code, 1)
        self.assertIn("trial", err)
        self.assertFalse((self.dir / "l.json").exists())
        self.assertEqual(self.run_issue("--license-type", "trial", "--expires", "2027-03-31")[0], 0)

    def test_sales_register_is_non_sensitive_and_outside_the_project(self):
        register = self.dir / "sales" / "register.jsonl"
        code, out, err = self.run_issue("--license-type", "commercial", "--perpetual", "--host", "poster.example.com",
                                        "--register-file", str(register))
        self.assertEqual(code, 0, err)
        line = json.loads(register.read_text().strip())
        self.assertEqual((line["customer_ref"], line["license_type"], line["expires"], line["host"]), ("order-9", "commercial", "perpetual", "poster.example.com"))
        raw = register.read_text()
        license_text = (self.dir / "l.json").read_text()
        signature = json.loads(license_text)["signature"]
        for secret in (self.private, signature, "payload"):
            self.assertNotIn(secret, raw)                          # no key, no signature, no license text
        self.assertNotIn(self.private, out + err)
        inside = ROOT / "tests" / "_register.jsonl"
        code, _, err = self.run_issue("--license-type", "standard", "--perpetual", "--register-file", str(inside))
        self.assertEqual(code, 1)
        self.assertFalse(inside.exists())

    def test_keygen_template_keeps_the_release_flag(self):
        from license_server import keygen
        template = keygen._CONSTANTS_TEMPLATE.format(product="telegram-auto-poster", url="", public="A" * 43)
        self.assertIn("RELEASE_BUILD = False", template)


class PackageFlagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "data", "test-report.json"))
        self.out = Path(self.tmp.name) / "release"

    def tearDown(self):
        self.tmp.cleanup()

    def test_customer_package_is_a_release_build_and_the_source_tree_is_not_touched(self):
        target, _ = bcz.build(self.root, self.out)
        with zipfile.ZipFile(target) as zf:
            consts = zf.read("telegram-auto-poster/app/licensing/constants.py").decode()
            self.assertEqual(consts.count("RELEASE_BUILD = True"), 1)
            self.assertNotIn("RELEASE_BUILD = False", consts)
            self.assertIn(constants.LICENSE_PUBLIC_KEY, consts)
            self.assertIn('LICENSE_SERVER_URL = ""', consts)
            self.assertIn("start_single_host.py", "".join(zf.namelist()))
            self.assertNotIn("telegram-auto-poster/main.py", zf.namelist())
        self.assertIn("RELEASE_BUILD = False", (self.root / "app" / "licensing" / "constants.py").read_text())

    def test_manifest_lists_the_hash_of_every_packed_file_and_no_secret(self):
        import hashlib
        target, _ = bcz.build(self.root, self.out)
        with zipfile.ZipFile(target) as zf:
            manifest = json.loads(zf.read("telegram-auto-poster/RELEASE_MANIFEST.json"))
            self.assertEqual((manifest["product"], manifest["version"], manifest["release_build"]), ("telegram-auto-poster", "0.1.0", True))
            self.assertEqual(manifest["license_public_key"], constants.LICENSE_PUBLIC_KEY)
            packed = {n[len("telegram-auto-poster/"):] for n in zf.namelist()} - {"RELEASE_MANIFEST.json"}
            self.assertEqual(set(manifest["files"]), packed)
            for rel, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(zf.read("telegram-auto-poster/" + rel)).hexdigest(), digest, rel)
            self.assertNotIn("PRIVATE", zf.read("telegram-auto-poster/RELEASE_MANIFEST.json").decode().upper().replace("PRIVATE SIGNING KEY", ""))

    def test_build_refuses_if_the_flag_line_is_missing(self):
        consts = self.root / "app" / "licensing" / "constants.py"
        consts.write_text(consts.read_text().replace("RELEASE_BUILD = False", "OTHER = 1"))
        with self.assertRaises(bcz.ReleaseError):
            bcz.build(self.root, self.out)

    def test_extracted_customer_package_refuses_to_run_without_enforcement(self):
        target, _ = bcz.build(self.root, self.out)
        dest = Path(self.tmp.name) / "customer"
        with zipfile.ZipFile(target) as zf:
            zf.extractall(dest)
        code = ("import os; os.environ.update(APP_ENV='development', LICENSE_ENFORCEMENT='false', APP_URL='http://localhost:8000', "
                "APP_SECRET='s'*40, DATABASE_URL='postgresql://u:p@h/d', SESSION_ENCRYPTION_KEY=%r); "
                "from app.config import settings_from_env, ConfigError\n"
                "try:\n    settings_from_env(dict(os.environ)); print('ACCEPTED')\nexcept ConfigError as e:\n    print('REFUSED', e)" % generate_key())
        result = subprocess.run([sys.executable, "-c", code], cwd=dest / "telegram-auto-poster", capture_output=True, text=True,
                                env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertIn("REFUSED", result.stdout, result.stderr)
        self.assertNotIn("ACCEPTED", result.stdout)


if __name__ == "__main__":
    unittest.main()
