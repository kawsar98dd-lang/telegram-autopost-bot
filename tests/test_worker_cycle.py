"""The worker loop body: nothing happens without a valid license; recovery timing; stop handling.

Offline tests with recording stand-ins plus one end-to-end run on the SQLite queue with the fake Telegram network.
They do NOT replace a live test of the real process (database connection, signals, Telegram).
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.licensing.offline import OfflineLicenseManager
from app.workers.main import CycleState, work_cycle
from tests.support import signing
from tests.test_offline_license import HOST, NOW, PRODUCT, make_license
from tests.test_scheduler_queue import SchedBase


class Recorder:
    def __init__(self):
        self.calls = []

    async def recover_stale(self):
        self.calls.append("recover")
        return {"uncertain": 0, "worker_crashed": 0, "requeued": 0}

    async def materialize_due(self):
        self.calls.append("materialize")
        return 0

    async def run_once(self):
        self.calls.append("run")
        return False


class Mgr:
    def __init__(self, manager):
        self.m = manager

    async def ensure_fresh(self):
        return await self.m.ensure_fresh()


class CycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.private, self.public = signing.generate_keypair()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "license.json"
        self.now = [NOW]
        self.manager = OfflineLicenseManager(public_key=self.public, product=PRODUCT, host=HOST, file_path=str(self.path),
                                             clock=lambda: self.now[0], reload_seconds=1)
        self.rec = Recorder()
        self.state = CycleState()
        self.stop = asyncio.Event()
        self.mono = [1000.0]

    def tearDown(self):
        self.tmp.cleanup()

    async def cycle(self):
        return await work_cycle(self.manager, self.rec, self.rec, self.stop, self.state, monotonic=lambda: self.mono[0])

    async def test_without_a_license_nothing_is_recovered_created_or_sent(self):
        self.assertFalse(await self.cycle())
        self.assertEqual(self.rec.calls, [])

    async def test_invalid_or_foreign_license_does_the_same(self):
        other_private, _ = signing.generate_keypair()
        for text in (make_license(other_private), "{broken", make_license(self.private, product="other-product")):
            self.path.write_text(text)
            self.now[0] += 5
            self.assertFalse(await self.cycle())
        self.assertEqual(self.rec.calls, [])

    async def test_valid_license_runs_recovery_first_then_creates_and_sends(self):
        self.path.write_text(make_license(self.private))
        self.assertTrue(await self.cycle())
        self.assertEqual(self.rec.calls, ["recover", "materialize", "run"])

    async def test_recovery_runs_on_the_first_cycle_even_right_after_boot_and_then_every_30_seconds(self):
        self.path.write_text(make_license(self.private))
        self.mono[0] = 3.0                                   # a host that booted 3 seconds ago
        await self.cycle()
        self.assertEqual(self.rec.calls.count("recover"), 1)
        self.mono[0] += 10
        await self.cycle()
        self.assertEqual(self.rec.calls.count("recover"), 1)
        self.mono[0] += 25
        await self.cycle()
        self.assertEqual(self.rec.calls.count("recover"), 2)

    async def test_license_expiring_while_running_stops_all_work(self):
        self.path.write_text(make_license(self.private, expires=NOW + 100))
        self.assertTrue(await self.cycle())
        self.rec.calls.clear()
        self.now[0] = NOW + 101
        self.assertFalse(await self.cycle())
        self.assertEqual(self.rec.calls, [])

    async def test_replacing_the_license_resumes_work_without_restart(self):
        self.assertFalse(await self.cycle())
        self.path.write_text(make_license(self.private))
        self.now[0] += 5
        self.assertTrue(await self.cycle())

    async def test_stop_request_prevents_claiming(self):
        self.path.write_text(make_license(self.private))
        self.stop.set()
        await self.cycle()
        self.assertNotIn("run", self.rec.calls)


class EndToEndEnforcementTests(SchedBase):
    """Real queue + executor + fake Telegram: a due post is sent only while the license is valid."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.private, self.public = signing.generate_keypair()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "license.json"
        self.manager = OfflineLicenseManager(public_key=self.public, product=PRODUCT, host=HOST, file_path=str(self.path),
                                             clock=lambda: self.env.clock.now, reload_seconds=1)
        self.state = CycleState()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_no_license_no_post_then_license_then_exactly_one_post(self):
        await self.schedule()
        self.due_now()
        self.env.clock.now = NOW + 19 * 3600 + 60       # the scheduler tests' clock is the license tests' clock
        self.assertFalse(await work_cycle(self.manager, self.queue, self.executor, self.stop, self.state))
        self.assertEqual(self.world.sent, [])
        self.assertEqual(self.jobs(), [])                # not even a job was created
        self.path.write_text(make_license(self.private, issued=NOW - 86400))
        self.env.clock.advance(5)
        self.assertTrue(await work_cycle(self.manager, self.queue, self.executor, self.stop, self.state))
        self.assertEqual(len(self.world.sent), 1)
        self.assertEqual(self.only_job()["status"], "posted")
        await work_cycle(self.manager, self.queue, self.executor, self.stop, self.state)
        self.assertEqual(len(self.world.sent), 1)         # never twice


if __name__ == "__main__":
    unittest.main()
