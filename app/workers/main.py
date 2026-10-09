"""Background worker process: license checks, due schedules -> jobs, job claiming, Telegram sending, crash recovery.

The worker is the ONLY process that sends anything to Telegram. It shares the PostgreSQL database and the encryption
key with the web process; all state lives in PostgreSQL (see app/scheduler/queue.py), so the worker can be stopped,
restarted or run twice at any time. Several worker processes are safe: every claim is an atomic SKIP LOCKED update.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

from ..config import ConfigError, load_settings
from ..db.migrate import ensure_schema
from ..db.pool import PoolDb, create_pool
from ..licensing.setup import LicenseSetupError, build_license_manager
from ..logging_setup import setup_logging
from ..posts.storage import make_storage
from ..scheduler import policy
from ..scheduler.executor import JobExecutor
from ..scheduler.queue import JobQueue
from ..security.crypto import Cipher, init_cipher
from ..telegram.connect import TelegramConnectionService
from ..telegram.service import TelegramClientService

log = logging.getLogger("worker")
HEARTBEAT_FILE = os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/worker.heartbeat")


def touch_heartbeat(path: str = HEARTBEAT_FILE) -> None:
    """Liveness signal for the Docker health check."""
    try:
        Path(path).touch()
    except OSError:
        log.warning("cannot write heartbeat file")


async def run() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    setup_logging(settings.log_level)
    init_cipher(settings.session_encryption_key)
    cipher = Cipher(settings.session_encryption_key)
    if settings.auto_migrate:
        await ensure_schema(settings.database_url)
    db = PoolDb(await create_pool(settings.database_url, max_size=5))

    try:
        manager = await build_license_manager(settings, db, cipher)
    except LicenseSetupError as exc:
        log.error("%s", exc)
        await db.close()
        return 3

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    queue = JobQueue(db, time.time)
    telegram = TelegramClientService()
    connect = TelegramConnectionService(db, cipher, telegram, settings, time.time)
    executor = JobExecutor(db, queue, connect, telegram, make_storage(settings, db, time.time), settings, time.time,
                           worker_id=worker_id, stop=stop)

    log.info("worker started (id=%s)", worker_id)
    last_state = None
    last_recovery = 0.0
    while not stop.is_set():
        touch_heartbeat()
        try:
            status = await manager.ensure_fresh()
            if status.state != last_state:
                log.info("license state: %s %s", status.state.value, status.detail)
                last_state = status.state
            if status.enabled:
                if time.monotonic() - last_recovery >= policy.STALE_RECOVERY_SECONDS:
                    recovered = await queue.recover_stale()
                    if any(recovered.values()):
                        log.warning("recovered stale jobs: %s", recovered)
                    last_recovery = time.monotonic()
                await queue.materialize_due()
                while not stop.is_set() and await executor.run_once():  # drains everything that is due, account by account
                    touch_heartbeat()
        except Exception:
            log.exception("worker iteration failed")  # secrets are redacted by the log formatter
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
        except asyncio.TimeoutError:
            pass

    await db.close()
    log.info("worker stopped")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
