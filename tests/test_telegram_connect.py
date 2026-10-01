import asyncio
import io
import logging
import unittest

from app.config import settings_from_env
from app.security.crypto import Cipher, generate_key
from app.security.redact import RedactingFormatter
from app.telegram.connect import (ATTEMPT_TTL_SECONDS, MAX_FAILED_ATTEMPTS, AccountNotFound, LoginAttemptGone,
                                  TelegramConnectionService, WrongStep, session_context)
from app.telegram.errors import (CodeExpired, InvalidApiCredentials, InvalidCode, InvalidPassword, InvalidPhone,
                                 NetworkProblem)
from tests.fake_telegram import GOOD_API_HASH, GOOD_API_ID, FakeTelegram
from tests.support import Clock, migrated_db
from tests.web_support import BASE_ENV

PHONE, PHONE2 = "+8801712345678", "+8801812345678"
CODE, PASSWORD = "48213", "my-cloud-2fa-Pa55"
API_ID = str(GOOD_API_ID)


def dump_database(db) -> str:
    """Every value of every table as one string: used to prove that a secret is nowhere in the database."""
    parts = []
    for (table,) in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in db.conn.execute(f'SELECT * FROM "{table}"').fetchall():
            parts.extend(str(v) for v in tuple(row))
    return "\n".join(parts)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self, env_extra=None):
        self.db = await migrated_db()
        self.clock = Clock()
        self.cipher = Cipher(generate_key())
        self.world = FakeTelegram()
        self.world.add_account(PHONE, 111, code=CODE, username="alice", first_name="Alice")
        self.world.add_account(PHONE2, 222, code=CODE, username="bob", first_name="Bob")
        self.settings = settings_from_env({**BASE_ENV, **(env_extra or {})})
        self.svc = TelegramConnectionService(self.db, self.cipher, self.world.service(), self.settings, self.clock)
        for n, email in ((1, "a@example.com"), (2, "b@example.com")):
            self.db.conn.execute("INSERT INTO users (id, email, password_hash) VALUES (?, ?, 'h')", (f"user{n}", email))
        self.db.conn.commit()

    async def connect(self, user="user1", phone=PHONE, password=None):
        attempt = await self.svc.start(user, phone, API_ID, GOOD_API_HASH)
        outcome = await self.svc.submit_code(user, attempt, CODE)
        if outcome.password_needed:
            outcome = await self.svc.submit_password(user, attempt, password)
        return outcome.account_id

    def rows(self, sql, *args):
        return self.db.conn.execute(sql, args).fetchall()


