"""Static + behavioural checks of the temporary Render deployment (cannot contact Render from here)."""

import base64
import os
import re
import unittest
from pathlib import Path

from app.config import ConfigError, normalize_database_url, settings_from_env
from app.security.crypto import Cipher, CryptoError

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "render.yaml"
DOC = ROOT / "docs" / "STEP3_REAL_TELEGRAM_VERIFICATION_RENDER.md"
RENDER_URL = "https://telegram-auto-poster-test.onrender.com"
RENDER_DB_URL = "postgres://poster:Zx9fakeTestOnlyPw@dpg-abc123def456-a/poster"  # shape of Render's internal URL


def generated_value() -> str:
    """What Render's generateValue documents: a base64-encoded 256-bit value (standard alphabet, padded)."""
    return base64.b64encode(os.urandom(32)).decode()


@unittest.skipUnless(yaml, "PyYAML not installed")
class BlueprintStructureTests(unittest.TestCase):
    EXISTING_DB = "poster-test-db"
    REGION = "oregon"

    def setUp(self):
        self.bp = yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))
        (self.web,) = self.bp["services"]
        self.env = {e["key"]: e for e in self.web["envVars"]}

    def test_exactly_one_free_docker_web_service(self):
        self.assertEqual(set(self.bp), {"services"})
        self.assertEqual((self.web["type"], self.web["runtime"], self.web["plan"]), ("web", "docker", "free"))
        self.assertEqual(self.web["dockerfilePath"], "./Dockerfile")
        self.assertTrue((ROOT / "Dockerfile").exists())

    def test_the_blueprint_can_never_create_or_modify_a_database(self):
        # A `databases:` entry named like the hand-made database would make Render try to manage it (and clash with its
        # PostgreSQL version / region); any other name would create a second, duplicate database.
        self.assertNotIn("databases", self.bp)
        self.assertNotIn("envVarGroups", self.bp)

        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for item in node:
                    yield from keys(item)

        for forbidden in ("databases", "postgresMajorVersion", "ipAllowList", "databaseName", "diskSizeGB"):
            self.assertNotIn(forbidden, set(keys(self.bp)), forbidden)  # database settings belong to the existing database

    def test_no_worker_cron_or_other_paid_resource(self):
        self.assertEqual([s["type"] for s in self.bp["services"]], ["web"])
        for key in ("disk", "numInstances", "scaling", "preDeployCommand"):
            self.assertNotIn(key, self.web)  # none of these exist on the free plan

    def test_region_is_oregon_like_the_existing_database(self):
        self.assertEqual(self.web["region"], self.REGION)
        doc = DOC.read_text(encoding="utf-8")
        self.assertIn("Oregon", doc)  # the database's region is documented as a manual precondition
        self.assertIn(self.EXISTING_DB, doc)

    def test_database_url_is_wired_from_the_internal_connection_string(self):
        wiring = self.env["DATABASE_URL"]
        self.assertEqual(wiring["fromDatabase"], {"name": self.EXISTING_DB, "property": "connectionString"})  # the hand-made DB
        self.assertEqual(list(wiring), ["key", "fromDatabase"])
        self.assertNotIn("value", wiring)

    def test_secrets_are_generated_by_render_never_written_in_the_file(self):
        for key in ("APP_SECRET", "SESSION_ENCRYPTION_KEY", "SETUP_TOKEN"):
            self.assertIs(self.env[key]["generateValue"], True, key)
            self.assertNotIn("value", self.env[key], key)

    def test_every_other_variable_is_a_short_non_secret_literal(self):
        wired = {"DATABASE_URL", "APP_SECRET", "SESSION_ENCRYPTION_KEY", "SETUP_TOKEN"}
        literals = {k: e["value"] for k, e in self.env.items() if k not in wired}
        self.assertEqual(literals, {"APP_ENV": "development", "LICENSE_ENFORCEMENT": "false", "PORT": "8000",
                                    "AUTO_MIGRATE": "true", "LOG_LEVEL": "INFO"})

    def test_no_app_url_telegram_or_secret_like_names_are_configured(self):
        self.assertNotIn("APP_URL", self.env)  # comes from RENDER_EXTERNAL_URL
        self.assertFalse([k for k in self.env if k.startswith("TELEGRAM")])
        self.assertNotIn("TRUST_PROXY_HEADERS", self.env)  # Render's proxy-header behaviour was not verified

    def test_port_matches_the_dockerfile_and_health_check_exists_in_the_app(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("EXPOSE " + self.env["PORT"]["value"], dockerfile)
        self.assertIn('"--port", "' + self.env["PORT"]["value"] + '"', dockerfile)
        self.assertIn('@app.get("/health/ready")', (ROOT / "app" / "web" / "main.py").read_text(encoding="utf-8"))
        self.assertEqual(self.web["healthCheckPath"], "/health/ready")

    def test_deploys_are_manual_so_a_push_cannot_redeploy_a_test_environment_unnoticed(self):
        self.assertIs(self.web["autoDeploy"], False)


@unittest.skipUnless(yaml, "PyYAML not installed")
class RenderRuntimeConfigurationTests(unittest.TestCase):
    """Feed the settings loader exactly what Render would provide at run time."""

    def render_env(self, **override):
        bp = yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))
        env = {e["key"]: e["value"] for e in bp["services"][0]["envVars"] if "value" in e}
        env.update(DATABASE_URL=RENDER_DB_URL, APP_SECRET=generated_value(), SESSION_ENCRYPTION_KEY=generated_value(),
                   SETUP_TOKEN=generated_value(), RENDER_EXTERNAL_URL=RENDER_URL, RENDER="true")
        env.update(override)
        return env

    def test_the_blueprint_environment_produces_a_valid_https_configuration(self):
        settings = settings_from_env(self.render_env())
        self.assertEqual(settings.app_url, RENDER_URL)
        self.assertEqual(settings.app_host, "telegram-auto-poster-test.onrender.com")
        self.assertTrue(settings.cookie_secure)  # '__Host-' Secure cookies
        self.assertFalse(settings.license_enforcement)
        self.assertTrue(settings.auto_migrate)
        self.assertFalse(settings.telegram_configured)
        self.assertFalse(settings.trust_proxy_headers)
        self.assertTrue(settings.database_url.startswith("postgresql://poster:"))
        self.assertNotIn("Zx9fakeTestOnlyPw", repr(settings))

    def test_render_internal_database_url_is_accepted_and_normalised(self):
        self.assertEqual(normalize_database_url(RENDER_DB_URL), "postgresql://poster:Zx9fakeTestOnlyPw@dpg-abc123def456-a/poster")

    def test_production_mode_still_requires_https_for_the_platform_url(self):
        ok = settings_from_env(self.render_env(APP_ENV="production", LICENSE_ENFORCEMENT="true"))
        self.assertTrue(ok.is_production and ok.cookie_secure)
        with self.assertRaises(ConfigError) as ctx:
            settings_from_env(self.render_env(APP_ENV="production", LICENSE_ENFORCEMENT="true",
                                              RENDER_EXTERNAL_URL="http://x.onrender.com"))
        self.assertIn("https://", str(ctx.exception))

    def test_without_any_url_production_is_refused_exactly_as_before(self):
        env = self.render_env(APP_ENV="production", LICENSE_ENFORCEMENT="true")
        del env["RENDER_EXTERNAL_URL"]
        with self.assertRaises(ConfigError) as ctx:
            settings_from_env(env)
        self.assertIn("APP_URL must start with https://", str(ctx.exception))

    def test_explicit_app_url_always_wins_over_the_platform_url(self):
        settings = settings_from_env(self.render_env(APP_URL="https://poster.example.com"))
        self.assertEqual(settings.app_url, "https://poster.example.com")
        with self.assertRaises(ConfigError):  # and is validated on its own: a bad explicit value is not rescued
            settings_from_env(self.render_env(APP_ENV="production", LICENSE_ENFORCEMENT="true", APP_URL="http://bad.example.com"))

    def test_production_validation_was_not_weakened(self):
        for change, needle in [({"LICENSE_ENFORCEMENT": "false"}, "LICENSE_ENFORCEMENT"),
                               ({"APP_SECRET": "short"}, "32"), ({"SESSION_ENCRYPTION_KEY": "nope"}, "Fernet"),
                               ({"DATABASE_URL": ""}, "DATABASE_URL")]:
            with self.assertRaises(ConfigError, msg=change) as ctx:
                settings_from_env(self.render_env(APP_ENV="production", **{"LICENSE_ENFORCEMENT": "true", **change}))
            self.assertIn(needle, str(ctx.exception))

    def test_the_original_render_failure_messages_are_reproduced_without_the_blueprint(self):
        with self.assertRaises(ConfigError) as ctx:
            settings_from_env({"APP_ENV": "production"})
        text = str(ctx.exception)
        for needle in ("APP_URL must start with https://", "APP_SECRET is not set", "DATABASE_URL is not set",
                       "SESSION_ENCRYPTION_KEY is not set"):
            self.assertIn(needle, text)


