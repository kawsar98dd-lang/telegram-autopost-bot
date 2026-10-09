"""Retry / FloodWait / failure classification: pure functions."""

import unittest

from app.scheduler import policy
from app.telegram import errors as e


class DecisionTests(unittest.TestCase):
    def d(self, exc, attempts=1, maximum=5):
        return policy.decide(exc, attempts=attempts, max_attempts=maximum, rng=lambda: 0.5)

    def test_floodwait_is_obeyed_and_does_not_use_an_attempt(self):
        dec = self.d(e.FloodWait(120))
        self.assertEqual((dec.action, dec.code, dec.counts_attempt), ("defer", "rate_limited", False))
        self.assertGreaterEqual(dec.delay, 120)            # never earlier than Telegram asked
        self.assertLessEqual(dec.delay, 120 + policy.FLOOD_JITTER_MAX_SECONDS)
        self.assertGreaterEqual(self.d(e.FloodWait(86400)).delay, 86400)  # a very long wait is deferred, not slept

    def test_network_problem_before_send_retries_with_bounded_backoff_then_fails(self):
        delays = [self.d(e.NetworkProblem(), attempts=n).delay for n in (1, 2, 3, 4)]
        self.assertEqual(delays, sorted(delays))
        self.assertTrue(all(0 < x <= policy.BACKOFF_CAP_SECONDS * 1.2 for x in delays))
        last = self.d(e.NetworkProblem(), attempts=5)
        self.assertEqual((last.action, last.code), ("fail", "network_error"))

    def test_backoff_has_jitter_and_a_cap(self):
        low, high = policy.backoff_seconds(3, lambda: 0.0), policy.backoff_seconds(3, lambda: 1.0)
        self.assertLess(low, high)
        self.assertEqual(policy.backoff_seconds(30, lambda: 0.5), policy.BACKOFF_CAP_SECONDS)

    def test_definitive_refusals_are_permanent(self):
        for exc, code in ((e.NoPostPermission(), "no_permission"), (e.GroupUnavailable(), "group_unavailable"),
                          (e.InvalidMedia(), "invalid_media"), (e.InvalidMessage(), "message_invalid"),
                          (e.SendRejected(), "rejected")):
            dec = self.d(exc)
            self.assertEqual((dec.action, dec.code), ("fail", code))
            self.assertIn(code, policy.NEEDS_ATTENTION)

    def test_uncertain_and_unknown_are_never_retried(self):
        self.assertEqual(self.d(e.DeliveryUncertain()).action, "uncertain")
        self.assertEqual(self.d(e.TelegramError()).action, "uncertain")  # unknown error during a send: no duplicate risk taken
        self.assertEqual(self.d(e.SessionRevoked()).action, "revoked")

    def test_telethon_error_names_are_classified_conservatively(self):
        class ChatWriteForbiddenError(Exception): ...
        class SlowModeWaitError(Exception):
            seconds = 77
        class FloodWaitError(Exception):
            seconds = 31
        class AuthKeyUnregisteredError(Exception): ...
        class PhotoInvalidDimensionsError(Exception): ...
        class SomethingNewError(Exception): ...
        self.assertIsInstance(e.map_send_exception(ChatWriteForbiddenError()), e.NoPostPermission)
        self.assertEqual(e.map_send_exception(SlowModeWaitError()).seconds, 77)
        self.assertEqual(e.map_send_exception(FloodWaitError()).seconds, 31)
        self.assertIsInstance(e.map_send_exception(AuthKeyUnregisteredError()), e.SessionRevoked)
        self.assertIsInstance(e.map_send_exception(PhotoInvalidDimensionsError()), e.InvalidMedia)
        for exc in (SomethingNewError(), ConnectionResetError(), TimeoutError(), ValueError("x")):
            self.assertIsInstance(e.map_send_exception(exc), e.DeliveryUncertain, type(exc).__name__)

    def test_stored_messages_are_fixed_texts_without_secrets(self):
        for code, text in policy.SAFE_MESSAGES.items():
            self.assertNotRegex(text, r"(?i)api_hash|session_enc|StringSession|traceback|password|\+\d{8}")
        self.assertEqual(policy.safe_message("nonsense"), "The post could not be sent.")


if __name__ == "__main__":
    unittest.main()