class LoginFlowTests(Base):
    async def test_add_account_sends_a_code_and_keeps_only_encrypted_pending_state(self):
        attempt = await self.svc.start("user1", "+880 1712-345678", API_ID, GOOD_API_HASH)
        row = self.rows("SELECT * FROM telegram_login_attempts")[0]
        self.assertEqual((row["id"], row["user_id"], row["state"], row["failed_attempts"]), (attempt, "user1", "code_sent", 0))
        self.assertEqual(row["phone_masked"][:3], "+88")
        self.assertEqual(row["expires_at"], int(self.clock()) + ATTEMPT_TTL_SECONDS)
        everything = dump_database(self.db)
        for secret in (PHONE, PHONE[1:], GOOD_API_HASH, API_ID, "pending|", "hash"):
            self.assertNotIn(secret, everything, secret)  # phone, API creds, session and code hash: all encrypted
        for column in ("api_id_enc", "api_hash_enc", "phone_enc", "phone_code_hash_enc", "pending_session_enc"):
            self.assertTrue(row[column].startswith("gAAAA"), column)
        pending = await self.svc.pending("user1", attempt)
        self.assertEqual((pending.state, pending.remaining_attempts), ("code_sent", MAX_FAILED_ATTEMPTS))

    async def test_successful_code_verification_creates_the_account_and_an_encrypted_session(self):
        account_id = await self.connect()
        (acc,) = self.rows("SELECT * FROM telegram_accounts")
        self.assertEqual((acc["id"], acc["user_id"], acc["tg_user_id"], acc["username"], acc["first_name"], acc["status"]),
                         (account_id, "user1", 111, "alice", "Alice", "connected"))
        self.assertEqual(acc["phone_masked"][:3] + acc["phone_masked"][-2:], "+8878")
        self.assertNotIn(PHONE, dump_database(self.db))
        (sess,) = self.rows("SELECT * FROM telegram_sessions")
        self.assertEqual((sess["account_id"], sess["status"]), (account_id, "active"))
        live = next(iter(self.world.live))
        self.assertNotIn(live, dump_database(self.db))  # the raw session string is nowhere in the database
        self.assertTrue(sess["session_enc"].startswith("gAAAA"))
        self.assertEqual(self.cipher.decrypt(sess["session_enc"], session_context("user1", account_id)), live)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_login_attempts")[0][0], 0)  # attempt cleaned up

    async def test_invalid_code_counts_and_five_failures_end_the_attempt(self):
        attempt = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        for used in range(1, MAX_FAILED_ATTEMPTS):
            with self.assertRaises(InvalidCode):
                await self.svc.submit_code("user1", attempt, "00000")
            self.assertEqual((await self.svc.pending("user1", attempt)).remaining_attempts, MAX_FAILED_ATTEMPTS - used)
        with self.assertRaises(LoginAttemptGone) as ctx:
            await self.svc.submit_code("user1", attempt, "00000")
        self.assertEqual(ctx.exception.reason, "too_many_failures")
        with self.assertRaises(LoginAttemptGone):
            await self.svc.submit_code("user1", attempt, CODE)  # even the right code is useless now
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_accounts")[0][0], 0)

    async def test_wrong_code_then_right_code_still_works(self):
        attempt = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        with self.assertRaises(InvalidCode):
            await self.svc.submit_code("user1", attempt, "11111")
        with self.assertRaises(InvalidCode):
            await self.svc.submit_code("user1", attempt, "12")  # malformed input never reaches Telegram
        self.assertEqual(len([c for c in self.world.calls if c[0] == "sign_in_code"]), 1)
        self.assertTrue((await self.svc.submit_code("user1", attempt, " 482-13 ")).account_id)

    async def test_expired_code_removes_the_attempt(self):
        attempt = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        self.world.expired_hashes.update(self.world.code_hashes)
        with self.assertRaises(CodeExpired):
            await self.svc.submit_code("user1", attempt, CODE)
        with self.assertRaises(LoginAttemptGone):
            await self.svc.pending("user1", attempt)

    async def test_attempts_expire_by_time(self):
        attempt = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        self.clock.advance(ATTEMPT_TTL_SECONDS + 1)
        with self.assertRaises(LoginAttemptGone) as ctx:
            await self.svc.submit_code("user1", attempt, CODE)
        self.assertEqual(ctx.exception.reason, "expired")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_login_attempts")[0][0], 0)

    async def test_purge_removes_only_expired_attempts(self):
        await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        self.clock.advance(ATTEMPT_TTL_SECONDS - 100)
        await self.svc.start("user2", PHONE2, API_ID, GOOD_API_HASH)
        self.clock.advance(200)
        await self.svc.purge_expired()
        self.assertEqual([r["user_id"] for r in self.rows("SELECT user_id FROM telegram_login_attempts")], ["user2"])

    async def test_only_one_login_in_progress_per_user(self):
        first = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        second = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        with self.assertRaises(LoginAttemptGone):
            await self.svc.pending("user1", first)
        self.assertTrue(await self.svc.pending("user1", second))

    async def test_cancel(self):
        attempt = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        await self.svc.cancel("user1", attempt)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_login_attempts")[0][0], 0)


class TwoFactorTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.world.accounts[PHONE].password = PASSWORD

    async def test_password_is_requested_then_accepted(self):
        attempt = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        step = await self.svc.submit_code("user1", attempt, CODE)
        self.assertTrue(step.password_needed)
        self.assertEqual((await self.svc.pending("user1", attempt)).state, "password_needed")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_accounts")[0][0], 0)  # not connected yet
        with self.assertRaises(WrongStep):
            await self.svc.submit_code("user1", attempt, CODE)
        done = await self.svc.submit_password("user1", attempt, PASSWORD)
        self.assertTrue(done.account_id)
        self.assertEqual(self.rows("SELECT status FROM telegram_accounts")[0][0], "connected")

    async def test_invalid_password_counts_and_locks_after_five(self):
        attempt = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        await self.svc.submit_code("user1", attempt, CODE)
        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            with self.assertRaises(InvalidPassword):
                await self.svc.submit_password("user1", attempt, "wrong")
        with self.assertRaises(LoginAttemptGone):
            await self.svc.submit_password("user1", attempt, "wrong")
        with self.assertRaises(LoginAttemptGone):
            await self.svc.submit_password("user1", attempt, PASSWORD)

    async def test_password_step_requires_the_code_step_first(self):
        attempt = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        with self.assertRaises(WrongStep):
            await self.svc.submit_password("user1", attempt, PASSWORD)

    async def test_neither_the_code_nor_the_2fa_password_is_ever_persisted_or_logged(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(name)s %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            attempt = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
            with self.assertRaises(InvalidCode):
                await self.svc.submit_code("user1", attempt, "99999")
            await self.svc.submit_code("user1", attempt, CODE)
            with self.assertRaises(InvalidPassword):
                await self.svc.submit_password("user1", attempt, "wrong-password-xyz")
            await self.svc.submit_password("user1", attempt, PASSWORD)
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        stored, logged = dump_database(self.db), stream.getvalue()
        live = next(iter(self.world.live))
        for secret in (CODE, "99999", PASSWORD, "wrong-password-xyz"):
            self.assertNotIn(secret, stored, secret)
            self.assertNotIn(secret, logged, secret)
        for secret in (GOOD_API_HASH, live, PHONE):
            self.assertNotIn(secret, logged, secret)
            self.assertNotIn(secret, stored, secret)


class ReconnectAndDisconnectTests(Base):
    async def test_connecting_an_already_known_account_reuses_the_record(self):
        first = await self.connect()
        first_session = self.rows("SELECT session_enc FROM telegram_sessions")[0][0]
        second = await self.connect()
        self.assertEqual(first, second)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_accounts")[0][0], 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_sessions")[0][0], 1)
        self.assertNotEqual(self.rows("SELECT session_enc FROM telegram_sessions")[0][0], first_session)
        usable = await self.svc.load_session("user1", first)
        self.assertIn(usable.session, self.world.live)

    async def test_the_same_telegram_account_can_belong_to_two_app_users_independently(self):
        a = await self.connect("user1", PHONE)
        b = await self.connect("user2", PHONE)
        self.assertNotEqual(a, b)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_accounts")[0][0], 2)

    async def test_disconnect_logs_out_on_telegram_and_removes_everything_secret(self):
        account_id = await self.connect()
        self.assertTrue(await self.svc.disconnect("user1", account_id))
        self.assertEqual(self.world.live, set())  # revoked on Telegram's side
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_sessions")[0][0], 0)
        (acc,) = self.rows("SELECT * FROM telegram_accounts")
        self.assertEqual((acc["status"], acc["api_id_enc"], acc["api_hash_enc"]), ("disconnected", None, None))
        with self.assertRaises(AccountNotFound):
            await self.svc.load_session("user1", account_id)
        self.assertFalse((await self.svc.list_accounts("user1"))[0].get("session_enc"))
        self.assertTrue(self.world.assert_all_clients_closed())

    async def test_disconnect_is_idempotent_and_survives_telegram_being_unreachable(self):
        account_id = await self.connect()
        self.world.down = True
        self.assertFalse(await self.svc.disconnect("user1", account_id))  # could not reach Telegram ...
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_sessions")[0][0], 0)  # ... local copy is gone anyway
        self.world.down = False
        self.assertFalse(await self.svc.disconnect("user1", account_id))  # second call: nothing left, no error

    async def test_remove_deletes_the_account_record(self):
        account_id = await self.connect()
        await self.svc.remove("user1", account_id)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_accounts")[0][0], 0)
        self.assertEqual(self.world.live, set())

    async def test_revoked_session_is_detected_by_check(self):
        account_id = await self.connect()
        self.assertTrue(await self.svc.check("user1", account_id))
        self.world.revoke_everything()  # user terminated the session from the Telegram app
        self.assertFalse(await self.svc.check("user1", account_id))
        (acc,) = self.rows("SELECT status, api_hash_enc FROM telegram_accounts")
        self.assertEqual(acc["status"], "session_expired")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_sessions")[0][0], 0)
        with self.assertRaises(AccountNotFound):
            await self.svc.load_session("user1", account_id)

    async def test_check_failing_because_of_the_network_changes_nothing(self):
        account_id = await self.connect()
        self.world.down = True
        with self.assertRaises(NetworkProblem):
            await self.svc.check("user1", account_id)
        self.assertEqual(self.rows("SELECT status FROM telegram_accounts")[0][0], "connected")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_sessions")[0][0], 1)


