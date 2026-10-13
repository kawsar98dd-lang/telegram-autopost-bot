"""start_single_host.py: the optional launcher for single-command hosts (replaces the unsafe ad-hoc main.py wrapper)."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import start_single_host as launcher

ROOT = Path(__file__).resolve().parent.parent


class Proc:
    def __init__(self, codes, pid=1):
        self.codes, self.pid, self.terminated, self.killed, self._alive = list(codes), pid, False, False, True
        self.wait_timeouts = []

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.codes and self.codes[0] == "hang":
            self.codes.pop(0)
            raise subprocess.TimeoutExpired("x", timeout)
        self._alive = False
        return self.codes.pop(0) if self.codes else 0

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self._alive = False


class ConfigurationTests(unittest.TestCase):
    def test_env_file_is_read_with_the_apps_parser_and_real_environment_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("PORT=9000  # panel port\nAPP_ENV=production\nQUOTED='x y'\n")
            env = launcher.merged_env(path, {"PORT": "9100"})
        self.assertEqual(env["PORT"], "9100")                  # the real environment overrides .env (the old wrapper did the opposite)
        self.assertEqual((env["APP_ENV"], env["QUOTED"]), ("production", "x y"))   # APP_ENV is never forced
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("PORT=9000  # panel port\n")
            self.assertEqual(launcher.merged_env(path, {})["PORT"], "9000")         # inline comment is not part of the value

    def test_port_comes_from_configuration_not_from_a_constant(self):
        self.assertEqual(launcher.web_port({}), 8000)
        self.assertEqual(launcher.web_port({"WEB_PORT": "8123"}), 8123)
        self.assertEqual(launcher.web_port({"PORT": "10926", "WEB_PORT": "1"}), 10926)
        for bad in ("abc", "0", "70000", "-5"):
            with self.assertRaises(SystemExit, msg=bad):
                launcher.web_port({"PORT": bad})

    def test_source_never_forces_development_mode_or_a_fixed_port(self):
        text = (ROOT / "start_single_host.py").read_text()
        self.assertNotIn('os.environ[', text)
        self.assertNotIn('"development"', text)
        self.assertNotIn("10926", text)
        self.assertNotIn("check=False)\n\n# 2", text)


class MigrationTests(unittest.TestCase):
    def test_failed_migration_stops_the_start(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=3))
        with self.assertRaises(SystemExit) as ctx:
            launcher.run_migrations({}, runner=runner)
        self.assertIn("FAILED", str(ctx.exception))
        self.assertIn("exit code 3", str(ctx.exception))

    def test_success_and_opt_out(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        launcher.run_migrations({}, runner=runner)
        self.assertEqual(runner.call_count, 1)
        launcher.run_migrations({"AUTO_MIGRATE": "false"}, runner=runner)
        self.assertEqual(runner.call_count, 1)

    def test_main_never_starts_worker_or_web_when_migrations_fail(self):
        with mock.patch.object(launcher, "merged_env", return_value={}), \
                mock.patch.object(launcher, "run_migrations", side_effect=SystemExit("boom")), \
                mock.patch.object(launcher, "WorkerSupervisor") as sup:
            with self.assertRaises(SystemExit):
                launcher.main()
        sup.assert_not_called()


class SupervisorTests(unittest.TestCase):
    def make(self, procs, clock):
        sleeps = []
        spawned = iter(procs)
        sup = launcher.WorkerSupervisor(popen=lambda cmd: next(spawned), sleep=sleeps.append, monotonic=lambda: clock[0],
                                        command=["worker"])
        return sup, sleeps

    def test_a_crashing_worker_is_restarted_with_growing_delays(self):
        clock = [0.0]
        procs = [Proc([1]), Proc([1]), Proc([1]), Proc([1]), Proc([1]), Proc([1]), Proc([1])]
        sup, sleeps = self.make(procs, clock)
        original_wait = sup._wait

        def wait(seconds):
            original_wait(seconds)
            if len(sleeps) == 7:
                sup._stop.set()
        sup._wait = wait
        sup.run()
        self.assertEqual(sleeps[:6], [5, 10, 20, 40, 60, 60])   # grows, then stays at the maximum
        self.assertEqual(sup.starts, 7)

    def test_a_long_running_worker_resets_the_delay(self):
        clock = [0.0]

        class Slow(Proc):
            def wait(self, timeout=None):
                clock[0] += launcher.STABLE_SECONDS + 1
                return super().wait(timeout)
        sup, sleeps = self.make([Proc([1]), Slow([1]), Proc([1])], clock)
        original_wait = sup._wait
        sup._wait = lambda s: (original_wait(s), sup._stop.set() if len(sleeps) == 3 else None)[0]
        sup.run()
        self.assertEqual(sleeps, [5, 5, 5])                     # without the reset it would be [5, 10, 20]: the stable run restarted the sequence

    def test_stop_terminates_gracefully_and_prevents_restarts(self):
        proc = Proc([0])
        sup = launcher.WorkerSupervisor(popen=lambda c: proc, sleep=lambda s: None, command=["worker"])
        sup._proc = proc
        sup.stop()
        self.assertTrue(proc.terminated)
        self.assertFalse(proc.killed)
        self.assertEqual(proc.wait_timeouts, [launcher.STOP_GRACE_SECONDS])
        sup.run()                                               # stopped: run() returns without starting anything
        self.assertEqual(sup.starts, 0)

    def test_a_worker_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        proc = Proc(["hang", 0])
        sup = launcher.WorkerSupervisor(popen=lambda c: proc, command=["worker"])
        sup._proc = proc
        sup.stop()
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)


if __name__ == "__main__":
    unittest.main()
