"""SESSION_ENCRYPTION_KEY: a Fernet key is used directly; any other high-entropy secret is derived with HKDF-SHA256."""

import base64
import hashlib
import io
import logging
import os
import subprocess
import sys
import unittest
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import ConfigError, settings_from_env
from app.security import crypto
from app.security.crypto import Cipher, CryptoError, DecryptionError, generate_key, parse_keys
from app.security.redact import RedactingFormatter

ROOT = Path(__file__).resolve().parents[1]
RENDER_STYLE = base64.b64encode(os.urandom(48)).decode()  # an arbitrary 384-bit value: not a Fernet key
VECTOR_SECRET = "render-test-vector-0123456789-ABCDEFGHIJKLMNOP"  # public dummy value, not a real secret
VECTOR_FINGERPRINT = "eabbe8add6ba8fbe2ef0bae75ad9b44cb2f111eb38ae87acb9f535ac778ad407"  # sha256 of its derived key
BASE_ENV = {"APP_ENV": "development", "APP_URL": "http://localhost:8000", "APP_SECRET": "dev-secret-value-16chars",
            "DATABASE_URL": "postgresql://u:p@db/x"}


def independent_derivation(secret: str) -> str:
    """Re-implementation straight from the documented parameters (guards against accidental changes)."""
    okm = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"telegram-auto-poster/session-encryption/salt/v1",
               info=b"telegram-auto-poster/session-encryption/fernet-key/v1").derive(secret.encode())
    return base64.urlsafe_b64encode(okm).decode()


class FernetKeysKeepWorkingTests(unittest.TestCase):
    def test_valid_fernet_key_is_used_directly_and_unchanged(self):
        key = generate_key()
        self.assertEqual(parse_keys(key), [key])
        cipher = Cipher(key)
        self.assertEqual(Fernet(key.encode()).decrypt(cipher.encrypt("x").encode()) != b"", True)  # same key material

    def test_ciphertext_made_with_a_plain_fernet_key_before_this_change_still_decrypts(self):
        key = generate_key()
        token = Fernet(key.encode()).encrypt(b"v1\x00ctx\x00legacy value").decode()  # envelope format of earlier releases
        self.assertEqual(Cipher(key).decrypt(token, "ctx"), "legacy value")

    def test_unpadded_fernet_key_still_works_and_is_the_same_key(self):
        key = generate_key()
        self.assertEqual(parse_keys(key.rstrip("=")), [key])

    def test_rotation_with_mixed_entries_fernet_first_derived_second(self):
        old_fernet, new_secret = generate_key(), RENDER_STYLE
        token = Cipher(old_fernet).encrypt("data", "ctx")
        rotating = Cipher(f"{new_secret},{old_fernet}")  # new secret encrypts; the old Fernet key still decrypts
        self.assertEqual(rotating.decrypt(token, "ctx"), "data")
        moved = rotating.rotate(token)
        self.assertEqual(Cipher(new_secret).decrypt(moved, "ctx"), "data")
        with self.assertRaises(DecryptionError):
            Cipher(old_fernet).decrypt(moved, "ctx")