class BindingAndIsolationTests(Base):
    async def test_a_session_cannot_be_used_as_another_users_session(self):
        a = await self.connect("user1", PHONE)
        b = await self.connect("user2", PHONE2)
        # Attacker with database access copies user1's ciphertext into user2's row:
        stolen = self.rows("SELECT session_enc FROM telegram_sessions WHERE account_id = ?", a)[0][0]
        self.db.conn.execute("UPDATE telegram_sessions SET session_enc = ? WHERE account_id = ?", (stolen, b))
        self.db.conn.commit()
        with self.assertRaises(AccountNotFound):
            await self.svc.load_session("user2", b)
        self.assertTrue(await self.svc.load_session("user1", a))  # the original is unaffected

    async def test_a_session_cannot_be_moved_between_accounts_of_the_same_user(self):
        a = await self.connect("user1", PHONE)
        b = await self.connect("user1", PHONE2)
        token = self.rows("SELECT session_enc FROM telegram_sessions WHERE account_id = ?", a)[0][0]
        self.db.conn.execute("UPDATE telegram_sessions SET session_enc = ? WHERE account_id = ?", (token, b))
        self.db.conn.commit()
        with self.assertRaises(AccountNotFound):
            await self.svc.load_session("user1", b)

    async def test_api_credentials_are_bound_too(self):
        a = await self.connect("user1", PHONE)
        b = await self.connect("user2", PHONE2)
        row = self.rows("SELECT api_hash_enc FROM telegram_accounts WHERE id = ?", a)[0][0]
        self.db.conn.execute("UPDATE telegram_accounts SET api_hash_enc = ? WHERE id = ?", (row, b))
        self.db.conn.commit()
        with self.assertRaises(AccountNotFound):
            await self.svc.load_session("user2", b)

    async def test_wrong_encryption_key_makes_sessions_unusable(self):
        a = await self.connect()
        other = TelegramConnectionService(self.db, Cipher(generate_key()), self.world.service(), self.settings, self.clock)
        with self.assertRaises(AccountNotFound):
            await other.load_session("user1", a)

    async def test_users_cannot_touch_each_others_accounts_or_attempts(self):
        a = await self.connect("user1", PHONE)
        attempt = await self.svc.start("user1", PHONE2, API_ID, GOOD_API_HASH)
        with self.assertRaises(AccountNotFound):
            await self.svc.load_session("user2", a)
        for action in (self.svc.disconnect, self.svc.check, self.svc.remove):
            with self.assertRaises(AccountNotFound):
                await action("user2", a)
        with self.assertRaises(LoginAttemptGone):
            await self.svc.submit_code("user2", attempt, CODE)
        with self.assertRaises(LoginAttemptGone):
            await self.svc.pending("user2", attempt)
        await self.svc.cancel("user2", attempt)  # silently ignored: does not delete user1's attempt
        self.assertTrue(await self.svc.pending("user1", attempt))
        self.assertEqual(await self.svc.list_accounts("user2"), [])
        self.assertEqual(self.rows("SELECT status FROM telegram_accounts WHERE id = ?", a)[0][0], "connected")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_sessions")[0][0], 1)

    async def test_an_attempt_copied_to_another_user_is_undecryptable(self):
        attempt = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        self.db.conn.execute("UPDATE telegram_login_attempts SET user_id = 'user2' WHERE id = ?", (attempt,))
        self.db.conn.commit()
        with self.assertRaises(Exception):
            await self.svc.submit_code("user2", attempt, CODE)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_accounts")[0][0], 0)

    async def test_listing_returns_metadata_only(self):
        await self.connect()
        (item,) = await self.svc.list_accounts("user1")
        self.assertEqual(set(item), {"id", "tg_user_id", "username", "first_name", "phone_masked", "status",
                                     "connected_at", "last_seen_at"})
        self.assertNotIn(PHONE, str(item))

    async def test_usable_session_repr_hides_secrets(self):
        a = await self.connect()
        usable = await self.svc.load_session("user1", a)
        self.assertNotIn(usable.session, repr(usable))
        self.assertNotIn(usable.api_hash, repr(usable))
        self.assertNotIn(usable.api_hash, str([usable]))


