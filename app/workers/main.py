"""Background worker process (foundation: start-up, license checks, clean shutdown).

Job processing (scheduler, queue claim with FOR UPDATE SKIP LOCKED, Telegram
sending) is added in later steps; this process already enforces the license and
keeps the DB/schema/encryption wiring identical to the web process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from ..config import ConfigError, load_settings
from ..db.migrate import ensure_schema
from ..db.pool import PoolDb, create_pool
from ..licensing.setup import LicenseSetupError, build_license_manager
from ..logging_setup import setup_logging
from ..security.crypto import init_cipher

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
    cipher = init_cipher(settings.session_encryption_key)
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

    log.info("worker started")
    last_state = None
    while not stop.is_set():
        touch_heartbeat()
        try:
            status = await manager.ensure_fresh()
            if status.state != last_state:
                log.info("license state: %s %s", status.state.value, status.detail)
                last_state = status.state
            if status.enabled:
                pass  # job processing arrives in a later step
        except Exception:
            log.exception("worker iteration failed")
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
