"""Static + logic tests for the temporary HTTPS test deployment (cannot start Docker/cloud servers here)."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.config import parse_dotenv, settings_from_env
from app.auth import csrf

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "cloud-init-https-test.sh"
DOC = ROOT / "docs" / "STEP3_REAL_TELEGRAM_VERIFICATION_ANDROID_CLOUD.md"
HAVE_BASH = shutil.which("bash") is not None


def sh(code: str, env: dict | None = None, cwd=None) -> subprocess.CompletedProcess:
    full = {**os.environ, **(env or {})}
    return subprocess.run(["bash", "-c", f'source "{SCRIPT}"\n{code}'], capture_output=True, text=True, env=full, cwd=cwd)


@unittest.skipUnless(yaml, "PyYAML not installed")
class ComposeOverrideTests(unittest.TestCase):
    def setUp(self):
        self.base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        self.https = yaml.safe_load((ROOT / "docker-compose.https.yml").read_text())

    def test_only_caddy_is_public_and_postgres_is_never_published(self):
        self.assertEqual(self.https["services"]["caddy"]["ports"], ["80:80", "443:443"])
        self.assertEqual(set(self.https["services"]), {"caddy"})
        self.assertNotIn("ports", self.base["services"]["db"])
        self.assertNotIn("ports", self.base["services"]["worker"])
        self.assertNotIn("ports", self.base["services"]["migrate"])

    def test_web_port_can_be_bound_to_loopback_and_the_default_is_unchanged(self):
        (port,) = self.base["services"]["web"]["ports"]
        self.assertTrue(port.startswith("${WEB_BIND_ADDR:-0.0.0.0}:${WEB_PORT:-8000}:8000"))

    def test_caddy_waits_for_a_healthy_web_service_and_needs_a_host_name(self):
        caddy = self.https["services"]["caddy"]
        self.assertEqual(caddy["depends_on"]["web"]["condition"], "service_healthy")
        self.assertIn("${APP_HOST:?", caddy["environment"]["APP_HOST"])
        self.assertTrue(any("Caddyfile" in v and v.endswith(":ro") for v in caddy["volumes"]))
        self.assertTrue(caddy["image"].startswith("caddy:"))

    def test_caddyfile_proxies_to_the_internal_web_service_only(self):
        text = (ROOT / "deploy" / "Caddyfile").read_text()
        self.assertIn("{$APP_HOST}", text)
        self.assertIn("reverse_proxy web:8000", text)
        self.assertNotRegex(text, r"(?i)tls\s+internal|http://")  # real certificates, never plain-http sites
        directives = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
        self.assertEqual(directives, ["{$APP_HOST} {", "reverse_proxy web:8000", "}"])  # no access log, no compression


def run_script(code: str, env: dict | None = None, extra_path: str | None = None, stdin: str | None = None):
    full = {**os.environ, **(env or {})}
    if extra_path:
        full["PATH"] = extra_path + os.pathsep + full["PATH"]
    return subprocess.run(["bash", "-c", code], capture_output=True, text=True, env=full, input=stdin)


@unittest.skipUnless(HAVE_BASH, "bash not available")
class BootstrapScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.env = {"POSTER_LOG": str(self.t / "log"), "POSTER_TOKEN_FILE": str(self.t / "setup-token.txt"),
                    "POSTER_DIR_FILE": str(self.t / "dir"), "POSTER_PROJECT_DIR": str(self.t / "proj")}

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, code, **kw):
        return run_script(f'source "{SCRIPT}"\n{code}', {**self.env, **kw.pop("env", {})}, **kw)

    def project(self, name="project"):
        proj = self.t / name
        (proj / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "generate_keys.py", proj / "scripts")
        shutil.copy(ROOT / ".env.example", proj)
        return proj

    # ---- static properties ---------------------------------------------------------------------
    def test_syntax_of_every_deploy_script(self):
        for path in (ROOT / "deploy").glob("*.sh"):
            self.assertEqual(subprocess.run(["bash", "-n", str(path)]).returncode, 0, path.name)

    def test_user_data_is_small_and_has_the_shebang(self):
        self.assertTrue(SCRIPT.read_text().startswith("#!/bin/bash\n"))
        self.assertLess(SCRIPT.stat().st_size, 16 * 1024)  # far below the 32 KiB user-data limit
        self.assertLess(SCRIPT.stat().st_size, 32 * 1024)

    def test_user_data_contains_no_secret_and_no_secret_inputs(self):
        text = SCRIPT.read_text()
        for forbidden in ("GIT_TOKEN", "x-access-token", "github_pat_", "ghp_", "SETUP_TOKEN=\"", "APP_SECRET=",
                          "SESSION_ENCRYPTION_KEY=", "POSTGRES_PASSWORD=", "TELEGRAM"):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertNotRegex(text, r"[0-9a-f]{32}")
        assigned = re.findall(r"(?m)^([A-Z_]+)=\"[^$]", text)
        self.assertEqual(assigned, ["REPO_URL"])  # the only value a person could/should ever see in the paste
        self.assertIn("github.com/kawsar98dd-lang/telegram-autopost-bot.git", text)

    def test_no_shell_tracing_and_secrets_are_not_printed_or_logged(self):
        text = SCRIPT.read_text()
        self.assertNotRegex(text, r"(?m)^\s*set\s+-[a-z]*x")
        self.assertNotRegex(text, r"echo[^\n]*(TOKEN|APP_SECRET|ENCRYPTION)")
        self.assertNotRegex(text, r"log [^\n]*\$\(<")  # nothing read from the token file goes into the log
        announces = [l for l in text.splitlines() if "announce_on_console" in l and "$(<" in l]
        self.assertEqual(len(announces), 1)  # the token is only ever sent to the console screens
        self.assertIn("> \"$t\"", text)

    def test_docker_is_installed_from_signed_ubuntu_packages_first(self):
        text = SCRIPT.read_text()
        self.assertLess(text.index("docker.io docker-compose-v2"), text.index("get.docker.com"))

    def test_helper_scripts_never_pass_secrets_on_command_lines(self):
        text = (ROOT / "deploy" / "check-secrets.sh").read_text()
        self.assertNotRegex(text, r'grep\s+(-\w+\s+)*(--\s+)?"\$secret"')  # a direct argument would show up in `ps`
        self.assertEqual(text.count("-f <(printf '%s\\n' \"$secret\")"), 2)
        self.assertNotRegex((SCRIPT.read_text()), r"(python3|psql|docker)[^\n]*\$\(<")  # token never an argument

    # ---- behaviour -------------------------------------------------------------------------------
    def test_sourcing_does_not_run_the_installation(self):
        self.assertEqual(self.call("echo sourced-only").stdout.strip(), "sourced-only")

    def test_input_check(self):
        self.assertIn("rc=0", self.call("check_inputs; echo rc=$?").stdout)
        for bad in ("http://github.com/a/b.git", "https://x.com/a b", "https://x.com/a;b", "git@github.com:a/b.git"):
            out = run_script(f'source "{SCRIPT}"\nREPO_URL="$1"; check_inputs; echo rc=$?', self.env, None) if False else \
                subprocess.run(["bash", "-c", f'source "{SCRIPT}"\nREPO_URL="$1"; check_inputs; echo rc=$?', "_", bad],
                               capture_output=True, text=True, env={**os.environ, **self.env})
            self.assertIn("rc=1", out.stdout, bad)

    def test_ip_validation_and_host_derivation(self):
        self.assertEqual(self.call("derive_host 203.0.113.5").stdout.strip(), "203-0-113-5.sslip.io")
        for bad in ("999.1.1.1", "1.2.3", "a.b.c.d", "1.2.3.4.5", "256.0.0.1", "", "1.2.3.400"):
            self.assertNotEqual(self.call(f'derive_host "{bad}"; echo "rc=$?"').stdout.strip().split()[-1], "rc=0", bad)
        for good in ("0.0.0.0", "255.255.255.255", "10.20.30.40"):
            self.assertEqual(self.call(f'derive_host {good}; echo "rc=$?"').stdout.strip().split()[-1], "rc=0", good)

    def test_setup_token_is_random_private_and_never_printed(self):
        tokens = set()
        for _ in range(5):
            result = self.call("create_setup_token; echo rc=$?")
            self.assertIn("rc=0", result.stdout)
            self.assertEqual(result.stdout.strip(), "rc=0")  # nothing but the exit code reached stdout
            self.assertEqual(result.stderr, "")
            token_file = Path(self.env["POSTER_TOKEN_FILE"])
            self.assertEqual(oct(token_file.stat().st_mode & 0o777), "0o600")
            token = token_file.read_text().strip()
            self.assertRegex(token, r"^[A-HJ-NP-Z2-9]{5}(-[A-HJ-NP-Z2-9]{5}){3}$")
            self.assertNotIn(token, result.stdout + result.stderr)
            self.assertFalse(Path(self.env["POSTER_LOG"]).exists() and token in Path(self.env["POSTER_LOG"]).read_text())
            tokens.add(token)
        self.assertEqual(len(tokens), 5)  # fresh randomness every time

    def test_a_pre_existing_token_file_with_loose_permissions_ends_up_private(self):
        token_file = Path(self.env["POSTER_TOKEN_FILE"])
        token_file.write_text("old")
        token_file.chmod(0o644)
        self.call("create_setup_token")
        self.assertEqual(oct(token_file.stat().st_mode & 0o777), "0o600")

    def test_generated_env_is_private_and_valid_and_uses_the_server_token(self):
        proj = self.project()
        self.call("create_setup_token")
        token = Path(self.env["POSTER_TOKEN_FILE"]).read_text().strip()
        result = self.call(f'write_env "{proj}" 203-0-113-5.sslip.io; echo rc=$?')
        self.assertIn("rc=0", result.stdout, result.stderr)
        env_file = proj / ".env"
        self.assertEqual(oct(env_file.stat().st_mode & 0o777), "0o600")
        text = env_file.read_text()
        values = parse_dotenv(text)
        for key, expected in {"APP_URL": "https://203-0-113-5.sslip.io", "APP_HOST": "203-0-113-5.sslip.io",
                              "WEB_BIND_ADDR": "127.0.0.1", "LICENSE_ENFORCEMENT": "false",
                              "TRUST_PROXY_HEADERS": "true", "APP_ENV": "development", "SETUP_TOKEN": token}.items():
            self.assertEqual(values[key], expected, key)
            self.assertEqual(len(re.findall(rf"(?m)^{key}=", text)), 1, key)
        values["DATABASE_URL"] = "postgresql://poster:x@db:5432/poster"
        settings = settings_from_env(values)
        self.assertTrue(settings.cookie_secure)
        self.assertFalse(settings.license_enforcement)
        self.assertTrue(settings.trust_proxy_headers)
        self.assertEqual(settings.setup_token, token)
        self.assertTrue(csrf.origin_ok(settings.app_url, "https://203-0-113-5.sslip.io", None))
        self.assertGreaterEqual(len(values["APP_SECRET"]), 32)
        self.assertNotIn(token, result.stdout + result.stderr)
        again = self.call(f'write_env "{proj}" 203-0-113-5.sslip.io; echo rc=$?')
        self.assertIn("rc=1", again.stdout)  # never silently replaces existing secrets

    def test_each_server_gets_different_secrets(self):
        keys = []
        for n in range(2):
            proj = self.project(f"project{n}")
            self.call("create_setup_token")
            self.call(f'write_env "{proj}" 1-2-3-4.sslip.io')
            keys.append(parse_dotenv((proj / ".env").read_text())["SESSION_ENCRYPTION_KEY"])
        self.assertNotEqual(*keys)

    def test_the_setup_token_is_never_passed_in_a_process_command_line(self):
        shim_dir = self.t / "shims"
        shim_dir.mkdir()
        argv_log = self.t / "argv.log"
        for name in ("python3", "chmod", "cat"):
            real = shutil.which(name)
            shim = shim_dir / name
            shim.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{argv_log}"\nexec "{real}" "$@"\n')
            shim.chmod(0o755)
        proj = self.project()
        self.call("create_setup_token", extra_path=str(shim_dir))
        self.call(f'write_env "{proj}" 1-2-3-4.sslip.io', extra_path=str(shim_dir))
        token = Path(self.env["POSTER_TOKEN_FILE"]).read_text().strip()
        secrets = [token] + [parse_dotenv((proj / ".env").read_text())[k] for k in ("APP_SECRET", "SESSION_ENCRYPTION_KEY")]
        argv = argv_log.read_text()
        self.assertGreater(len(argv), 0)
        for secret in secrets:
            self.assertNotIn(secret, argv)

    # ---- automatic cleanup of the setup token -------------------------------------------------------
    CLEANUP = ROOT / "deploy" / "poster-token-cleanup.sh"

    def cleanup_call(self, code, env=None):
        return run_script(f'source "{self.CLEANUP}"\n{code}', {**self.env, **(env or {})})

    def test_token_is_removed_only_after_the_first_administrator_exists(self):
        proj = self.project()
        self.call("create_setup_token")
        self.call(f'write_env "{proj}" 1-2-3-4.sslip.io')
        Path(self.env["POSTER_DIR_FILE"]).write_text(str(proj) + "\n")
        before = (proj / ".env").read_text()
        token_file = Path(self.env["POSTER_TOKEN_FILE"])
        waiting = self.cleanup_call("setup_done() { return 1; }; run_once; echo rc=$?")
        self.assertIn("rc=1", waiting.stdout)
        self.assertTrue(token_file.exists())
        self.assertEqual((proj / ".env").read_text(), before)
        done = self.cleanup_call("setup_done() { return 0; }; run_once; echo rc=$?")
        self.assertIn("rc=0", done.stdout)
        self.assertFalse(token_file.exists())
        after = (proj / ".env").read_text()
        self.assertNotRegex(after, r"(?m)^SETUP_TOKEN=")
        self.assertEqual(oct((proj / ".env").stat().st_mode & 0o777), "0o600")
        self.assertEqual(sorted(l.split("=")[0] for l in before.splitlines() if "=" in l and not l.startswith("#") and not l.startswith("SETUP_TOKEN")),
                         sorted(l.split("=")[0] for l in after.splitlines() if "=" in l and not l.startswith("#")))
        self.assertIn("setup token deleted", Path(self.env["POSTER_LOG"]).read_text())
        self.assertNotIn(parse_dotenv(before)["SETUP_TOKEN"], Path(self.env["POSTER_LOG"]).read_text())

    def test_setup_done_asks_the_database_and_treats_errors_as_not_done(self):
        proj = self.project()
        Path(self.env["POSTER_DIR_FILE"]).write_text(str(proj) + "\n")
        shims = self.t / "dockershim"
        shims.mkdir()
        docker = shims / "docker"
        docker.write_text('#!/bin/bash\ncase "$DOCKER_ANSWER" in one) echo 1;; none) ;; fail) exit 1;; esac\n')
        docker.chmod(0o755)
        for answer, expected in (("one", "rc=0"), ("none", "rc=1"), ("fail", "rc=1")):
            out = run_script(f'source "{self.CLEANUP}"\nsetup_done; echo rc=$?', {**self.env, "DOCKER_ANSWER": answer}, str(shims))
            self.assertIn(expected, out.stdout, answer)

    def test_check_secrets_keeps_typed_secrets_off_command_lines_and_out_of_output(self):
        shims = self.t / "cs"
        shims.mkdir()
        argv_log = self.t / "cs-argv.log"
        secret_hash, secret_phone = "0123456789abcdef0123456789abcdef", "8801712345678"
        (shims / "docker").write_text(
            '#!/bin/bash\nprintf "%s\\n" "$*" >> "' + str(argv_log) + '"\n'
            'case "$*" in *pg_dump*) echo "row with ' + secret_hash + ' inside";; *logs*) echo "log line clean";; *psql*) echo "(1 row)";; esac\n')
        real_grep = shutil.which("grep")
        (shims / "grep").write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{argv_log}"\nexec "{real_grep}" "$@"\n')
        for f in shims.iterdir():
            f.chmod(0o755)
        out = run_script(f'bash "{ROOT / "deploy" / "check-secrets.sh"}"', {}, str(shims),
                         stdin=f"{secret_hash}\n\n\n{secret_phone}\n")
        self.assertIn("API hash: matches in database = 1, in logs = 0", out.stdout)
        self.assertIn("phone number digits: matches in database = 0, in logs = 0", out.stdout)
        for secret in (secret_hash, secret_phone):
            self.assertNotIn(secret, out.stdout.replace("row with " + secret_hash, ""))
            self.assertNotIn(secret, argv_log.read_text())  # never on any command line


class AndroidCloudDocTests(unittest.TestCase):
    def setUp(self):
        self.doc = DOC.read_text(encoding="utf-8")

    def test_document_covers_the_required_sections(self):
        for needle in ("Recommended temporary deployment", "Setup steps", "Android browser steps", "HTTPS", "Destroy",
                       "PASS", "FAIL", "view-source:", "Firewall", "Primary IP", "sslip.io", "docker-compose.https.yml",
                       "NEVER share", "poster-token", "poster-diagnose", "no secret", "has **not** been tested",
                       "STEP3_REAL_TELEGRAM_VERIFICATION.md"):
            self.assertIn(needle, self.doc, needle)

    def test_document_never_asks_for_secrets_and_contains_none(self):
        self.assertNotRegex(self.doc, r"[0-9a-f]{32}")
        self.assertNotRegex(self.doc, r"github_pat_[A-Za-z0-9]{20,}")
        self.assertNotRegex(self.doc, r"gAAAA[A-Za-z0-9_-]{20,}")
        self.assertNotRegex(self.doc, r"(?i)(send|paste|give|share) (it |them |this )?(to |with )?(me|the developer|claude)")
        for forbidden in ("GIT_TOKEN", "fine-grained", "Personal access token", "SETUP_TOKEN"):
            self.assertNotIn(forbidden, self.doc, forbidden)  # no GitHub token, and no token to place into user data

    def test_main_checklist_is_reused_not_duplicated_or_weakened(self):
        main = (ROOT / "docs" / "STEP3_REAL_TELEGRAM_VERIFICATION.md").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"^\| (\d+) \|", main, re.M), [str(i) for i in range(1, 17)])
        self.assertIn("steps 1-16", self.doc.lower())


if __name__ == "__main__":
    unittest.main()
