"""OPTIONAL launcher for hosting panels that can only run ONE start command (for example "python start_single_host.py").

Docker Compose (docker-compose.yml) is the recommended way: separate web and worker containers that Docker restarts. Use this
launcher only when your host gives you a single Python process and no Docker. It

  1. applies the database migrations and STOPS if they fail (the web server is never started on a broken schema);
  2. starts the worker as a supervised child process: it is restarted with a growing delay if it exits, and stopped cleanly
     when this launcher stops;
  3. runs the web server in the foreground on the port of the PORT variable (or WEB_PORT, default 8000).

It does NOT change your configuration: APP_ENV and every other setting come from the environment / `.env` exactly as
documented (real environment variables win over `.env`). It never forces development mode and never loads secrets itself.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Mapping

RESTART_DELAYS = (5, 10, 20, 40, 60)   # seconds; the last value repeats
STABLE_SECONDS = 300                   # a worker that ran this long starts again from the first delay
STOP_GRACE_SECONDS = 60                # the worker may finish the post it is sending before it is killed


def merged_env(dotenv: Path = Path(".env"), environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`.env` values, overridden by real environment variables (the same rule as app.config.load_settings)."""
    from app.config import parse_dotenv

    values: dict[str, str] = {}
    if dotenv.is_file():
        values.update(parse_dotenv(dotenv.read_text(encoding="utf-8")))
    values.update(os.environ if environ is None else environ)
    return values


def web_port(env: Mapping[str, str]) -> int:
    raw = (env.get("PORT") or env.get("WEB_PORT") or "8000").strip()
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"PORT/WEB_PORT must be a number, got {raw!r}") from None
    if not 1 <= port <= 65535:
        raise SystemExit("PORT/WEB_PORT must be between 1 and 65535")
    return port


def run_migrations(env: Mapping[str, str], *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
    if (env.get("AUTO_MIGRATE") or "true").strip().lower() in ("0", "false", "no", "off"):
        print(">>> AUTO_MIGRATE is off: migrations were not applied.", flush=True)
        return
    print(">>> Applying database migrations ...", flush=True)
    result = runner([sys.executable, "-m", "app.db.migrate"], check=False)
    if result.returncode != 0:
        raise SystemExit(f"Database migrations FAILED (exit code {result.returncode}); the web server is not started. "
                         "Read the messages above, fix DATABASE_URL / the database, and start again.")


class WorkerSupervisor:
    """Keeps `python -m app.workers.main` running; stops it cleanly. Injectable pieces make it testable without processes."""

    def __init__(self, *, popen=subprocess.Popen, sleep: Callable[[float], None] = time.sleep, monotonic=time.monotonic,
                 command: list[str] | None = None) -> None:
        self._popen, self._sleep, self._monotonic = popen, sleep, monotonic
        self._command = command or [sys.executable, "-m", "app.workers.main"]
        self._stop = threading.Event()
        self._proc = None
        self._thread: threading.Thread | None = None
        self.starts = 0

    def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            started = self._monotonic()
            self._proc = self._popen(self._command)
            self.starts += 1
            print(f">>> Worker started (pid {getattr(self._proc, 'pid', '?')}).", flush=True)
            code = self._proc.wait()
            if self._stop.is_set():
                return
            failures = 0 if self._monotonic() - started >= STABLE_SECONDS else failures + 1
            delay = RESTART_DELAYS[min(max(failures - 1, 0), len(RESTART_DELAYS) - 1)]
            print(f">>> Worker exited with code {code}; restarting in {delay} s.", flush=True)
            self._wait(delay)

    def _wait(self, seconds: float) -> None:
        if not self._stop.is_set():
            self._sleep(seconds)

    def start_in_background(self) -> None:
        self._thread = threading.Thread(target=self.run, name="worker-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()                       # SIGTERM: the worker finishes its current post, releases its jobs, exits
            try:
                proc.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=5)


def main() -> int:
    env = merged_env()
    port = web_port(env)
    run_migrations(env)
    supervisor = WorkerSupervisor()
    supervisor.start_in_background()

    def _terminate(*_args) -> None:  # panel/container stop (SIGTERM) -> same clean path as Ctrl+C
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    try:
        import uvicorn

        print(f">>> Starting the web server on port {port} ...", flush=True)
        uvicorn.run("app.web.main:create_app", host="0.0.0.0", port=port, factory=True)
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