class DerivedKeyTests(unittest.TestCase):
    def test_render_style_secret_is_not_a_fernet_key_but_is_accepted(self):
        with self.assertRaises(ValueError):
            Fernet(RENDER_STYLE.encode())  # proves it really is a non-Fernet value
        cipher = Cipher(RENDER_STYLE)
        self.assertEqual(cipher.decrypt(cipher.encrypt("session", "telegram_session:u:a"), "telegram_session:u:a"), "session")

    def test_other_high_entropy_formats_are_accepted(self):
        for secret in (os.urandom(32).hex(), base64.b32encode(os.urandom(32)).decode(), base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("="),
                       os.urandom(24).hex(), "".join(chr(33 + b % 90) for b in os.urandom(40)).replace(",", "x")):
            cipher = Cipher(secret)
            self.assertEqual(cipher.decrypt(cipher.encrypt("v", "c"), "c"), "v", secret[:6])

    def test_derivation_is_deterministic_and_matches_the_documented_construction(self):
        first, second = parse_keys(RENDER_STYLE), parse_keys(RENDER_STYLE)
        self.assertEqual(first, second)
        self.assertEqual(first, [independent_derivation(RENDER_STYLE)])
        self.assertTrue(crypto._is_fernet_key(first[0]))  # the derived value is a valid Fernet key

    def test_the_construction_is_frozen_by_a_known_answer(self):
        derived = parse_keys(VECTOR_SECRET)[0]
        self.assertEqual(hashlib.sha256(derived.encode()).hexdigest(), VECTOR_FINGERPRINT)

    def test_different_secrets_give_different_keys_and_cannot_decrypt_each_other(self):
        a, b = os.urandom(32).hex(), os.urandom(32).hex()
        self.assertNotEqual(parse_keys(a), parse_keys(b))
        token = Cipher(a).encrypt("x", "c")
        with self.assertRaises(DecryptionError):
            Cipher(b).decrypt(token, "c")
        one_char = RENDER_STYLE[:-1] + ("A" if RENDER_STYLE[-1] != "A" else "B")
        self.assertNotEqual(parse_keys(RENDER_STYLE), parse_keys(one_char))

    def test_the_derived_key_is_not_the_secret_or_a_trivial_encoding_of_it(self):
        derived = parse_keys(RENDER_STYLE)[0]
        self.assertNotEqual(derived, RENDER_STYLE)
        for encoding in (base64.urlsafe_b64encode(RENDER_STYLE.encode()[:32]).decode(), RENDER_STYLE[:44]):
            self.assertNotEqual(derived, encoding)

    def test_same_secret_gives_the_same_key_in_separate_processes(self):
        code = ("import sys, hashlib; from app.security.crypto import parse_keys; "
                "print(hashlib.sha256(parse_keys(sys.argv[1])[0].encode()).hexdigest())")
        outputs = {subprocess.run([sys.executable, "-c", code, RENDER_STYLE], capture_output=True, text=True, cwd=ROOT,
                                  env={**os.environ, "PYTHONPATH": str(ROOT)}).stdout.strip() for _ in range(2)}
        self.assertEqual(len(outputs), 1)
        in_process = hashlib.sha256(parse_keys(RENDER_STYLE)[0].encode()).hexdigest()
        self.assertEqual(outputs, {in_process})

    def test_data_encrypted_in_one_process_decrypts_in_a_fresh_one(self):
        token = Cipher(RENDER_STYLE).encrypt("persistent session", "telegram_session:u:a")
        code = ("import sys; from app.security.crypto import Cipher; "
                "print(Cipher(sys.argv[1]).decrypt(sys.argv[2], 'telegram_session:u:a'))")
        out = subprocess.run([sys.executable, "-c", code, RENDER_STYLE, token], capture_output=True, text=True, cwd=ROOT,
                             env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.assertEqual(out.stdout.strip(), "persistent session")

    def test_context_binding_and_isolation_are_unchanged_with_a_derived_key(self):
        cipher = Cipher(RENDER_STYLE)
        token = cipher.encrypt("session", "telegram_session:user-a:acc-1")
        for wrong in ("telegram_session:user-b:acc-1", "telegram_session:user-a:acc-2", ""):
            with self.assertRaises(DecryptionError):
                cipher.decrypt(token, wrong)
        self.assertNotEqual(cipher.encrypt("session", "c"), cipher.encrypt("session", "c"))  # randomised
        self.assertNotIn("session", token)

    def test_nothing_derived_is_stored_in_module_state(self):
        before = dict(vars(crypto))
        Cipher(RENDER_STYLE)
        after = vars(crypto)
        self.assertEqual({k for k in after if k not in before}, set())
        derived = parse_keys(RENDER_STYLE)[0]
        for name, value in after.items():
            self.assertNotEqual(value, derived, name)


class WeakOrMalformedSecretTests(unittest.TestCase):
    def test_weak_values_are_rejected(self):
        for weak in ("", "   ", "abc", "hunter2", "x" * 40, "ab" * 20, "changeme" + "Z9y8X7w6" * 4, "replace-me-" + "q" * 30,
                     "secret-" + os.urandom(20).hex(), "a1" * 15, base64.b64encode(os.urandom(16)).decode()):
            with self.assertRaises(CryptoError, msg=weak[:10]):
                Cipher(weak)

    def test_boundary_length(self):
        ok = "".join(chr(65 + i % 26) + str(i % 7) for i in range(16))[:32]  # 32 chars, many distinct
        Cipher(ok)
        with self.assertRaises(CryptoError):
            Cipher(ok[:31])

    def test_values_with_commas_or_spaces_are_split_and_each_part_must_be_valid(self):
        good = os.urandom(32).hex()
        self.assertEqual(len(parse_keys(f"{good},{generate_key()}")), 2)
        with self.assertRaises(CryptoError):
            parse_keys(good + ",tooshort")

    def test_non_ascii_secret_is_handled_without_crashing(self):
        secret = "বাংলা-গোপন-কী-" + os.urandom(16).hex()
        cipher = Cipher(secret)
        self.assertEqual(cipher.decrypt(cipher.encrypt("x", "c"), "c"), "x")


class NoLeakageTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        handler = logging.StreamHandler(self.stream)
        handler.setFormatter(RedactingFormatter("%(name)s %(levelname)s %(message)s"))
        self.root = logging.getLogger()
        self.root.addHandler(handler)
        self.old = self.root.level
        self.root.setLevel(logging.DEBUG)
        self.addCleanup(self.root.removeHandler, handler)
        self.addCleanup(self.root.setLevel, self.old)

    def test_errors_never_contain_the_secret_or_a_derived_key(self):
        for secret in ("weakvalue", "x" * 40, "change-me-" + "k9" * 20):
            for action in (lambda s=secret: Cipher(s), lambda s=secret: parse_keys(s),
                           lambda s=secret: settings_from_env({**BASE_ENV, "SESSION_ENCRYPTION_KEY": s})):
                with self.assertRaises((CryptoError, ConfigError)) as ctx:
                    action()
                text = str(ctx.exception) + repr(ctx.exception)
                self.assertNotIn(secret, text)
                self.assertNotIn(secret[:12], text)

    def test_nothing_secret_is_logged_or_shown_by_successful_use(self):
        secret = RENDER_STYLE
        settings = settings_from_env({**BASE_ENV, "SESSION_ENCRYPTION_KEY": secret})
        cipher = Cipher(secret)
        token = cipher.encrypt("session-material", "ctx")
        cipher.decrypt(token, "ctx")
        with self.assertRaises(DecryptionError) as ctx:
            cipher.decrypt(token, "other")
        derived = parse_keys(secret)[0]
        visible = " ".join([self.stream.getvalue(), repr(settings), repr(cipher), str(ctx.exception), repr(ctx.exception)])
        for hidden in (secret, secret[:16], derived, derived[:16], "session-material"):
            self.assertNotIn(hidden, visible)

    def test_config_error_for_a_bad_key_is_helpful_but_value_free(self):
        with self.assertRaises(ConfigError) as ctx:
            settings_from_env({**BASE_ENV, "SESSION_ENCRYPTION_KEY": "bad-value-123"})
        text = str(ctx.exception)
        self.assertIn("Fernet", text)
        self.assertIn("32 characters", text)
        self.assertNotIn("bad-value-123", text)

    def test_crypto_module_has_no_logging_or_printing(self):
        source = (ROOT / "app" / "security" / "crypto.py").read_text(encoding="utf-8")
        for needle in ("logging", "print(", "log.", "logger"):
            self.assertNotIn(needle, source, needle)

    def test_the_derived_key_is_never_written_to_the_database_schema(self):
        migrations = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "migrations").glob("*.sql"))
        self.assertNotRegex(migrations, r"(?i)derived|session_encryption|encryption_key")


