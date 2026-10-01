"""Static checks of the deployment files (they cannot start Docker, but they catch regressions)."""

import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


@unittest.skipUnless(yaml, "PyYAML not installed")
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.ci = load(".github/workflows/ci.yml")
        # PyYAML parses the key `on` as boolean True
        self.job = self.ci["jobs"]["test"]
        self.text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    def test_real_postgres_service_is_provisioned(self):
        pg = self.job["services"]["postgres"]
        self.assertTrue(pg["image"].startswith("postgres:"))
        self.assertIn("5432:5432", pg["ports"])
        self.assertIn("--health-cmd", pg["options"])

    def test_integration_tests_cannot_be_skipped_in_ci(self):
        env = self.job["env"]
        self.assertTrue(env["TEST_DATABASE_URL"].startswith("postgresql://"))
        for flag in ("REQUIRE_POSTGRES_TESTS", "REQUIRE_FASTAPI_TESTS", "REQUIRE_VENDORED_HTMX"):
            self.assertEqual(env[flag], "1")
        self.assertIn('grep -E "skipped" test.log', self.text)
        self.assertIn("shell: bash", self.text)

    def test_ci_installs_real_dependencies_and_runs_cli_migrations_twice(self):
        self.assertIn("pip install -r requirements.txt -r requirements-dev.txt", self.text)
        for needle in ("FOR UPDATE SKIP LOCKED",):
            self.assertIn(needle, (ROOT / "tests/test_postgres_integration.py").read_text(encoding="utf-8"))
        self.assertIn("DROP SCHEMA public CASCADE", self.text)
        self.assertEqual(self.text.count("python -m app.db.migrate"), 2)
        self.assertIn("uvicorn --factory app.web.main:create_app", self.text)

    def test_credentials_are_ci_only(self):
        self.assertIn("ci-only", self.text)
        self.assertNotRegex(self.text, r"(?i)secrets\.")  # no repository secrets are needed or used

    def test_final_gate_fails_unless_every_job_really_succeeded(self):
        gate = self.ci["jobs"]["verify-all"]
        self.assertEqual(gate["needs"], ["test", "docker"])
        self.assertEqual(gate["if"], "always()")  # runs even when a needed job failed or was skipped
        self.assertIn('"${{ needs.docker.result }}" = "success"', self.text)
        self.assertIn('"${{ needs.test.result }}" = "success"', self.text)

    def test_docker_job_builds_starts_and_checks_health(self):
        text = self.text
        for needle in ("docker compose build", "docker compose up -d", "scripts/vendor_htmx.py --verify",
                       "Health.Status", "docker compose down -v", "curl -fsS localhost:8000/health/ready",
                       "postgres:16"):
            self.assertIn(needle, text)


@unittest.skipUnless(yaml, "PyYAML not installed")
class ComposeAndDockerfileTests(unittest.TestCase):
    def setUp(self):
        self.compose = load("docker-compose.yml")
        self.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.services = self.compose["services"]

    def test_services_and_startup_order(self):
        self.assertEqual(set(self.services), {"db", "migrate", "web", "worker"})
        for name in ("web", "worker"):
            deps = self.services[name]["depends_on"]
            self.assertEqual(deps["migrate"]["condition"], "service_completed_successfully")
            self.assertEqual(deps["db"]["condition"], "service_healthy")
        self.assertEqual(self.services["migrate"]["command"], ["python", "-m", "app.db.migrate"])
        self.assertEqual(self.services["migrate"]["restart"], "no")

    def test_environment_is_passed_without_baking_secrets(self):
        for name in ("migrate", "web", "worker"):
            svc = self.services[name]
            self.assertEqual(svc["env_file"], ".env")
            self.assertTrue(svc["environment"]["DATABASE_URL"].startswith("postgresql://poster:${POSTGRES_PASSWORD:?"))
            self.assertEqual(svc["environment"]["AUTO_MIGRATE"], "false")
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for line in text.splitlines():
            if re.search(r"(?i)(password|secret|token)", line) and ":" in line and not line.lstrip().startswith("#"):
                value = line.split(":", 1)[1].strip()
                self.assertTrue(value == "" or value.startswith(("${", "postgresql://poster:${", "[", "pg_")) or "POSTGRES_PASSWORD" in value or "pg_isready" in value, line)
        self.assertNotIn("ARG ", self.dockerfile)
        self.assertNotRegex(self.dockerfile, r"(?i)(secret|password|api_hash|token)\s*=")

    def test_health_checks_are_meaningful(self):
        web = " ".join(self.services["web"]["healthcheck"]["test"])
        self.assertIn("/health/ready", web)  # checks the database, not just the process
        worker = " ".join(self.services["worker"]["healthcheck"]["test"])
        self.assertIn("/tmp/worker.heartbeat", worker)
        self.assertIn("pg_isready", " ".join(self.services["db"]["healthcheck"]["test"]))

    def test_db_is_not_published_to_the_host(self):
        self.assertNotIn("ports", self.services["db"])

    def test_dockerfile_runs_the_real_fastapi_app_as_non_root_with_vendored_htmx(self):
        d = self.dockerfile
        self.assertIn('"uvicorn", "--factory", "app.web.main:create_app"', d)
        self.assertIn("USER poster", d)
        self.assertIn("scripts/vendor_htmx.py --verify", d)
        self.assertNotIn("unpkg", d)
        self.assertLess(d.index("vendor_htmx.py"), d.index("USER poster"))

    def test_dockerignore_keeps_secrets_and_seller_material_out_of_the_image(self):
        lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        for entry in (".env", ".env.*", "license_server", "tests", "data"):
            self.assertIn(entry, lines)

    def test_worker_writes_the_heartbeat_the_healthcheck_reads(self):
        import tempfile

        from app.workers.main import touch_heartbeat

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "hb")
            touch_heartbeat(path)
            self.assertTrue(Path(path).exists())
        touch_heartbeat("/nonexistent-dir/hb")  # must not raise

    def test_production_dependencies_are_declared(self):
        req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        for package in ("fastapi", "uvicorn", "asyncpg", "cryptography", "jinja2", "telethon"):
            self.assertRegex(req, rf"(?m)^{package}", package)


class EnvExampleTests(unittest.TestCase):
    def test_env_example_has_no_values_for_secrets(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        for key in ("APP_SECRET", "SESSION_ENCRYPTION_KEY", "POSTGRES_PASSWORD", "TELEGRAM_API_HASH", "SETUP_TOKEN", "DATABASE_URL"):
            self.assertRegex(text, rf"(?m)^{key}=$", key)


if __name__ == "__main__":
    unittest.main()
