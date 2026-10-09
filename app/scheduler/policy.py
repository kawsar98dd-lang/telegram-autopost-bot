"""Retry / FloodWait / failure policy of the posting worker, in ONE place (pure functions, easy to test).

Principles
----------
* A message is only ever re-sent when it is CERTAIN that the previous request did not deliver it (Telegram answered with a
  definitive refusal, or the connection failed before the request was sent).
* When the outcome is unknown the job becomes ``failed`` with delivery_state ``uncertain``: a person decides.
* Telegram's FloodWait / slow-mode wait is obeyed exactly (plus a little jitter); it does not use up an attempt.
* Everything else that can be retried is retried a bounded number of times with exponential back-off and jitter.
* Only fixed, safe texts are stored and shown. Exception texts are never persisted.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from ..telegram.errors import (DeliveryUncertain, FloodWait, GroupUnavailable, InvalidMedia, InvalidMessage, NetworkProblem,
                               NoPostPermission, SendRejected, SessionRevoked, TelegramError)

MAX_ATTEMPTS = 5                    # default of posting_jobs.max_attempts (migration 0001)
BACKOFF_BASE_SECONDS = 30.0
BACKOFF_CAP_SECONDS = 900.0         # 15 minutes
JITTER_FRACTION = 0.2               # +-20 % on back-off, up to +5 s on a FloodWait
FLOOD_JITTER_MAX_SECONDS = 5.0
LEASE_SECONDS = 300                 # a claimed job must be finished (or extended) within this time
ACCOUNT_LOCK_SECONDS = 300          # same as the lease: a dead worker frees its account as soon as its jobs are recovered
CLAIM_BATCH = 20                    # jobs claimed per account per round
MISSED_AFTER_SECONDS = 6 * 3600     # an occurrence that could not start within 6 hours is recorded as missed, not sent late
STALE_RECOVERY_SECONDS = 30         # how often the worker looks for expired leases

# Codes that mean "a person has to do something" (shown with an attention badge, never retried automatically).
NEEDS_ATTENTION = frozenset({
    "no_permission", "group_unavailable", "target_missing", "account_unavailable", "session_revoked", "invalid_media",
    "media_missing", "message_invalid", "delivery_uncertain", "rejected", "account_mismatch", "worker_crashed",
})

SAFE_MESSAGES = {
    "no_permission": "Telegram does not allow this account to post in the group. Check the account's rights in the group.",
    "group_unavailable": "The group is not available to this account any more (left, removed or deleted).",
    "target_missing": "The target group no longer exists in the application.",
    "account_unavailable": "The Telegram account is not connected. Reconnect it, then retry the job.",
    "account_mismatch": "The job does not belong to this Telegram account.",
    "session_revoked": "Telegram ended the account's session. Reconnect the account, then retry the job.",
    "invalid_media": "Telegram did not accept the image.",
    "media_missing": "The stored image could not be found.",
    "message_invalid": "The final message (with the automatic footer) does not fit Telegram's limits.",
    "network_error": "Telegram could not be reached. Gave up after several attempts; nothing was sent.",
    "rate_limited": "Telegram asked to wait (rate limit). The post will be retried automatically.",
    "retrying": "A temporary problem occurred. The post will be retried automatically.",
    "delivery_uncertain": "It is not certain whether Telegram delivered this post. Check the group, then choose what to do.",
    "rejected": "Telegram refused the post.",
    "missed": "The worker could not run this on time (it was not running), so it was not sent late.",
    "worker_crashed": "The worker stopped repeatedly while processing this job. Nothing was confirmed as sent.",
    "cancelled": "Cancelled before it was sent.",
}


def safe_message(code: str) -> str:
    return SAFE_MESSAGES.get(code, "The post could not be sent.")


@dataclass(frozen=True)
class Decision:
    action: str          # retry | defer | fail | uncertain | revoked
    code: str
    delay: float = 0.0   # seconds until the next attempt (retry / defer)
    counts_attempt: bool = True


def backoff_seconds(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """30 s, 60 s, 120 s ... capped at 15 min, with +-20 % jitter. ``attempt`` is the number of attempts already used (>= 1)."""
    base = min(BACKOFF_BASE_SECONDS * (2 ** max(attempt - 1, 0)), BACKOFF_CAP_SECONDS)
    return base * (1 - JITTER_FRACTION + 2 * JITTER_FRACTION * rng())


def decide(exc: TelegramError, *, attempts: int, max_attempts: int, rng: Callable[[], float] = random.random) -> Decision:
    """What to do with a job after a Telegram error. ``attempts`` already includes the attempt that just failed."""
    if isinstance(exc, FloodWait):
        return Decision("defer", "rate_limited", exc.seconds + rng() * FLOOD_JITTER_MAX_SECONDS, counts_attempt=False)
    if isinstance(exc, SessionRevoked):
        return Decision("revoked", "session_revoked")
    if isinstance(exc, DeliveryUncertain):
        return Decision("uncertain", "delivery_uncertain")
    if isinstance(exc, NetworkProblem):  # raised only BEFORE a request is sent: certain that nothing was delivered
        if attempts >= max_attempts:
            return Decision("fail", "network_error")
        return Decision("retry", "retrying", backoff_seconds(attempts, rng))
    for cls, code in ((NoPostPermission, "no_permission"), (GroupUnavailable, "group_unavailable"),
                      (InvalidMedia, "invalid_media"), (InvalidMessage, "message_invalid")):
        if isinstance(exc, cls):
            return Decision("fail", code)
    if isinstance(exc, SendRejected):
        return Decision("fail", "rejected")
    return Decision("uncertain", "delivery_uncertain")  # an unclassified error during a send: never risk a duplicate
