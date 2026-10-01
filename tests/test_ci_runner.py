"""Tests for the CI verification runner itself (scripts/ci_verify.py)."""

import io
import itertools
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from scripts import ci_verify

_ids = itertools.count(1)


def make_suite(files: dict[str, str]):
    """Creates a throw-away test package on disk and returns (dir, package_name)."""
    tmp = tempfile.TemporaryDirectory()
    pkg = f"citmp{next(_ids)}"
    root = Path(tmp.name)
    (root / pkg).mkdir()
    (root / pkg / "__init__.py").write_text("")
    for name, body in files.items():
        (root / pkg / name).write_text(textwrap.dedent(body))
    sys.path.insert(0, str(root))
    return tmp, root, pkg


def run(files, required=None):
    tmp, root, pkg = make_suite(files)
    try:
        report = ci_verify.run_suite(root / pkg, root, required, stream=io.StringIO(), verbosity=0)
    finally:
        sys.path.remove(str(root))
        for name in [m for m in sys.modules if m.startswith(pkg)]:
            del sys.modules[name]
        tmp.cleanup()
    return report, pkg


PASSING = "import unittest\nclass A(unittest.TestCase):\n    def test_one(self): pass\n    def test_two(self): pass\n"


class RunnerTests(unittest.TestCase):
    def test_all_passing_is_pass(self):
        report, _ = run({"test_a.py": PASSING})
        self.assertEqual((report.total, report.passed, report.skipped, report.failed, report.errors), (2, 2, 0, 0, 0))
        self.assertTrue(report.ok)

    def test_a_real_skip_makes_it_fail_and_names_the_test_and_reason(self):
        report, _ = run({"test_a.py": PASSING, "test_s.py": """
            import unittest
            class S(unittest.TestCase):
                @unittest.skip("needs postgres")
                def test_x(self): pass
        """})
        self.assertEqual(report.skipped, 1)
        self.assertFalse(report.ok)
        self.assertIn("needs postgres", report.skipped_tests[0])

    def test_skip_inside_the_test_body_is_detected_too(self):
        report, _ = run({"test_s.py": "import unittest\nclass S(unittest.TestCase):\n    def test_x(self): self.skipTest('late skip')\n"})
        self.assertEqual((report.skipped, report.ok), (1, False))

    def test_the_word_skipped_in_a_test_name_or_output_is_NOT_a_skip(self):
        # This is exactly what broke the first GitHub run (a test called ..._cannot_be_skipped_in_ci).
        report, _ = run({"test_names.py": """
            import unittest
            class SkippedWordTests(unittest.TestCase):
                def test_integration_tests_cannot_be_skipped_in_ci(self):
                    print("this output says skipped skipped skipped")
        """})
        self.assertEqual((report.total, report.passed, report.skipped), (1, 1, 0))
        self.assertTrue(report.ok)

    def test_failures_and_errors_fail(self):
        report, _ = run({"test_f.py": """
            import unittest
            class F(unittest.TestCase):
                def test_fail(self): self.assertEqual(1, 2)
                def test_error(self): raise RuntimeError("x")
        """})
        self.assertEqual((report.failed, report.errors, report.ok), (1, 1, False))

    def test_unexpected_success_counts_as_failure(self):
        report, _ = run({"test_u.py": "import unittest\nclass U(unittest.TestCase):\n    @unittest.expectedFailure\n    def test_x(self): pass\n"})
        self.assertFalse(report.ok)

    def test_required_tests_must_have_actually_executed(self):
        files = {"test_a.py": PASSING}
        tmp, root, pkg = make_suite(files)
        try:
            ok = ci_verify.run_suite(root / pkg, root, {f"{pkg}.test_a.A.": 2}, stream=io.StringIO(), verbosity=0)
            missing = ci_verify.run_suite(root / pkg, root, {f"{pkg}.test_a.A.": 3, f"{pkg}.test_zzz.": 1},
                                          stream=io.StringIO(), verbosity=0)
        finally:
            sys.path.remove(str(root))
            for name in [m for m in sys.modules if m.startswith(pkg)]:
                del sys.modules[name]
            tmp.cleanup()
        self.assertTrue(ok.ok)
        self.assertEqual(len(missing.missing_required), 2)
        self.assertFalse(missing.ok)

    def test_a_required_class_that_is_entirely_skipped_fails_twice_over(self):
        tmp, root, pkg = make_suite({"test_pg.py": """
            import unittest
            @unittest.skip("no database")
            class PostgresLike(unittest.TestCase):
                def test_a(self): pass
        """})
        try:
            report = ci_verify.run_suite(root / pkg, root, {f"{pkg}.test_pg.PostgresLike.": 1}, stream=io.StringIO(), verbosity=0)
        finally:
            sys.path.remove(str(root))
            for name in [m for m in sys.modules if m.startswith(pkg)]:
                del sys.modules[name]
            tmp.cleanup()
        self.assertEqual((report.skipped, len(report.missing_required), report.ok), (1, 1, False))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_exit_codes_and_report_file(self):
        passing = ci_verify.Report(total=3, passed=3)
        skipping = ci_verify.Report(total=3, passed=2, skipped=1, skipped_tests=["t.x  [why]"])
        with mock.patch("builtins.print"):
            self.assertEqual(ci_verify.print_report(passing, allow_skips=False), 0)
            self.assertEqual(json.loads(Path("test-report.json").read_text())["result"], "PASS")
            self.assertEqual(ci_verify.print_report(skipping, allow_skips=False), 1)
            data = json.loads(Path("test-report.json").read_text())
            self.assertTrue(data["result"].startswith("FAIL"))
            self.assertEqual(data["skipped"], 1)
            self.assertEqual(ci_verify.print_report(skipping, allow_skips=True), 0)  # local convenience only
            self.assertEqual(ci_verify.print_report(ci_verify.Report(total=1, failed=1), allow_skips=True), 1)

    def test_github_step_summary_is_written(self):
        summary = Path(self.tmp.name) / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), mock.patch("builtins.print"):
            ci_verify.print_report(ci_verify.Report(total=5, passed=5), allow_skips=False)
        self.assertIn("**PASS**", summary.read_text())


