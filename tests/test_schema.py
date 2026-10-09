import re
import sqlite3
import unittest

from tests.support import SqliteDb, SqliteMigrationDriver, migrated_db
from app.db import models
from app.db.migrate import MIGRATIONS_DIR, Migration, MigrationError, discover, run_migrations
from app.licensing.store import DbLicenseStore, LicenseRecord, get_or_create_installation_id
from app.security.crypto import Cipher, DecryptionError, generate_key

SQL = "\n".join(p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS_DIR.glob("*.sql")))


def uid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


class Fixture:
    """Two users, each with an account, group, post and schedule."""

    def __init__(self, db: SqliteDb) -> None:
        c = db.conn
        for n, email in ((1, "a@example.com"), (2, "b@example.com")):
            c.execute("INSERT INTO users (id, email, password_hash) VALUES (?, ?, 'h')", (uid(n), email))
            c.execute("INSERT INTO telegram_accounts (id, user_id, tg_user_id) VALUES (?, ?, ?)", (uid(10 + n), uid(n), 1000 + n))
            c.execute("INSERT INTO telegram_groups (id, user_id, account_id, tg_chat_id, title, chat_type) VALUES (?,?,?,?,?,'supergroup')",
                      (uid(20 + n), uid(n), uid(10 + n), -100 * n, f"G{n}"))
            c.execute("INSERT INTO posts (id, user_id, body) VALUES (?, ?, 'hello')", (uid(30 + n), uid(n)))
            c.execute("INSERT INTO schedules (id, user_id, post_id, account_id, kind, timezone) VALUES (?,?,?,?,'daily','Asia/Dhaka')",
                      (uid(40 + n), uid(n), uid(30 + n), uid(10 + n)))
            c.execute("INSERT INTO schedule_targets (schedule_id, group_id, user_id) VALUES (?,?,?)", (uid(40 + n), uid(20 + n), uid(n)))
        c.commit()


class MigrationRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_applies_once_and_is_idempotent(self):
        db = SqliteDb()
        driver = SqliteMigrationDriver(db)
        first = await run_migrations(driver, discover())
        second = await run_migrations(driver, discover())
        self.assertEqual(first, ["0001_initial", "0002_auth", "0003_telegram_connect", "0004_telegram_groups", "0005_posts", "0006_scheduler"])
        self.assertEqual(second, [])

    async def test_modified_migration_is_refused(self):
        db = SqliteDb()
        driver = SqliteMigrationDriver(db)
        await run_migrations(driver, [Migration("0001", "x", "CREATE TABLE t (a INT);")])
        with self.assertRaises(MigrationError):
            await run_migrations(driver, [Migration("0001", "x", "CREATE TABLE t (a INT, b INT);")])

    async def test_failed_migration_rolls_back(self):
        db = SqliteDb()
        driver = SqliteMigrationDriver(db)
        with self.assertRaises(sqlite3.Error):
            await run_migrations(driver, [Migration("0001", "bad", "CREATE TABLE ok (a INT); CREATE TABLE ok (a INT);")])
        self.assertEqual(await driver.applied(), {})
        self.assertIsNone(db.conn.execute("SELECT name FROM sqlite_master WHERE name='ok'").fetchone())


class SchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await migrated_db()
        self.fx = Fixture(self.db)
        self.c = self.db.conn

    def test_all_required_tables_exist(self):
        tables = {r[0] for r in self.c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"users", "telegram_accounts", "telegram_sessions", "telegram_groups", "posts", "schedules",
                    "schedule_targets", "posting_jobs", "posting_logs", "settings", "license_activation",
                    "auth_sessions", "rate_limits", "telegram_login_attempts"}
        self.assertLessEqual(required, tables)

    def test_enums_match_check_constraints(self):
        expected = {
            ("telegram_accounts", "status"): models.AccountStatus,
            ("telegram_sessions", "status"): models.SessionStatus,
            ("telegram_groups", "chat_type"): models.ChatType,
            ("telegram_groups", "permission_status"): models.PermissionStatus,
            ("schedules", "kind"): models.ScheduleKind,
            ("schedules", "status"): models.ScheduleStatus,
            ("posting_jobs", "status"): models.JobStatus,
            ("posting_logs", "level"): models.LogLevel,
            ("telegram_login_attempts", "state"): models.LoginState,
            ("post_media", "kind"): models.MediaKind,
            ("post_media", "content_type"): models.MediaContentType,
            ("post_media", "storage_backend"): models.StorageBackend,
        }
        found = {}
        for table, body in re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", SQL, re.S):
            for column, values in re.findall(r"CHECK \((\w+) IN \(([^)]*)\)\)", body):
                found[(table, column)] = set(re.findall(r"'([^']+)'", values))
        self.assertEqual(set(found), set(expected))
        for key, enum in expected.items():
            self.assertEqual(found[key], {e.value for e in enum}, key)

    def test_cross_user_references_are_rejected_by_the_database(self):
        c = self.c
        cases = [
            # schedule of user 1 pointing at user 2's post / account
            ("INSERT INTO schedules (id, user_id, post_id, account_id, kind, timezone) VALUES (?, ?, ?, ?, 'once', 'UTC')",
             (uid(50), uid(1), uid(32), uid(11))),
            ("INSERT INTO schedules (id, user_id, post_id, account_id, kind, timezone) VALUES (?, ?, ?, ?, 'once', 'UTC')",
             (uid(51), uid(1), uid(31), uid(12))),
            # target: user 1's schedule with user 2's group
            ("INSERT INTO schedule_targets (schedule_id, group_id, user_id) VALUES (?, ?, ?)", (uid(41), uid(22), uid(1))),
            # group registered under someone else's account
            ("INSERT INTO telegram_groups (id, user_id, account_id, tg_chat_id, title, chat_type) VALUES (?, ?, ?, 5, 't', 'group')",
             (uid(60), uid(1), uid(12))),
        ]
        for sql, params in cases:
            with self.subTest(sql=sql[:40]):
                with self.assertRaises(sqlite3.IntegrityError):
                    c.execute(sql, params)

    def test_cascade_on_user_delete_removes_everything_of_that_user_only(self):
        c = self.c
        c.execute("DELETE FROM users WHERE id = ?", (uid(1),))
        for table in ("telegram_accounts", "telegram_groups", "posts", "schedules", "schedule_targets"):
            n1 = c.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (uid(1),)).fetchone()[0]
            n2 = c.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (uid(2),)).fetchone()[0]
            self.assertEqual((n1, n2), (0, 1), table)

    def test_email_unique_case_insensitive(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("INSERT INTO users (id, email, password_hash) VALUES (?, 'A@Example.com', 'h')", (uid(3),))

    def test_session_unique_per_account_and_invalid_status_rejected(self):
        c = self.c
        c.execute("INSERT INTO telegram_sessions (id, account_id, session_enc) VALUES (?, ?, 'enc')", (uid(70), uid(11)))
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO telegram_sessions (id, account_id, session_enc) VALUES (?, ?, 'enc')", (uid(71), uid(11)))
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("UPDATE telegram_accounts SET status = 'banana' WHERE id = ?", (uid(11),))

    def _job(self, job_id: int, key: str):
        self.c.execute(
            "INSERT INTO posting_jobs (id, user_id, schedule_id, account_id, group_id, group_title, text_snapshot, "
            "scheduled_for, next_attempt_at, idempotency_key) VALUES (?,?,?,?,?,'G1','text','2026-10-01','2026-10-01',?)",
            (uid(job_id), uid(1), uid(41), uid(11), uid(21), key),
        )

    def test_duplicate_job_protection_and_defaults(self):
        self._job(80, "sched1:group1:2026-10-01T10:00")
        with self.assertRaises(sqlite3.IntegrityError):
            self._job(81, "sched1:group1:2026-10-01T10:00")
        row = self.c.execute("SELECT status, attempts, max_attempts, media_snapshot FROM posting_jobs").fetchone()
        self.assertEqual(tuple(row), ("scheduled", 0, 5, "[]"))

    def test_history_survives_schedule_and_group_deletion(self):
        self._job(82, "k1")
        self.c.execute("DELETE FROM telegram_groups WHERE id = ?", (uid(21),))
        self.c.execute("DELETE FROM schedules WHERE id = ?", (uid(41),))
        row = self.c.execute("SELECT group_id, schedule_id, text_snapshot FROM posting_jobs").fetchone()
        self.assertEqual(tuple(row), (None, None, "text"))

    def test_single_license_row_only(self):
        insert = ("INSERT INTO license_activation (id, license_key_enc, license_id, product, installation_id, status, "
                  "payload_json, signature, activated_at, last_verified_at, high_water_at, updated_at) "
                  "VALUES (?, 'e','l','p','i','active','{}','s',1,1,1,1)")
        self.c.execute(insert, (1,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute(insert, (2,))

    def test_model_from_record(self):
        row = self.c.execute("SELECT * FROM telegram_groups WHERE id = ?", (uid(21),)).fetchone()
        group = models.TelegramGroup.from_record(row)
        self.assertEqual((group.title, group.is_enabled, group.permission_status), ("G1", 0, "unknown"))
        self.assertEqual(len(models.new_id()), 36)


class LicenseStoreTests(unittest.IsolatedAsyncioTestCase):
    def record(self) -> LicenseRecord:
        return LicenseRecord("TAP-AAAAA-BBBBB-CCCCC-DDDDD", "lic-1", "telegram-auto-poster", "inst", "h.example.com",
                             "active", '{"a":1}', "sig", 100, 100, None, "", 100)

    async def test_roundtrip_encrypts_key_at_rest(self):
        db = await migrated_db()
        store = DbLicenseStore(db, Cipher(generate_key()))
        self.assertIsNone(await store.load())
        await store.save(self.record())
        loaded = await store.load()
        self.assertEqual(loaded.license_key, "TAP-AAAAA-BBBBB-CCCCC-DDDDD")
        raw = db.conn.execute("SELECT license_key_enc FROM license_activation").fetchone()[0]
        self.assertNotIn("TAP-AAAAA", raw)
        self.assertNotIn("BBBBB", raw)
        self.assertTrue(raw.startswith("gAAAA"))  # Fernet token
        await store.save(self.record().with_(last_error="unreachable"))  # upsert
        self.assertEqual((await store.load()).last_error, "unreachable")
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM license_activation").fetchone()[0], 1)

    async def test_wrong_encryption_key_cannot_read_license(self):
        db = await migrated_db()
        await DbLicenseStore(db, Cipher(generate_key())).save(self.record())
        with self.assertRaises(DecryptionError):
            await DbLicenseStore(db, Cipher(generate_key())).load()

    async def test_installation_id_is_stable(self):
        db = await migrated_db()
        first = await get_or_create_installation_id(db)
        self.assertEqual(first, await get_or_create_installation_id(db))
        self.assertEqual(len(first), 36)


class LicenseServerSchemaTests(unittest.TestCase):
    def test_server_schema_is_valid_sql(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript((MIGRATIONS_DIR.parent / "license_server" / "schema.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO customers (id, name) VALUES ('c1', 'Acme')")
        conn.execute("INSERT INTO licenses (id, key_hash, key_hint, customer_id, product) VALUES ('l1','h','ABCD','c1','p')")
        self.assertEqual(conn.execute("SELECT max_activations, status FROM licenses").fetchone(), (1, "active"))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO licenses (id, key_hash, key_hint, customer_id, product, max_activations) "
                         "VALUES ('l2','h2','EFGH','c1','p', 0)")


if __name__ == "__main__":
    unittest.main()
