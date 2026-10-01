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