class ApiCredentialTests(Base):
    async def test_invalid_credential_formats_are_rejected_before_contacting_telegram(self):
        for api_id, api_hash in (("", ""), ("abc", GOOD_API_HASH), (API_ID, "short"), (API_ID, "x" * 32 + "!"),
                                 ("1" * 13, GOOD_API_HASH), (API_ID, "")):
            with self.assertRaises(InvalidApiCredentials, msg=(api_id, api_hash)):
                await self.svc.start("user1", PHONE, api_id, api_hash)
        self.assertEqual(self.world.calls, [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_login_attempts")[0][0], 0)

    async def test_telegram_rejecting_the_credentials_is_reported_and_nothing_is_stored(self):
        with self.assertRaises(InvalidApiCredentials):
            await self.svc.start("user1", PHONE, "99999", "a" * 32)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_login_attempts")[0][0], 0)

    async def test_server_default_credentials_are_used_when_the_form_is_empty_and_stored_encrypted(self):
        await self.asyncSetUp({"TELEGRAM_API_ID": API_ID, "TELEGRAM_API_HASH": GOOD_API_HASH})
        account_id = await self.connect_with_defaults()
        self.assertNotIn(GOOD_API_HASH, dump_database(self.db))
        usable = await self.svc.load_session("user1", account_id)
        self.assertEqual((usable.api_id, usable.api_hash), (GOOD_API_ID, GOOD_API_HASH))

    async def connect_with_defaults(self):
        attempt = await self.svc.start("user1", PHONE, "", "")
        return (await self.svc.submit_code("user1", attempt, CODE)).account_id

    async def test_empty_form_without_server_defaults_fails(self):
        with self.assertRaises(InvalidApiCredentials):
            await self.svc.start("user1", PHONE, "", "")

    async def test_phone_validation(self):
        for bad in ("", "12345", "+abc", "0171234"):
            with self.assertRaises(InvalidPhone):
                await self.svc.start("user1", bad, API_ID, GOOD_API_HASH)
        with self.assertRaises(InvalidPhone):  # well-formed but unknown to Telegram
            await self.svc.start("user1", "+8801999999999", API_ID, GOOD_API_HASH)


class FailureRecoveryTests(Base):
    async def test_network_failure_while_sending_the_code_leaves_no_state_and_can_be_retried(self):
        self.world.down = True
        with self.assertRaises(NetworkProblem):
            await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM telegram_login_attempts")[0][0], 0)
        self.world.down = False
        self.assertTrue(await self.connect())

    async def test_network_failure_while_verifying_keeps_the_attempt_and_does_not_count_as_a_wrong_code(self):
        attempt = await self.svc.start("user1", PHONE, API_ID, GOOD_API_HASH)
        self.world.down = True
        with self.assertRaises(NetworkProblem):
            await self.svc.submit_code("user1", attempt, CODE)
        self.world.down = False
        self.assertEqual((await self.svc.pending("user1", attempt)).failed_attempts, 0)
        self.assertTrue((await self.svc.submit_code("user1", attempt, CODE)).account_id)

    async def test_no_client_is_left_connected_after_any_flow(self):
        await self.connect()
        with self.assertRaises(InvalidCode):
            attempt = await self.svc.start("user1", PHONE2, API_ID, GOOD_API_HASH)
            await self.svc.submit_code("user1", attempt, "00000")
        self.assertGreater(len(self.world.clients), 3)
        self.assertTrue(self.world.assert_all_clients_closed())


if __name__ == "__main__":
    unittest.main()
