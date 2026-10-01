import asyncio
import unittest

from app.telegram.errors import (CodeExpired, FloodWait, InvalidApiCredentials, InvalidCode, InvalidPassword,
                                 InvalidPhone, NetworkProblem, PasswordRequired, PhoneBanned, SessionRevoked,
                                 TelegramError, map_telethon_exception)
from app.telegram.service import TelegramClientService, mask_phone, normalize_phone
from tests.fake_telegram import GOOD_API_HASH, GOOD_API_ID, FakeTelegram

PHONE = "+8801712345678"


def named(name, **attrs):
    return type(name, (Exception,), {})(), attrs


class PhoneTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_phone(" +880 1712-345678 "), PHONE)
        self.assertEqual(normalize_phone("+1 (415) 555-0100"), "+14155550100")
        for bad in ("", "01712345678", "+0123456789", "+12", "+" + "1" * 20, "abc", "+88017abc45678"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_phone(bad)

    def test_mask_never_reveals_the_number(self):
        masked = mask_phone(PHONE)
        self.assertTrue(masked.startswith("+88") and masked.endswith("78"))
        self.assertNotIn("1712345", masked)
        self.assertEqual(len(masked), len(PHONE))


class ExceptionMappingTests(unittest.TestCase):
    def make(self, name, **attrs):
        exc = type(name, (Exception,), {})()
        for k, v in attrs.items():
            setattr(exc, k, v)
        return exc

    def test_telethon_errors_are_translated_by_name(self):
        cases = {
            "PhoneNumberInvalidError": InvalidPhone, "PhoneNumberBannedError": PhoneBanned,
            "ApiIdInvalidError": InvalidApiCredentials, "PhoneCodeInvalidError": InvalidCode,
            "PhoneCodeExpiredError": CodeExpired, "SessionPasswordNeededError": PasswordRequired,
            "PasswordHashInvalidError": InvalidPassword, "AuthKeyUnregisteredError": SessionRevoked,
            "SessionRevokedError": SessionRevoked, "UserDeactivatedBanError": SessionRevoked,
        }
        for name, expected in cases.items():
            self.assertIsInstance(map_telethon_exception(self.make(name)), expected, name)

    def test_flood_wait_carries_seconds(self):
        mapped = map_telethon_exception(self.make("FloodWaitError", seconds=87))
        self.assertIsInstance(mapped, FloodWait)
        self.assertEqual(mapped.seconds, 87)
        self.assertIsInstance(map_telethon_exception(self.make("FloodWaitError")), FloodWait)  # no seconds attr

    def test_network_and_unknown_errors(self):
        for exc in (ConnectionError("x"), TimeoutError(), OSError("boom")):
            self.assertIsInstance(map_telethon_exception(exc), NetworkProblem)
        unknown = map_telethon_exception(self.make("WeirdError"))
        self.assertEqual(unknown.code, "telegram_error")
        self.assertNotIn("Weird", str(unknown))  # no details from unknown errors are propagated


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.world = FakeTelegram()
        self.world.add_account(PHONE, 111, code="12345")
        self.svc = self.world.service()

    async def test_send_code_and_sign_in(self):
        sent = await self.svc.send_code(GOOD_API_ID, GOOD_API_HASH, PHONE)
        self.assertTrue(sent.phone_code_hash and sent.session.startswith("pending|"))
        result = await self.svc.sign_in_code(GOOD_API_ID, GOOD_API_HASH, sent.session, PHONE, "12345", sent.phone_code_hash)
        self.assertFalse(result.password_needed)
        self.assertEqual((result.profile.tg_user_id, result.profile.username), (111, "alice"))
        self.assertIn(result.session, self.world.live)

    async def test_errors_from_send_code(self):
        with self.assertRaises(InvalidPhone):
            await self.svc.send_code(GOOD_API_ID, GOOD_API_HASH, "+8801999999999")
        with self.assertRaises(InvalidApiCredentials):
            await self.svc.send_code(1, "0" * 32, PHONE)
        self.world.flood = 30
        with self.assertRaises(FloodWait) as ctx:
            await self.svc.send_code(GOOD_API_ID, GOOD_API_HASH, PHONE)
        self.assertEqual(ctx.exception.seconds, 30)
        self.world.flood = 0
        self.world.accounts[PHONE].banned = True
        with self.assertRaises(PhoneBanned):
            await self.svc.send_code(GOOD_API_ID, GOOD_API_HASH, PHONE)

    async def test_wrong_and_expired_codes(self):
        sent = await self.svc.send_code(GOOD_API_ID, GOOD_API_HASH, PHONE)
        with self.assertRaises(InvalidCode):
            await self.svc.sign_in_code(GOOD_API_ID, GOOD_API_HASH, sent.session, PHONE, "00000", sent.phone_code_hash)
        self.world.expired_hashes.add(sent.phone_code_hash)
        with self.assertRaises(CodeExpired):
            await self.svc.sign_in_code(GOOD_API_ID, GOOD_API_HASH, sent.session, PHONE, "12345", sent.phone_code_hash)

    async def test_two_factor_flow(self):
        self.world.accounts[PHONE].password = "cloud-pass"
        sent = await self.svc.send_code(GOOD_API_ID, GOOD_API_HASH, PHONE)
        step = await self.svc.sign_in_code(GOOD_API_ID, GOOD_API_HASH, sent.session, PHONE, "12345", sent.phone_code_hash)
        self.assertTrue(step.password_needed)
        self.assertIsNone(step.profile)
        with self.assertRaises(InvalidPassword):
            await self.svc.sign_in_password(GOOD_API_ID, GOOD_API_HASH, step.session, "wrong")
        done = await self.svc.sign_in_password(GOOD_API_ID, GOOD_API_HASH, step.session, "cloud-pass")
        self.assertEqual(done.profile.tg_user_id, 111)

    async def test_check_authorization_and_logout(self):
        sent = await self.svc.send_code(GOOD_API_ID, GOOD_API_HASH, PHONE)
        live = (await self.svc.sign_in_code(GOOD_API_ID, GOOD_API_HASH, sent.session, PHONE, "12345", sent.phone_code_hash)).session
        self.assertEqual((await self.svc.check_authorization(GOOD_API_ID, GOOD_API_HASH, live)).tg_user_id, 111)
        await self.svc.log_out(GOOD_API_ID, GOOD_API_HASH, live)
        self.assertIsNone(await self.svc.check_authorization(GOOD_API_ID, GOOD_API_HASH, live))  # revoked
        await self.svc.log_out(GOOD_API_ID, GOOD_API_HASH, live)  # already dead = success, no error

    async def test_every_client_is_disconnected_even_on_failures(self):
        for coro in (self.svc.send_code(1, "0" * 32, PHONE), self.svc.send_code(GOOD_API_ID, GOOD_API_HASH, "+8801999999999"),
                     self.svc.send_code(GOOD_API_ID, GOOD_API_HASH, PHONE)):
            try:
                await coro
            except TelegramError:
                pass
        self.assertEqual(len(self.world.clients), 3)
        self.assertTrue(self.world.assert_all_clients_closed())

    async def test_network_failure_and_timeout_are_mapped_and_cleaned_up(self):
        self.world.down = True
        with self.assertRaises(NetworkProblem):
            await self.svc.send_code(GOOD_API_ID, GOOD_API_HASH, PHONE)
        self.world.down, self.world.connect_delay = False, 1.0
        with self.assertRaises(NetworkProblem):
            await self.world.service(timeout=0.05).send_code(GOOD_API_ID, GOOD_API_HASH, PHONE)
        self.assertTrue(self.world.assert_all_clients_closed())

    async def test_unexpected_exceptions_do_not_leak_details(self):
        class Boom(FakeTelegram):
            def factory(self, *a):
                client = super().factory(*a)

                async def bad(phone):
                    raise RuntimeError("secret detail api_hash=abcdef")

                client.send_code = bad
                return client

        world = Boom()
        with self.assertRaises(TelegramError) as ctx:
            await world.service().send_code(GOOD_API_ID, GOOD_API_HASH, PHONE)
        self.assertNotIn("secret detail", str(ctx.exception))
        self.assertNotIn("abcdef", repr(ctx.exception))
        self.assertTrue(world.assert_all_clients_closed())


if __name__ == "__main__":
    unittest.main()
