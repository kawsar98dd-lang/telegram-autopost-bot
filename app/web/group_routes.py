"""Groups pages: choose a connected Telegram account, refresh its groups from Telegram, select posting targets."""

from __future__ import annotations

import logging

from ..auth.ratelimit import key_part
from ..telegram.connect import AccountNotFound
from ..telegram.errors import FloodWait, NetworkProblem, SessionRevoked, TelegramError
from ..telegram.group_sync import MAX_SELECTED_GROUPS, GroupNotFound, NotSelectable, SyncBlocked, TooManySelected
from .errors import HttpError
from .http import Request, Response, redirect
from .pipeline import Auth, Route
from .views import limit_check, render

log = logging.getLogger(__name__)

REFRESH_LIMIT, REFRESH_WINDOW = 6, 10 * 60  # refreshes per user per 10 minutes
NOTICES = {
    "refreshed": "Groups refreshed from Telegram.",
    "saved": "Selection saved.",
}
STATUS_LABELS = {
    "ok": ("Can post", "ok"),
    "no_permission": ("Cannot post", "bad"),
    "restricted": ("Restricted", "bad"),
    "unavailable": ("Unavailable", "warn"),
    "unknown": ("Not checked", "warn"),
}
TYPE_LABELS = {"group": "Group", "supergroup": "Supergroup", "forum": "Forum supergroup", "channel": "Channel"}


def _svc(request: Request):
    return request.ctx.groups


async def _render_for(request: Request, account_id: str, status: int = 200, error: str = "") -> Response:
    svc = _svc(request)
    try:
        account = await svc.account(request.user.id, account_id)
    except AccountNotFound:
        raise HttpError(404) from None  # foreign ids look exactly like missing ones
    groups = await svc.groups(request.user.id, account["id"])
    connected = [a for a in await request.ctx.telegram_connect.list_accounts(request.user.id) if a["status"] == "connected"]
    postable = [g for g in groups if g["permission_status"] == "ok" and g["is_present"]]
    return render(request, "groups.html", status, active="groups", account=account, groups=groups, accounts=connected,
                  postable_count=len(postable), selected_count=sum(1 for g in groups if g["is_enabled"]),
                  notice=NOTICES.get(request.query.get("notice", ""), ""), error=error,
                  status_labels=STATUS_LABELS, type_labels=TYPE_LABELS, max_selected=MAX_SELECTED_GROUPS)


async def groups_index(request: Request) -> Response:
    accounts = await request.ctx.telegram_connect.list_accounts(request.user.id)
    connected = [a for a in accounts if a["status"] == "connected"]
    if len(connected) >= 1:
        return redirect(request, f"/groups/accounts/{connected[0]['id']}")
    return render(request, "groups_empty.html", active="groups", accounts=accounts)


async def account_get(request: Request) -> Response:
    return await _render_for(request, request.path_params["account_id"])


def _message_for(exc: TelegramError) -> tuple[int, str, int | None]:
    if isinstance(exc, FloodWait):
        return 429, f"Telegram asks to wait {exc.seconds} seconds before the groups can be loaded again.", exc.seconds
    if isinstance(exc, SessionRevoked):
        return 409, "Telegram no longer accepts this session. Connect the account again on the Telegram accounts page.", None
    if isinstance(exc, NetworkProblem):
        return 503, "Telegram is temporarily unavailable. Please try again in a few minutes.", None
    return 502, "Telegram returned an unexpected answer. Please try again later.", None


async def refresh_post(request: Request) -> Response:
    ctx, user_id, account_id = request.ctx, request.user.id, request.path_params["account_id"]
    key = f"groups-refresh:{key_part(user_id)}"
    retry = await limit_check(request, key, REFRESH_LIMIT, REFRESH_WINDOW)
    if retry:
        raise HttpError(429, retry)
    await ctx.limiter.record(key, REFRESH_WINDOW, int(ctx.clock()))
    try:
        await _svc(request).sync(user_id, account_id)
    except AccountNotFound:
        raise HttpError(404) from None
    except SyncBlocked as blocked:
        resp = await _render_for(request, account_id, 429, f"Telegram asked to wait. Try again in {blocked.seconds} seconds.")
        return resp.header("Retry-After", str(blocked.seconds))
    except TelegramError as exc:
        status, message, retry_after = _message_for(exc)
        resp = await _render_for(request, account_id, status, message)
        return resp.header("Retry-After", str(retry_after)) if retry_after else resp
    return redirect(request, f"/groups/accounts/{account_id}?notice=refreshed")


async def save_post(request: Request) -> Response:
    user_id, account_id = request.user.id, request.path_params["account_id"]
    selected = {k[4:] for k in request.form if k.startswith("sel_")}  # checkbox names: sel_<group uuid>
    try:
        await _svc(request).save_selection(user_id, account_id, selected)
    except (AccountNotFound, GroupNotFound):
        raise HttpError(404) from None
    except NotSelectable:
        return await _render_for(request, account_id, 409, "One of the chosen groups cannot be used for posting. Refresh the list and try again.")
    except TooManySelected:
        return await _render_for(request, account_id, 409, f"You can select at most {MAX_SELECTED_GROUPS} groups.")
    return redirect(request, f"/groups/accounts/{account_id}?notice=saved")


P = "groups.manage"
GROUP_ROUTES = [
    Route("groups", "GET", "/groups", groups_index, Auth.USER, P),
    Route("groups-account", "GET", "/groups/accounts/{account_id}", account_get, Auth.USER, P),
    Route("groups-refresh", "POST", "/groups/accounts/{account_id}/refresh", refresh_post, Auth.USER, P),
    Route("groups-save", "POST", "/groups/accounts/{account_id}/save", save_post, Auth.USER, P),
]
