"""Offline signed license: verification, tampering, expiry, production start-up, development separation, seller tools.

Keys here are generated in memory for the test; no real key exists in this repository.
"""

import asyncio
import base64
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import Clock, migrated_db, signing
from app.config import ConfigError, settings_from_env
from app.licensing import constants, offline
from app.licensing.gate import LicenseGateMiddleware
from app.licensing.manager import LicenseState
from app.licensing.offline import LicenseFileError, OfflineLicenseManager, verify_license_text
from app.licensing.setup import LicenseSetupError, build_license_manager
from app.security.crypto import Cipher, generate_key

PRODUCT = "telegram-auto-poster"
HOST = "poster.example.com"
NOW = 1_800_000_000
DAY = 86400


def make_license(private, *, product=PRODUCT, issued=NOW - DAY, expires=None, host="", ltype="standard", ref="order-1",
                 license_id="LIC-ABCD1234EFGH", mutate=None):
    payload = signing.build_offline_payload(license_id=license_id, product=product, customer_ref=ref, license_type=ltype,
                                            issued_at=issued, expires_at=expires, host=host)
    if mutate:
        mutate(payload)
    return json.dumps(signing.sign_payload(private, payload))


class Base(unittest.TestCase):
    def setUp(self):
        self.private, self.public = signing.generate_keypair()
        self.other_private, self.other_public = signing.generate_keypair()

    def verify(self, text, **kw):
        args = dict(public_key=self.public, product=PRODUCT, host=HOST, now=NOW)
        args.update(kw)
        return verify_license_text(text, **args)

    def refused(self, text, state, **kw):
        with self.assertRaises(LicenseFileError) as ctx:
            self.verify(text, **kw)
        self.assertEqual(ctx.exception.state, state, ctx.exception.message)
        return ctx.exception


class VerificationTests(Base):
    def test_valid_perpetual_license(self):
        lic = self.verify(make_license(self.private))
        self.assertTrue(lic.perpetual)
        self.assertIsNone(lic.expires_at)
        self.assertEqual((lic.license_id, lic.customer_ref, lic.license_type), ("LIC-ABCD1234EFGH", "order-1", "standard"))

    def test_valid_time_limited_license_and_expiry_enforced(self):
        text = make_license(self.private, expires=NOW + 10 * DAY)
        self.assertEqual(self.verify(text).expires_at, NOW + 10 * DAY)
        self.assertEqual(self.verify(text, now=NOW + 10 * DAY).license_id, "LIC-ABCD1234EFGH")   # last second still valid
        err = self.refused(text, LicenseState.EXPIRED, now=NOW + 10 * DAY + 1)
        self.assertIn("expired", err.message)
        self.assertEqual(err.expires_at, NOW + 10 * DAY)

    def test_signature_by_another_key_is_rejected(self):
        self.refused(make_license(self.other_private), LicenseState.TAMPERED)

    def test_tampered_payload_is_rejected(self):
        doc = json.loads(make_license(self.private, expires=NOW + DAY))
        for field, value in (("expires_at", NOW + 999 * DAY), ("customer_ref", "someone-else"), ("license_type", "commercial"),
                             ("perpetual", True), ("license_id", "LIC-XXXXXXXXXXXX")):
            forged = json.loads(json.dumps(doc))
            forged["payload"][field] = value
            self.refused(json.dumps(forged), LicenseState.TAMPERED)

    def test_tampered_signature_is_rejected(self):
        doc = json.loads(make_license(self.private))
        sig = doc["signature"]
        doc["signature"] = ("A" if sig[0] != "A" else "B") + sig[1:]
        self.refused(json.dumps(doc), LicenseState.TAMPERED)
        doc["signature"] = "not base64 !!"
        self.refused(json.dumps(doc), LicenseState.TAMPERED)

    def test_wrong_product_is_rejected_even_when_signed(self):
        self.refused(make_license(self.private, product="some-other-product"), LicenseState.MISMATCH)

    def test_host_binding_when_present(self):
        text = make_license(self.private, host=HOST)
        self.assertEqual(self.verify(text).host, HOST)
        self.refused(text, LicenseState.MISMATCH, host="other.example.org")
        self.verify(make_license(self.private), host="anything.example.org")   # unbound license works anywhere

    def test_license_issued_in_the_future_is_refused_but_small_skew_tolerated(self):
        self.refused(make_license(self.private, issued=NOW + 3 * DAY), LicenseState.TAMPERED)
        self.verify(make_license(self.private, issued=NOW + 3600))

    def test_online_protocol_payloads_cannot_pass_as_license_files(self):
        online = {"v": 1, "license_id": "LIC-ABCD1234EFGH", "product": PRODUCT, "status": "active", "issued_at": NOW - 1,
                  "expires_at": None, "installation_id": "x", "host": HOST, "nonce": "n"}
        self.refused(json.dumps(signing.sign_payload(self.private, online)), LicenseState.TAMPERED)

    def test_inconsistent_perpetual_and_expiry_combinations(self):
        self.refused(make_license(self.private, mutate=lambda p: p.update(expires_at=NOW + DAY)), LicenseState.TAMPERED)  # perpetual + expiry
        self.refused(make_license(self.private, expires=NOW + DAY, mutate=lambda p: p.update(expires_at=None)), LicenseState.TAMPERED)
        self.refused(make_license(self.private, expires=NOW + DAY, mutate=lambda p: p.update(expires_at=True)), LicenseState.TAMPERED)
        self.refused(make_license(self.private, expires=NOW + DAY, mutate=lambda p: p.update(expires_at=NOW - 5 * DAY)), LicenseState.TAMPERED)

    def test_malformed_files(self):
        good = json.loads(make_license(self.private))
        cases = ["", "not json", "[]", "null", "{}", json.dumps({"payload": good["payload"]}),
                 json.dumps({**good, "extra": 1}), json.dumps({"payload": "x", "signature": good["signature"]}),
                 json.dumps({"payload": good["payload"], "signature": 5}), "x" * (offline.MAX_LICENSE_BYTES + 1)]
        for text in cases:
            self.refused(text, LicenseState.TAMPERED)

    def test_signed_but_malformed_fields(self):
        for mutate in (lambda p: p.update(license_id="x"), lambda p: p.update(customer_ref=""), lambda p: p.update(license_type="free"),
                       lambda p: p.update(issued_at="1"), lambda p: p.update(issued_at=True), lambda p: p.update(perpetual="yes"),
                       lambda p: p.update(host=7), lambda p: p.update(type="other"), lambda p: p.update(v=2)):
            self.refused(make_license(self.private, mutate=mutate), LicenseState.TAMPERED)

    def test_garbage_public_key_never_crashes(self):
        for key in ("", "short", "!!!", "A" * 43):
            self.refused(make_license(self.private), LicenseState.TAMPERED, public_key=key)


