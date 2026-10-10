"""CI verification: non-secret diagnostics + a deterministic test runner.

    python scripts/ci_verify.py diagnostics   # prints environment facts (never values of secrets)
    python scripts/ci_verify.py run           # runs the whole suite; ANY skip or failure => exit 1
    python scripts/ci_verify.py run --allow-skips   # local convenience only; CI never uses it

Instead of grepping console text, the runner inspects the unittest result object, so a test that merely
has the word "skipped" in its name cannot cause (or hide) anything. It also insists that the integration
tests which previously could be skipped (PostgreSQL, FastAPI, HTMX) were actually EXECUTED and PASSED.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import sys
import unittest
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# (id prefix, minimum number of tests of that kind that must have run and passed)
REQUIRED_EXECUTED = {
    "tests.test_postgres_integration.PostgresTests.": 8,
    "tests.test_fastapi_smoke.FastApiSmokeTests.": 8,
    "tests.test_web_dashboard_errors.StaticAnalysisTests.test_vendored_htmx_file_matches_the_pinned_checksum": 1,
    "tests.test_web_posts.": 40,
    "tests.test_posts_units.": 30,
    # Step 6: scheduler + worker (the PostgreSQL ones must really run against the CI database)
    "tests.test_postgres_scheduler.SchedulerPostgresTests.": 6,
    "tests.test_scheduler_queue.": 46,
    "tests.test_web_schedules.": 11,
    "tests.test_recurrence.": 19,
    "tests.test_scheduler_policy.": 7,
    # Offline signed licensing and the customer release builder
    "tests.test_offline_license.": 43,
    "tests.test_customer_release.": 7,
    "tests.test_licensing_docs.": 5,
    # Production setup lock and worker enforcement
    "tests.test_setup_lock.": 12,
    "tests.test_worker_cycle.": 8,
}
REQUIRE_FLAGS = ("REQUIRE_POSTGRES_TESTS", "REQUIRE_FASTAPI_TESTS", "REQUIRE_VENDORED_HTMX")


# ---------------------------------------------------------------------------------- diagnostics
def _yes(flag: bool) -> str:
    return "YES" if flag else "NO"


def diagnostics() -> int:
    problems = 0
    print("==== CI DIAGNOSTICS (non-secret) ====")
    print(f"python: {platform.python_version()} ({platform.system()})")
    for package in ("fastapi", "starlette", "uvicorn", "asyncpg", "httpx", "cryptography", "jinja2", "telethon", "pyyaml"):
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = "NOT INSTALLED"
        print(f"package {package}: {version}")
    for module in ("fastapi", "fastapi.testclient", "asyncpg", "httpx", "yaml", "telethon"):
        try:
            importlib.import_module(module)
            ok = True
        except Exception as exc:  # noqa: BLE001 - only the class name is printed
            ok, detail = False, type(exc).__name__
        print(f"import {module}: {_yes(ok)}" + ("" if ok else f" ({detail})"))
        if not ok and module in ("fastapi", "fastapi.testclient", "asyncpg", "httpx", "yaml"):
            problems += 1

    print(f"TEST_DATABASE_URL set: {_yes(bool(os.environ.get('TEST_DATABASE_URL')))}")  # value is never printed
    for flag in REQUIRE_FLAGS:
        print(f"{flag} set: {_yes(bool(os.environ.get(flag)))}")

    from scripts import vendor_htmx

    target = vendor_htmx.TARGET
    print(f"htmx file exists ({target.relative_to(ROOT)}): {_yes(target.exists())}")
    if target.exists():
        actual = base64.b64encode(hashlib.sha384(target.read_bytes()).digest()).decode()
        match = actual == vendor_htmx.SHA384_B64
        print(f"htmx sha384 matches pinned value: {_yes(match)}")
        if not match:  # checksums are public values, safe to print for diagnosis
            print(f"  pinned : {vendor_htmx.SHA384_B64}\n  actual : {actual}")
            problems += 1
    else:
        problems += 1

    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        reachable, major = asyncio.run(_postgres_probe(url))
        print(f"postgresql reachable: {_yes(reachable)}" + (f" (server major version {major})" if reachable else ""))
        if not reachable:
            problems += 1
    else:
        print("postgresql reachable: NOT CHECKED (no TEST_DATABASE_URL)")
        if os.environ.get("REQUIRE_POSTGRES_TESTS"):
            problems += 1
    print(f"diagnostic problems: {problems}")
    return 1 if problems and os.environ.get("CI") else 0


async def _postgres_probe(url: str) -> tuple[bool, str]:
    try:
        import asyncpg

        conn = await asyncio.wait_for(asyncpg.connect(url), 15)
        try:
            version = await conn.fetchval("SHOW server_version_num")
        finally:
            await conn.close()
        return True, str(int(version) // 10000)
    except Exception:  # noqa: BLE001 - never print the message (it could contain connection details)
        return False, ""


# ---------------------------------------------------------------------------------- test runner
class RecordingResult(unittest.TextTestResult):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.passed: list[str] = []

    def addSuccess(self, test) -> None:  # noqa: N802 (unittest API)
        super().addSuccess(test)
        self.passed.append(test.id())


@dataclass
class Report:
    total: int = 0
    passed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: int = 0
    skipped_tests: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    required_counts: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not (self.skipped or self.failed or self.errors or self.missing_required)


def run_suite(start_dir: str | Path, top_level: str | Path, required: dict[str, int] | None = None,
              stream=None, verbosity: int = 2) -> Report:
    suite = unittest.TestLoader().discover(str(start_dir), top_level_dir=str(top_level))
    runner = unittest.TextTestRunner(stream=stream or sys.stdout, verbosity=verbosity, resultclass=RecordingResult)
    result = runner.run(suite)
    report = Report(
        total=result.testsRun, passed=len(result.passed), skipped=len(result.skipped),
        failed=len(result.failures) + len(result.unexpectedSuccesses), errors=len(result.errors),
        skipped_tests=[f"{t.id()}  [{reason}]" for t, reason in result.skipped],
    )
    for prefix, minimum in (required or {}).items():
        count = sum(1 for test_id in result.passed if test_id.startswith(prefix))
        report.required_counts[prefix] = count
        if count < minimum:
            report.missing_required.append(f"{prefix}: expected >= {minimum} executed and passed, got {count}")
    return report


def print_report(report: Report, allow_skips: bool) -> int:
    ok = report.ok if not allow_skips else not (report.failed or report.errors)
    status = "PASS" if report.ok else "FAIL"
    print("\n==== CI TEST VERIFICATION ====")
    print(f"tests run : {report.total}\npassed    : {report.passed}\nskipped   : {report.skipped}\n"
          f"failed    : {report.failed}\nerrors    : {report.errors}")
    for prefix, count in report.required_counts.items():
        print(f"required  : {prefix.split('.')[-1] or prefix.split('.')[-2]} executed+passed = {count}")
    for line in report.skipped_tests:
        print(f"SKIPPED   : {line}")
    for line in report.missing_required:
        print(f"MISSING   : {line}")
    if report.skipped:
        status = "FAIL (tests were skipped)" if not allow_skips else "SKIPPED (allowed locally)"
    print(f"RESULT: {status}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as fh:
            fh.write(f"### Test verification: **{status}**\n\n| total | passed | skipped | failed | errors |\n|---|---|---|---|---|\n"
                     f"| {report.total} | {report.passed} | {report.skipped} | {report.failed} | {report.errors} |\n")
    Path("test-report.json").write_text(json.dumps({"result": status, **asdict(report)}, indent=2), encoding="utf-8")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("diagnostics", "run"))
    parser.add_argument("--allow-skips", action="store_true", help="local use only; CI must not pass this")
    args = parser.parse_args(argv)
    if args.command == "diagnostics":
        return diagnostics()
    report = run_suite(ROOT / "tests", ROOT, None if args.allow_skips else REQUIRED_EXECUTED)
    return print_report(report, args.allow_skips)


if __name__ == "__main__":
    raise SystemExit(main())
