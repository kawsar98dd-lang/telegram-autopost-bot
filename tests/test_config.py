import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from app.config import ConfigError, load_settings, normalize_database_url, parse_dotenv, settings_from_env
from app.security.crypto import generate_key

GOOD = {
    "APP_ENV": "production",
    "APP_URL": "https://poster.example.com",
    "APP_SECRET": "x" * 40,
    "DATABASE_URL": "postgresql://u:p@db:5432/app",
    "SESSION_ENCRYPTION_KEY": generate_key(),
}


class ConfigTests(unittest.TestCase):
    def test_valid_settings(self):
        s = settings_from_env(GOOD)
        self.assertTrue(s.is_production)
        self.assertEqual(s.app_host, "poster.example.com")
        self.assertTrue(s.cookie_secure)
        self.assertFalse(s.telegram_configured)
        self.assertEqual(s.max_posts_per_hour, 60)

    def test_repr_hides_secrets(self):
        s = settings_from_env({**GOOD, "TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "hashvalue"})
        text = repr(s)
        for secret in (GOOD["APP_SECRET"], GOOD["SESSION_ENCRYPTION_KEY"], "hashvalue", "u:p@db"):
            self.assertNotIn(secret, text)

    def test_all_problems_reported_together(self):
        with self.assertRaises(ConfigError) as ctx:
            settings_from_env({})
        text = str(ctx.exception)
        self.assertIn("APP_SECRET", text)
        self.assertIn("DATABASE_URL", text)
        self.assertIn("SESSION_ENCRYPTION_KEY", text)

    def test_production_rules(self):
        for change, needle in [
            ({"APP_URL": "http://poster.example.com"}, "https://"),
            ({"APP_SECRET": "short"}, "32"),
            ({"APP_SECRET": "change-me"}, "APP_SECRET"),
            ({"LICENSE_ENFORCEMENT": "false"}, "LICENSE_ENFORCEMENT"),
            ({"LICENSE_SERVER_URL_OVERRIDE": "http://x"}, "LICENSE_SERVER_URL_OVERRIDE"),
            ({"SESSION_ENCRYPTION_KEY": "not-a-key"}, "Fernet"),
            ({"TELEGRAM_API_ID": "abc"}, "TELEGRAM_API_ID"),
            ({"MAX_UPLOAD_MB": "0"}, "MAX_UPLOAD_MB"),
            ({"APP_URL": "https://x.com/path"}, "APP_URL"),
        ]:
            with self.subTest(change=change):
                with self.assertRaises(ConfigError) as ctx:
                    settings_from_env({**GOOD, **change})
                self.assertIn(needle, str(ctx.exception))

    def test_development_relaxations(self):
        s = settings_from_env({**GOOD, "APP_ENV": "development", "APP_URL": "http://localhost:8000",
                               "APP_SECRET": "dev-secret-value-16", "LICENSE_ENFORCEMENT": "false"})
        self.assertFalse(s.license_enforcement)
        self.assertFalse(s.cookie_secure)

    def test_database_url_normalisation(self):
        self.assertEqual(normalize_database_url("postgres://u:p@h/db"), "postgresql://u:p@h/db")
        self.assertEqual(normalize_database_url("postgresql+asyncpg://u:p@h/db"), "postgresql://u:p@h/db")
        self.assertEqual(
            normalize_database_url("postgresql://u:p@h:6543/db?pgbouncer=true&sslmode=require"),
            "postgresql://u:p@h:6543/db?sslmode=require",
        )
        for bad in ("sqlite:///x.db", "mysql://u@h/db", "postgresql:///db"):
            with self.assertRaises(ValueError):
                normalize_database_url(bad)

    def test_dotenv_parser(self):
        parsed = parse_dotenv('# c\nA=1\nB="two words"\nC=3 # inline\nexport D=\'q\'\nE=\n\nnoequals')
        self.assertEqual(parsed, {"A": "1", "B": "two words", "C": "3", "D": "q", "E": ""})

    def test_env_overrides_dotenv_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            lines = [f"{k}={v}" for k, v in GOOD.items()]
            path.write_text("\n".join(lines) + "\nLOG_LEVEL=DEBUG\n")
            with mock.patch.dict(os.environ, {"LOG_LEVEL": "ERROR"}, clear=True):
                self.assertEqual(load_settings(path).log_level, "ERROR")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_settings(path).log_level, "DEBUG")


if __name__ == "__main__":
    unittest.main()