class ManagerTests(Base):
    def manager(self, tmp, text=None, *, inline="", enforcement=True, clock=None, **kw):
        path = Path(tmp) / "license.json"
        if text is not None:
            path.write_text(text, encoding="utf-8")
        return OfflineLicenseManager(public_key=self.public, product=PRODUCT, host=HOST, file_path=str(path), inline_content=inline,
                                     enforcement=enforcement, clock=clock or Clock(), **kw)

    def test_missing_license_file_gives_an_actionable_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self.manager(tmp, clock=lambda: NOW).status()
        self.assertEqual(st.state, LicenseState.NOT_ACTIVATED)
        self.assertFalse(st.enabled)
        self.assertIn("license.json", st.detail)
        self.assertIn("LICENSE_FILE", st.detail)

    def test_valid_file_activates_and_summary_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self.manager(tmp, make_license(self.private, expires=NOW + DAY), clock=lambda: NOW)
            self.assertTrue(m.status().enabled)
            s = m.summary()
        self.assertEqual((s.state, s.license_id, s.expires_at, s.enabled), (LicenseState.ACTIVE, "LIC-ABCD1234EFGH", NOW + DAY, True))
        self.assertNotIn(self.private, repr(s))

    def test_expiry_is_noticed_while_running(self):
        now = [NOW]
        with tempfile.TemporaryDirectory() as tmp:
            m = self.manager(tmp, make_license(self.private, expires=NOW + 100), clock=lambda: now[0])
            self.assertTrue(m.status().enabled)
            now[0] = NOW + 101
            self.assertEqual(m.status().state, LicenseState.EXPIRED)
            self.assertFalse(m.status().enabled)

    def test_replacing_the_file_takes_effect_without_restart(self):
        now = [NOW]
        with tempfile.TemporaryDirectory() as tmp:
            m = self.manager(tmp, make_license(self.private, expires=NOW + 100), clock=lambda: now[0], reload_seconds=30)
            now[0] = NOW + 200
            self.assertEqual(m.status().state, LicenseState.EXPIRED)
            (Path(tmp) / "license.json").write_text(make_license(self.private, expires=NOW + 100 * DAY), encoding="utf-8")
            self.assertEqual(m.status().state, LicenseState.EXPIRED)          # not looked at again yet (30 s throttle)
            now[0] += 31
            self.assertEqual(m.status().state, LicenseState.ACTIVE)
            asyncio.run(m.verify())                                           # "Check now" re-reads immediately

    def test_inline_content_json_and_base64_and_priority_over_file(self):
        text = make_license(self.private)
        with tempfile.TemporaryDirectory() as tmp:
            for inline in (text, base64.b64encode(text.encode()).decode(), base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")):
                self.assertTrue(self.manager(tmp, "garbage", inline=inline, clock=lambda: NOW).status().enabled)
            self.assertEqual(self.manager(tmp, inline="%%%not-a-license%%%", clock=lambda: NOW).status().state, LicenseState.TAMPERED)

    def test_unreadable_and_oversized_files_fail_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.manager(tmp, "x" * 20000, clock=lambda: NOW).status().state, LicenseState.TAMPERED)
            (Path(tmp) / "license.json").write_bytes(b"\xff\xfe\x00bad")
            self.assertFalse(self.manager(tmp, clock=lambda: NOW).status().enabled)

    def test_enforcement_off_is_development_only_behaviour(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self.manager(tmp, enforcement=False, clock=lambda: NOW).status()
        self.assertTrue(st.enabled)
        self.assertIn("development", st.detail)

    def test_unexpected_verifier_failure_locks_instead_of_crashing_or_opening(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self.manager(tmp, make_license(self.private), clock=lambda: NOW)
            self.assertTrue(m.status().enabled)
            with mock.patch.object(offline, "verify_license_text", side_effect=RuntimeError("boom")):
                st = m.status()
            self.assertFalse(st.enabled)
            self.assertEqual(st.state, LicenseState.TAMPERED)
            self.assertNotIn("boom", st.detail)

    def test_pathological_inputs_never_raise_out_of_the_verifier(self):
        for text in ("[" * 8000, "{" * 4000, '{"payload": ' * 600, "9" * 7000, '"' + "\\u0000" * 1000 + '"', "\x00\x01"):
            self.refused(text, LicenseState.TAMPERED)

    def test_activate_is_not_possible_in_offline_mode(self):
        from app.licensing.client import LicenseRejected
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(LicenseRejected) as ctx:
            asyncio.run(self.manager(tmp).activate("TAP-AAAAA-BBBBB-CCCCC-DDDDD"))
        self.assertEqual(ctx.exception.code, "offline_mode")

    def test_gate_blocks_without_license_and_opens_with_one(self):
        async def call(manager, path):
            sent = []
            async def app(scope, receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": []})
            async def send(msg):
                sent.append(msg)
            await LicenseGateMiddleware(app, manager)({"type": "http", "path": path, "headers": []}, None, send)
            return sent[0]["status"], dict(sent[0]["headers"]).get(b"location")
        with tempfile.TemporaryDirectory() as tmp:
            blocked = self.manager(tmp, clock=lambda: NOW)
            self.assertEqual(asyncio.run(call(blocked, "/posts")), (303, b"/activate"))
            self.assertEqual(asyncio.run(call(blocked, "/health/ready"))[0], 200)       # health checks stay reachable
            allowed = self.manager(tmp, make_license(self.private), clock=lambda: NOW)
            self.assertEqual(asyncio.run(call(allowed, "/posts"))[0], 200)


def prod_env(**extra):
    env = {"APP_ENV": "production", "APP_URL": f"https://{HOST}", "APP_SECRET": "s" * 40, "SESSION_ENCRYPTION_KEY": generate_key(),
           "DATABASE_URL": "postgresql://u:p@db.example.com/poster"}
    env.update(extra)
    return env


class StartupTests(Base):
    def build(self, env, public):
        settings = settings_from_env(env)
        with mock.patch.object(constants, "LICENSE_PUBLIC_KEY", public), mock.patch.object(constants, "LICENSE_SERVER_URL", ""):
            return asyncio.run(build_license_manager(settings, None, Cipher(settings.session_encryption_key)))

    def test_production_without_a_public_key_refuses_to_start_with_a_clear_message(self):
        for key in ("", "short", "A" * 50):
            with self.assertRaises(LicenseSetupError) as ctx:
                self.build(prod_env(), key)
            msg = str(ctx.exception)
            self.assertIn("no valid license public key", msg)
            self.assertIn("keygen.py", msg)
            self.assertIn("docs/LICENSING.md", msg)

    def test_production_with_key_but_no_license_starts_locked_not_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self.build(prod_env(LICENSE_FILE=os.path.join(tmp, "license.json")), self.public)
        self.assertIsInstance(m, OfflineLicenseManager)
        self.assertFalse(m.status().enabled)
        self.assertEqual(m.status().state, LicenseState.NOT_ACTIVATED)

    def test_production_with_valid_license_is_active_via_file_or_inline(self):
        text = make_license(self.private, issued=int(__import__("time").time()) - 100)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "l.json")
            Path(path).write_text(text)
            self.assertTrue(self.build(prod_env(LICENSE_FILE=path), self.public).status().enabled)
        self.assertTrue(self.build(prod_env(LICENSE_FILE_CONTENT=base64.b64encode(text.encode()).decode()), self.public).status().enabled)

    def test_production_license_signed_by_a_different_key_does_not_activate(self):
        text = make_license(self.other_private, issued=int(__import__("time").time()) - 100)
        m = self.build(prod_env(LICENSE_FILE_CONTENT=text), self.public)
        self.assertFalse(m.status().enabled)
        self.assertEqual(m.status().state, LicenseState.TAMPERED)

    def test_enforcement_cannot_be_turned_off_in_production(self):
        with self.assertRaises(ConfigError) as ctx:
            settings_from_env(prod_env(LICENSE_ENFORCEMENT="false"))
        self.assertIn("LICENSE_ENFORCEMENT", str(ctx.exception))
        settings = settings_from_env(prod_env())
        object.__setattr__(settings, "license_enforcement", False)       # even if config validation were bypassed ...
        with self.assertRaises(LicenseSetupError), mock.patch.object(constants, "LICENSE_PUBLIC_KEY", self.public):
            asyncio.run(build_license_manager(settings, None, Cipher(settings.session_encryption_key)))   # ... the builder refuses too

    def test_development_mode_is_separate_and_explicit(self):
        env = prod_env(APP_ENV="development", APP_URL="http://localhost:8000", LICENSE_ENFORCEMENT="false")
        settings = settings_from_env(env)
        async def go():
            db = await migrated_db()
            with mock.patch.object(constants, "LICENSE_PUBLIC_KEY", ""):
                return await build_license_manager(settings, db, Cipher(settings.session_encryption_key))
        m = asyncio.run(go())
        self.assertTrue(m.status().enabled)
        self.assertIn("development", m.status().detail)
        # development without the explicit switch still enforces (no accidental bypass)
        with self.assertRaises(LicenseSetupError):
            self.build(prod_env(APP_ENV="development", APP_URL="http://localhost:8000"), "")

    def test_large_inline_license_is_refused_by_config(self):
        with self.assertRaises(ConfigError):
            settings_from_env(prod_env(LICENSE_FILE_CONTENT="x" * 20000))


class NoPrivateKeyInCustomerRuntimeTests(unittest.TestCase):
    def test_application_code_never_references_the_signing_side(self):
        root = Path(__file__).resolve().parent.parent / "app"
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^\s*(from|import)\s+license_server\b", path.name)   # the seller tools are never imported
            self.assertNotIn("Ed25519PrivateKey", text, str(path))
            self.assertNotIn("PrivateFormat", text, str(path))
            self.assertNotIn("private_bytes", text, str(path))

    def test_runtime_works_with_the_public_key_only(self):
        private, public = signing.generate_keypair()
        text = make_license(private)
        del private
        self.assertEqual(verify_license_text(text, public_key=public, product=PRODUCT, host=HOST, now=NOW).product, PRODUCT)

    def test_docker_image_and_context_exclude_the_seller_tools(self):
        root = Path(__file__).resolve().parent.parent
        self.assertNotIn("license_server", (root / "Dockerfile").read_text())
        self.assertIn("license_server", (root / ".dockerignore").read_text())

    def test_signer_and_verifier_stay_in_sync(self):
        self.assertEqual(signing.OFFLINE_TYPE, offline.LICENSE_TYPE_MARK)
        self.assertEqual(signing.OFFLINE_FORMAT_VERSION, offline.FORMAT_VERSION)
        from app.licensing.protocol import canonical_json
        sample = {"b": 1, "a": [1, "é"]}
        self.assertEqual(signing.canonical_json(sample), canonical_json(sample))


class SellerToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.key = self.dir / "private.key"
        self.private, self.public = signing.generate_keypair()
        self.key.write_text(self.private + "\n")
        os.chmod(self.key, 0o600)
        from license_server import issue_license, keygen  # noqa: F401
        self.issue = issue_license.main
        self.keygen = keygen.main

    def tearDown(self):
        self.tmp.cleanup()

    def run_issue(self, *args, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.issue(list(args), now=now)
            except SystemExit as exc:  # argparse errors
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def base(self, *extra, out=None):
        return ["--private-key-file", str(self.key), "--customer-ref", "order-77", "--license-type", "standard",
                "--out", str(out or self.dir / "l.json"), "--yes", *extra]

    def test_issued_perpetual_license_verifies_with_the_public_key(self):
        code, out, err = self.run_issue(*self.base("--perpetual", "--host", "Poster.Example.com", "--also-base64"))
        self.assertEqual(code, 0, err)
        text = (self.dir / "l.json").read_text()
        lic = verify_license_text(text, public_key=self.public, product=PRODUCT, host=HOST, now=NOW)
        self.assertEqual((lic.customer_ref, lic.perpetual, lic.host), ("order-77", True, HOST))
        self.assertEqual(base64.b64decode((self.dir / "l.json.b64").read_text().strip()).decode(), text)
        self.assertNotIn(self.private, out + err + text)                               # the key is never printed or embedded

    def test_issued_time_limited_license(self):
        code, _, err = self.run_issue(*self.base("--expires", "2027-12-31"))
        self.assertEqual(code, 0, err)
        lic = verify_license_text((self.dir / "l.json").read_text(), public_key=self.public, product=PRODUCT, host=HOST, now=NOW)
        self.assertFalse(lic.perpetual)
        self.assertEqual(lic.expires_at, 1830297599)  # 2027-12-31 23:59:59 UTC

    def test_validity_must_be_chosen_deliberately(self):
        self.assertNotEqual(self.run_issue(*self.base())[0], 0)                        # neither --perpetual nor --expires
        self.assertNotEqual(self.run_issue(*self.base("--perpetual", "--expires", "2027-12-31"))[0], 0)
        code, _, err = self.run_issue(*self.base("--expires", "2020-01-01"))
        self.assertEqual(code, 1)
        self.assertIn("past", err)
        self.assertFalse((self.dir / "l.json").exists())

    def test_key_file_must_be_private_and_outside_the_project(self):
        os.chmod(self.key, 0o644)
        code, _, err = self.run_issue(*self.base("--perpetual"))
        self.assertEqual(code, 1)
        self.assertIn("chmod 600", err)
        self.assertNotIn(self.private, err)
        inside = Path(__file__).resolve().parent.parent / "tests" / "_tmp_private.key"
        inside.write_text(self.private)
        try:
            os.chmod(inside, 0o600)
            args = self.base("--perpetual")
            args[1] = str(inside)
            code, _, err = self.run_issue(*args)
            self.assertEqual(code, 1)
            self.assertIn("outside", err)
        finally:
            inside.unlink()

    def test_output_inside_project_or_existing_file_is_refused(self):
        code, _, err = self.run_issue(*self.base("--perpetual", out=Path(__file__).resolve().parent.parent / "x.json"))
        self.assertEqual(code, 1)
        (self.dir / "l.json").write_text("existing")
        self.assertEqual(self.run_issue(*self.base("--perpetual"))[0], 1)
        self.assertEqual((self.dir / "l.json").read_text(), "existing")

    def test_garbage_key_file_gives_a_generic_error(self):
        self.key.write_text("SECRET-LOOKING-GARBAGE-123\n")
        code, out, err = self.run_issue(*self.base("--perpetual"))
        self.assertEqual(code, 1)
        self.assertNotIn("SECRET-LOOKING-GARBAGE", out + err)

    def test_confirmation_is_required_without_yes(self):
        args = [a for a in self.base("--perpetual") if a != "--yes"]
        with mock.patch("builtins.input", return_value="no"):
            code, _, err = self.run_issue(*args)
        self.assertEqual(code, 1)
        self.assertFalse((self.dir / "l.json").exists())

    def test_keygen_creates_owner_only_key_outside_project_and_refuses_inside(self):
        target = self.dir / "new" / "k.key"
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sys.argv", ["keygen", "--private-out", str(target)]):
            self.assertEqual(self.keygen(), 0)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertNotIn(target.read_text().strip(), out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), mock.patch("sys.argv", ["keygen", "--private-out", str(target)]):
            self.assertEqual(self.keygen(), 1)                                         # never overwrites
        inside = Path(__file__).resolve().parent.parent / "private_test.key"
        with contextlib.redirect_stderr(io.StringIO()), mock.patch("sys.argv", ["keygen", "--private-out", str(inside)]):
            self.assertEqual(self.keygen(), 1)
        self.assertFalse(inside.exists())


if __name__ == "__main__":
    unittest.main()