class DiagnosticsTests(unittest.TestCase):
    def run_diagnostics(self, env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch("sys.stdout", out):
            code = ci_verify.diagnostics()
        return code, out.getvalue()

    def test_never_prints_secret_values(self):
        secret_url = "postgresql://ci_user:SuperSecretPw123@db.internal.example:5432/prod"
        env = {"TEST_DATABASE_URL": secret_url, "REQUIRE_POSTGRES_TESTS": "1", "DATABASE_URL": secret_url,
               "APP_SECRET": "very-secret-app-value", "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef"}
        with mock.patch.object(ci_verify, "_postgres_probe", new=mock.AsyncMock(return_value=(False, ""))):
            code, text = self.run_diagnostics(env)
        for secret in ("SuperSecretPw123", "db.internal.example", "ci_user", "very-secret-app-value", "0123456789abcdef"):
            self.assertNotIn(secret, text)
        self.assertIn("TEST_DATABASE_URL set: YES", text)
        self.assertIn("REQUIRE_POSTGRES_TESTS set: YES", text)
        self.assertIn("postgresql reachable: NO", text)

    def test_reports_flags_and_missing_pieces_as_yes_no(self):
        env = {k: "" for k in ("TEST_DATABASE_URL", "REQUIRE_POSTGRES_TESTS", "REQUIRE_FASTAPI_TESTS", "REQUIRE_VENDORED_HTMX")}
        code, text = self.run_diagnostics(env)
        for line in ("TEST_DATABASE_URL set: NO", "REQUIRE_FASTAPI_TESTS set: NO", "REQUIRE_VENDORED_HTMX set: NO",
                     "python: ", "package fastapi:", "import asyncpg:", "htmx file exists"):
            self.assertIn(line, text)

    def test_probe_failure_never_leaks_connection_details(self):
        import asyncio

        reachable, major = asyncio.run(ci_verify._postgres_probe("postgresql://u:pw@127.0.0.1:1/x"))
        self.assertEqual((reachable, major), (False, ""))

    def test_ci_mode_exits_nonzero_when_a_prerequisite_is_missing(self):
        code, _ = self.run_diagnostics({"CI": "true", "TEST_DATABASE_URL": "", "REQUIRE_POSTGRES_TESTS": "1"})
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
