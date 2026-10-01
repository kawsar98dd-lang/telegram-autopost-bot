import io
import logging
import unittest

import tests.support  # noqa: F401
from app import branding
from app.security.crypto import Cipher, CryptoError, DecryptionError, generate_key, get_cipher, init_cipher
from app.security.passwords import hash_password, needs_rehash, verify_password
from app.security.redact import MASK, RedactingFormatter, redact


class CryptoTests(unittest.TestCase):
    def setUp(self):
        self.cipher = Cipher(generate_key())

    def test_roundtrip_and_unicode(self):
        for text in ("session-string", "বাংলা 🔥", ""):
            self.assertEqual(self.cipher.decrypt(self.cipher.encrypt(text, "c"), "c"), text)

    def test_ciphertext_is_randomised_and_hides_plaintext(self):
        a, b = self.cipher.encrypt("secret-session"), self.cipher.encrypt("secret-session")
        self.assertNotEqual(a, b)
        self.assertNotIn("secret-session", a)

    def test_wrong_key_fails(self):
        token = self.cipher.encrypt("x")
        with self.assertRaises(DecryptionError):
            Cipher(generate_key()).decrypt(token)

    def test_context_binding_isolates_users(self):
        token = self.cipher.encrypt("session", "telegram_session:user-a")
        with self.assertRaises(DecryptionError):
            self.cipher.decrypt(token, "telegram_session:user-b")

    def test_tampered_token_fails(self):
        token = self.cipher.encrypt("x")
        with self.assertRaises(DecryptionError):
            self.cipher.decrypt(token[:-4] + "AAAA")

    def test_key_rotation(self):
        old, new = generate_key(), generate_key()
        token = Cipher(old).encrypt("data", "ctx")
        rotating = Cipher(f"{new},{old}")
        self.assertEqual(rotating.decrypt(token, "ctx"), "data")
        moved = rotating.rotate(token)
        self.assertEqual(Cipher(new).decrypt(moved, "ctx"), "data")

    def test_invalid_keys(self):
        for bad in ("", "abc", "   "):
            with self.assertRaises(CryptoError):
                Cipher(bad)

    def test_global_cipher(self):
        init_cipher(generate_key())
        self.assertEqual(get_cipher().decrypt(get_cipher().encrypt("v")), "v")


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        stored = hash_password("correct horse")
        self.assertTrue(verify_password("correct horse", stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertNotEqual(stored, hash_password("correct horse"))  # salted
        self.assertNotIn("correct horse", stored)
        self.assertFalse(needs_rehash(stored))

    def test_garbage_hash_is_rejected_not_crashing(self):
        self.assertFalse(verify_password("x", "garbage"))
        self.assertTrue(needs_rehash("garbage"))
        with self.assertRaises(ValueError):
            hash_password("")


class RedactionTests(unittest.TestCase):
    def test_key_value_pairs(self):
        out = redact("login api_hash=0123abcd password: 'hunter2' token=abc.def license_key=x")
        for leaked in ("0123abcd", "hunter2", "abc.def"):
            self.assertNotIn(leaked, out)

    def test_fernet_license_and_long_blob(self):
        token = Cipher(generate_key()).encrypt("s")
        blob = "1" + "A" * 300
        out = redact(f"{token} TAP-ABCDE-FGHJK-MNPQR-STVWX {blob}")
        self.assertNotIn(token, out)
        self.assertNotIn("TAP-ABCDE", out)
        self.assertNotIn("AAAAAAAAAA", out)
        self.assertEqual(out.count(MASK), 3)

    def test_plain_text_untouched(self):
        self.assertEqual(redact("Posted 3 messages to group 42"), "Posted 3 messages to group 42")

    def test_formatter_covers_exceptions(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(message)s"))
        logger = logging.getLogger("redact-test")
        logger.handlers[:] = [handler]
        logger.propagate = False
        try:
            raise RuntimeError("failed with password=topsecret")
        except RuntimeError:
            logger.exception("boom")
        self.assertNotIn("topsecret", stream.getvalue())


class FooterTests(unittest.TestCase):
    def test_footer(self):
        footer = branding.render_footer()
        self.assertIn("@YourService", footer)
        self.assertTrue(footer.startswith(branding.FOOTER_SEPARATOR))
        self.assertNotIn("TAP-", footer)


if __name__ == "__main__":
    unittest.main()