class DocumentationTests(unittest.TestCase):
    def test_documentation_explains_both_key_forms_without_showing_a_key(self):
        for rel in ("docs/STEP3_REAL_TELEGRAM_VERIFICATION_RENDER.md", ".env.example", "README.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("HKDF-SHA256", text, rel)
            self.assertIn("32 characters", text, rel)
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertRegex(env_example, r"(?m)^SESSION_ENCRYPTION_KEY=$")  # no value, ever
        self.assertIn("Fernet key", env_example)
        self.assertIn("Render", (ROOT / "docs" / "STEP3_REAL_TELEGRAM_VERIFICATION_RENDER.md").read_text(encoding="utf-8"))

    def test_the_generator_script_still_prints_real_fernet_keys(self):
        from scripts import generate_keys

        key = generate_keys.new_values()["SESSION_ENCRYPTION_KEY"]
        self.assertEqual(parse_keys(key), [key])


class ConfigAcceptsBothFormsTests(unittest.TestCase):
    def test_settings_validate_with_either_form(self):
        for value in (generate_key(), RENDER_STYLE, os.urandom(32).hex()):
            settings = settings_from_env({**BASE_ENV, "SESSION_ENCRYPTION_KEY": value})
            self.assertEqual(settings.session_encryption_key, value)  # kept verbatim; resolved only inside Cipher
            Cipher(settings.session_encryption_key)

    def test_production_rules_are_unchanged(self):
        prod = {**BASE_ENV, "APP_ENV": "production", "APP_URL": "https://poster.example.com", "APP_SECRET": "s" * 40,
                "SESSION_ENCRYPTION_KEY": RENDER_STYLE}
        self.assertTrue(settings_from_env(prod).is_production)
        for change, needle in ({"LICENSE_ENFORCEMENT": "false"}, "LICENSE_ENFORCEMENT"), ({"APP_URL": "http://x.example.com"}, "https://"), \
                              ({"APP_SECRET": "short"}, "32"), ({"SESSION_ENCRYPTION_KEY": "nope"}, "Fernet"):
            with self.assertRaises(ConfigError) as ctx:
                settings_from_env({**prod, **change})
            self.assertIn(needle, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