class GeneratedKeyCompatibilityTests(unittest.TestCase):
    def test_render_style_256_bit_values_work_as_the_encryption_key(self):
        seen_url_unsafe = False
        for _ in range(200):
            key = generated_value()
            seen_url_unsafe |= ("+" in key or "/" in key)
            cipher = Cipher(key)
            self.assertEqual(cipher.decrypt(cipher.encrypt("session", "ctx"), "ctx"), "session")
        self.assertTrue(seen_url_unsafe)  # the '+' and '/' alphabet was really exercised

    def test_the_same_key_with_or_without_padding_is_the_same_key(self):
        raw = os.urandom(32)
        padded = base64.b64encode(raw).decode()
        for variant in (padded, padded.rstrip("="), base64.urlsafe_b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("=")):
            self.assertEqual(Cipher(variant).decrypt(Cipher(padded).encrypt("x", "c"), "c"), "x")

    def test_too_short_or_garbage_keys_are_still_rejected(self):
        # 16-byte base64 (24 chars) is too short to be a secure secret; the rest is not a key at all.
        for bad in (base64.b64encode(os.urandom(16)).decode(), "not base64 at all!", "A", "====", ""):
            with self.assertRaises(CryptoError, msg=bad):
                Cipher(bad)

    def test_other_length_high_entropy_values_are_now_derived_instead_of_rejected(self):
        # 31/33-byte values are not Fernet keys; as arbitrary high-entropy secrets they are accepted via HKDF.
        for size in (31, 33, 48):
            secret = base64.b64encode(os.urandom(size)).decode()
            cipher = Cipher(secret)
            self.assertEqual(cipher.decrypt(cipher.encrypt("x", "c"), "c"), "x")


