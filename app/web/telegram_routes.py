"""Telegram account pages: add (phone + API credentials), verify (code, optional 2FA), manage."""

from __future__ import annotations

import logging

from ..auth.ratelimit import key_part
from ..telegram.connect import AccountNotFound, LoginAttemptGone, WrongStep
from ..telegram.errors import FloodWait, TelegramError
from .errors import HttpError
from .http import Request, Response, redirect
from .pipeline import Auth, Route
from .views import limit_check, render

log = logging.getLogger(__name__)

SEND_LIMIT, SEND_WINDOW = 5, 3600  # login codes per user per hour
VERIFY_LIMIT, VERIFY_WINDOW = 20, 15 * 60

ERRORS = {
    "invalid_phone": (400, "That phone number is not valid. Use the international format, for example +8801XXXXXXXXX."),
    "phone_banned": (400, "Telegram does not allow this phone number to sign in."),
    "invalid_api_credentials": (400, "Telegram did not accept the API ID or API hash. Check them at my.telegram.org/apps."),
    "invalid_code": (400, "That code is not correct. Check the newest message from Telegram and try again."),
    "code_expired": (400, "The code has expired. Start again to receive a new one."),
    "invalid_password": (400, "That password is not correct."),
    "session_revoked": (400, "This Telegram session is no longer valid."),
    "network": (503, "Telegram could not be reached. Check the server's internet connection and try again."),
}
GONE = {
    "not_found": "This login is no longer available. Start again.",
    "expired": "This login expired. Start again to receive a new code.",
    "too_many_failures": "Too many wrong attempts. Start again to receive a new code.",
}
NOTICES = {
    "connected": "Telegram account connected.",
    "disconnected": "Telegram account disconnected. The session was removed from this server.",
    "removed": "Telegram account removed.",
    "checked": "The session is valid.",
    "expired": "Telegram no longer accepts this session. Please connect the account again.",
}


def _svc(request: Request):
    return request.ctx.telegram_connect


def _describe(exc: TelegramError) -> tuple[int, str, int | None]:
    """Fixed, safe texts only: nothing that Telegram (or an exception) said is ever shown."""
    if isinstance(exc, FloodWait):
        return 429, f"Telegram asks you to wait {exc.seconds} seconds before trying again.", exc.seconds
    status, message = ERRORS.get(exc.code, (502, "Telegram returned an unexpected answer. Please try again later."))
    return status, message, None


def _with_retry(response: Response, retry: int | None) -> Response:
    return response.header("Retry-After", str(retry)) if retry else response


async def _accounts_page(request: Request, status: int = 200, error: str = "") -> Response:
    notice = NOTICES.get(request.query.get("notice", ""), "")
    accounts = await _svc(request).list_accounts(request.user.id)
    return render(request, "telegram_accounts.html", status, active="telegram", accounts=accounts,
                  notice=notice, error=error)


async def accounts_get(request: Request) -> Response:
    return await _accounts_page(request)


def _add_page(request: Request, status: int = 200, error: str = "", phone: str = "") -> Response:
    # Credentials are never echoed back into the form.
    return render(request, "telegram_add.html", status, active="telegram", error=error, phone=phone,
                  defaults_available=request.ctx.settings.telegram_configured)


async def add_get(request: Request) -> Response:
    return _add_page(request, error=GONE.get(request.query.get("gone", ""), ""))


async def add_post(request: Request) -> Response:
    ctx, user_id = request.ctx, request.user.id
    key = f"tg-send:{key_part(user_id)}"
    retry = await limit_check(request, key, SEND_LIMIT, SEND_WINDOW)
    if retry:
        raise HttpError(429, retry)
    await ctx.limiter.record(key, SEND_WINDOW, int(ctx.clock()))
    form = request.form
    try:
        attempt_id = await _svc(request).start(user_id, form.get("phone", ""), form.get("api_id", ""), form.get("api_hash", ""))
    except TelegramError as exc:
        status, message, retry_after = _describe(exc)
        return _with_retry(_add_page(request, status, message, form.get("phone", "")[:30]), retry_after)
    return redirect(request, f"/telegram/verify/{attempt_id}")


