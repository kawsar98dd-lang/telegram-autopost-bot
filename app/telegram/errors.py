"""Telegram-related errors, independent of Telethon (so they can be tested without it)."""

from __future__ import annotations


class TelegramError(Exception):
    """Base class. ``code`` is a short machine-readable reason; messages never contain secrets."""

    code = "telegram_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class InvalidPhone(TelegramError):
    code = "invalid_phone"


class PhoneBanned(TelegramError):
    code = "phone_banned"


class InvalidApiCredentials(TelegramError):
    code = "invalid_api_credentials"


class InvalidCode(TelegramError):
    code = "invalid_code"


class CodeExpired(TelegramError):
    code = "code_expired"


class InvalidPassword(TelegramError):
    code = "invalid_password"


class PasswordRequired(TelegramError):
    code = "password_required"


class SessionRevoked(TelegramError):
    """The account's session was ended (for example from the Telegram app) or is no longer valid."""

    code = "session_revoked"


class NetworkProblem(TelegramError):
    code = "network"


class FloodWait(TelegramError):
    code = "flood_wait"

    def __init__(self, seconds: int) -> None:
        self.seconds = max(int(seconds), 1)
        super().__init__(f"flood wait {self.seconds}s")


def map_telethon_exception(exc: BaseException) -> TelegramError:
    """Translate a Telethon exception into ours by class name (no Telethon import needed)."""
    name = type(exc).__name__
    if name.startswith("FloodWait") or name in ("FloodError", "PhoneNumberFloodError"):
        return FloodWait(getattr(exc, "seconds", 60) or 60)
    table = {
        "PhoneNumberInvalidError": InvalidPhone,
        "PhoneNumberBannedError": PhoneBanned,
        "PhoneNumberUnoccupiedError": InvalidPhone,
        "ApiIdInvalidError": InvalidApiCredentials,
        "ApiIdPublishedFloodError": InvalidApiCredentials,
        "PhoneCodeInvalidError": InvalidCode,
        "PhoneCodeEmptyError": InvalidCode,
        "PhoneCodeExpiredError": CodeExpired,
        "SessionPasswordNeededError": PasswordRequired,
        "PasswordHashInvalidError": InvalidPassword,
        "AuthKeyUnregisteredError": SessionRevoked,
        "AuthKeyInvalidError": SessionRevoked,
        "SessionRevokedError": SessionRevoked,
        "SessionExpiredError": SessionRevoked,
        "UserDeactivatedError": SessionRevoked,
        "UserDeactivatedBanError": SessionRevoked,
    }
    if name in table:
        return table[name]()
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or name in ("TimeoutError",):
        return NetworkProblem()
    return TelegramError()  # unknown: deliberately no details (they could contain sensitive text)


# ---- sending (Step 6) ---------------------------------------------------------------------------------------------
# Every class below describes the outcome of ONE send request. The crucial distinction is whether Telegram ANSWERED:
#   * a definitive answer (an RPC error) means the message was NOT delivered by that request  -> safe to decide on;
#   * no answer (connection lost / timeout after the request left)                             -> DeliveryUncertain.
class NoPostPermission(TelegramError):
    """Telegram refused the message because the account may not write in this chat."""

    code = "no_permission"


class GroupUnavailable(TelegramError):
    """The chat does not exist for this account any more (left, kicked, deleted, private)."""

    code = "group_unavailable"


class InvalidMedia(TelegramError):
    code = "invalid_media"


class InvalidMessage(TelegramError):
    code = "message_invalid"


class SendRejected(TelegramError):
    """Telegram answered with an error that is not in our table: a definitive failure of this request."""

    code = "rejected"


class DeliveryUncertain(TelegramError):
    """The request may or may not have been executed by Telegram (no definitive answer). NEVER retried automatically."""

    code = "delivery_uncertain"


_SEND_DEFINITIVE = {
    "ChatWriteForbiddenError": NoPostPermission, "UserBannedInChannelError": NoPostPermission,
    "ChatAdminRequiredError": NoPostPermission, "ChannelBannedError": NoPostPermission,
    "ChatRestrictedError": NoPostPermission, "UserNotParticipantError": NoPostPermission,
    "ChatSendMediaForbiddenError": NoPostPermission, "ChatSendPhotosForbiddenError": NoPostPermission,
    "ChatGuestSendForbiddenError": NoPostPermission, "TopicClosedError": NoPostPermission,
    "ChannelPrivateError": GroupUnavailable, "ChatIdInvalidError": GroupUnavailable, "PeerIdInvalidError": GroupUnavailable,
    "ChannelInvalidError": GroupUnavailable, "ChatInvalidError": GroupUnavailable,
    "PhotoInvalidDimensionsError": InvalidMedia, "PhotoSaveFileInvalidError": InvalidMedia, "MediaEmptyError": InvalidMedia,
    "ImageProcessFailedError": InvalidMedia, "PhotoExtInvalidError": InvalidMedia, "PhotoInvalidError": InvalidMedia,
    "FilePartsInvalidError": InvalidMedia, "FilePartInvalidError": InvalidMedia, "MediaInvalidError": InvalidMedia,
    "MessageTooLongError": InvalidMessage, "MediaCaptionTooLongError": InvalidMessage, "MessageEmptyError": InvalidMessage,
}
_SEND_AUTH = ("AuthKeyUnregisteredError", "AuthKeyInvalidError", "SessionRevokedError", "SessionExpiredError",
              "UserDeactivatedError", "UserDeactivatedBanError")


def map_send_exception(exc: BaseException) -> TelegramError:
    """Translate an exception raised BY A SEND REQUEST into one of our errors, by class name (no Telethon import).

    Unknown exceptions are deliberately mapped to DeliveryUncertain: when in doubt the message is NOT sent a second time.
    """
    if isinstance(exc, TelegramError):
        return exc
    name = type(exc).__name__
    if "FloodWait" in name or "SlowModeWait" in name or name in ("FloodError", "FloodPremiumWaitError"):
        return FloodWait(getattr(exc, "seconds", 60) or 60)
    if name in _SEND_AUTH:
        return SessionRevoked()
    if name in _SEND_DEFINITIVE:
        return _SEND_DEFINITIVE[name]()
    return DeliveryUncertain()