@unittest.skipUnless(yaml, "PyYAML not installed")
class MigrationAndSecretsOnRenderTests(unittest.TestCase):
    def test_migrations_run_at_start_up_with_locking_and_checksums_intact(self):
        main = (ROOT / "app" / "web" / "main.py").read_text(encoding="utf-8")
        self.assertRegex(main, r"if settings\.auto_migrate:\s+await ensure_schema\(settings\.database_url\)")
        migrate = (ROOT / "app" / "db" / "migrate.py").read_text(encoding="utf-8")
        self.assertIn("pg_advisory_lock", migrate)
        self.assertIn("was modified after it was applied", migrate)
        self.assertEqual(settings_from_env({"APP_ENV": "development", "APP_SECRET": "x" * 20, "DATABASE_URL": RENDER_DB_URL,
                                            "SESSION_ENCRYPTION_KEY": generated_value()}).auto_migrate, True)

    def test_blueprint_and_docs_contain_no_secret_values(self):
        for path in (BLUEPRINT, DOC):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"[A-Za-z0-9+/]{43}=")  # no base64 256-bit value
            self.assertNotRegex(text, r"[0-9a-f]{32}")
            self.assertNotRegex(text, r"postgres(ql)?://[^\s]*:[^\s@]+@")  # no connection string with a password
            self.assertNotRegex(text, r"gAAAA[A-Za-z0-9_-]{20,}")

    def test_the_documentation_covers_the_procedure_and_is_honest(self):
        text = DOC.read_text(encoding="utf-8")
        for needle in ("NEVER share secrets", "This has not been run on Render yet", "Blueprint", "SETUP_TOKEN", "Environment",
                       "Delete Web Service", "Delete Database", "15 minutes", "30 days", "Access Control", "RENDER_EXTERNAL_URL",
                       "APP_ENV=development", "Logs", "STEP3_REAL_TELEGRAM_VERIFICATION.md", "poster-test-db", "Oregon",
                       "fromDatabase", "Internal Database URL", "PostgreSQL 18", "render blueprints validate",
                       "HKDF-SHA256", "Do not change or regenerate"):
            self.assertIn(needle, text, needle)
        self.assertNotRegex(text, r"(?i)(send|paste|give|share) (it |them |this )?(to |with )?(me|the developer|claude)")

    def test_env_example_and_gitignore_still_protect_secrets(self):
        self.assertIn(".env", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
        self.assertRegex((ROOT / ".env.example").read_text(encoding="utf-8"), r"(?m)^APP_SECRET=$")


if __name__ == "__main__":
    unittest.main()