async def _verify_page(request: Request, status: int = 200, error: str = "") -> Response:
    attempt_id = request.path_params["attempt_id"]
    try:
        pending = await _svc(request).pending(request.user.id, attempt_id)
    except LoginAttemptGone as gone:
        return redirect(request, f"/telegram/add?gone={gone.reason}")
    return render(request, "telegram_verify.html", status, active="telegram", pending=pending, error=error,
                  attempt_id=attempt_id)


async def verify_get(request: Request) -> Response:
    return await _verify_page(request)


async def _guarded(request: Request, action):
    """Common rate limiting + error mapping for the two verification steps."""
    ctx, user_id = request.ctx, request.user.id
    key = f"tg-verify:{key_part(user_id)}"
    retry = await limit_check(request, key, VERIFY_LIMIT, VERIFY_WINDOW)
    if retry:
        raise HttpError(429, retry)
    await ctx.limiter.record(key, VERIFY_WINDOW, int(ctx.clock()))
    attempt_id = request.path_params["attempt_id"]
    try:
        outcome = await action(user_id, attempt_id)
    except LoginAttemptGone as gone:
        return _add_page(request, 400, GONE.get(gone.reason, GONE["not_found"]))
    except WrongStep:
        return redirect(request, f"/telegram/verify/{attempt_id}")
    except TelegramError as exc:
        status, message, retry_after = _describe(exc)
        if exc.code == "code_expired":
            return _add_page(request, status, message)
        return _with_retry(await _verify_page(request, status, message), retry_after)
    if outcome.password_needed:
        return redirect(request, f"/telegram/verify/{attempt_id}")
    return redirect(request, "/telegram?notice=connected")


async def code_post(request: Request) -> Response:
    code = request.form.get("code", "")
    return await _guarded(request, lambda uid, aid: _svc(request).submit_code(uid, aid, code))


async def password_post(request: Request) -> Response:
    password = request.form.get("password", "")
    return await _guarded(request, lambda uid, aid: _svc(request).submit_password(uid, aid, password))


async def cancel_post(request: Request) -> Response:
    await _svc(request).cancel(request.user.id, request.path_params["attempt_id"])
    return redirect(request, "/telegram")


async def _account_action(request: Request, action, notice: str) -> Response:
    try:
        result = await action(request.user.id, request.path_params["account_id"])
    except AccountNotFound:
        raise HttpError(404) from None  # foreign ids look exactly like missing ones
    except TelegramError as exc:
        status, message, retry = _describe(exc)
        return _with_retry(await _accounts_page(request, status, message), retry)
    return redirect(request, f"/telegram?notice={notice(result) if callable(notice) else notice}")


async def disconnect_post(request: Request) -> Response:
    return await _account_action(request, _svc(request).disconnect, "disconnected")


async def remove_post(request: Request) -> Response:
    return await _account_action(request, _svc(request).remove, "removed")


async def check_post(request: Request) -> Response:
    return await _account_action(request, _svc(request).check, lambda ok: "checked" if ok else "expired")


P = "telegram.manage"
TELEGRAM_ROUTES = [
    Route("telegram", "GET", "/telegram", accounts_get, Auth.USER, P),
    Route("telegram-add", "GET", "/telegram/add", add_get, Auth.USER, P),
    Route("telegram-add", "POST", "/telegram/add", add_post, Auth.USER, P),
    Route("telegram-verify", "GET", "/telegram/verify/{attempt_id}", verify_get, Auth.USER, P),
    Route("telegram-code", "POST", "/telegram/verify/{attempt_id}/code", code_post, Auth.USER, P),
    Route("telegram-password", "POST", "/telegram/verify/{attempt_id}/password", password_post, Auth.USER, P),
    Route("telegram-cancel", "POST", "/telegram/verify/{attempt_id}/cancel", cancel_post, Auth.USER, P),
    Route("telegram-disconnect", "POST", "/telegram/accounts/{account_id}/disconnect", disconnect_post, Auth.USER, P),
    Route("telegram-check", "POST", "/telegram/accounts/{account_id}/check", check_post, Auth.USER, P),
    Route("telegram-remove", "POST", "/telegram/accounts/{account_id}/remove", remove_post, Auth.USER, P),
]
