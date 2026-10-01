"""Helpers shared by the page controllers."""

from __future__ import annotations

from .http import Request, Response

WINDOW = 15 * 60

NAV = [
    ("dashboard", "/", "Dashboard"),
    ("telegram", "/telegram", "Telegram accounts"),
    ("groups", "/groups", "Groups"),
    ("schedules", "/schedules", "Schedules"),
    ("jobs", "/jobs", "Posting jobs"),
    ("logs", "/logs", "Logs"),
    ("account", "/account", "Account"),
]

STATE_LABELS = {
    "active": ("Active", "ok"),
    "grace": ("Active (offline grace period)", "warn"),
    "verify_required": ("Verification required", "bad"),
    "not_activated": ("Not activated", "bad"),
    "expired": ("Expired", "bad"),
    "revoked": ("Disabled", "bad"),
    "mismatch": ("Does not match this installation", "bad"),
    "tampered": ("Check failed", "bad"),
}

def render(request: Request, template: str, status: int = 200, **values) -> Response:
    ctx = request.ctx
    body = ctx.templates.get_template(template).render(
        request=request, user=request.user, csrf_token=request.csrf_token, nav=NAV, **values
    )
    return Response(status, body.encode("utf-8"))


def license_view(request: Request) -> dict:
    summary = request.ctx.license.summary()
    label, tone = STATE_LABELS.get(summary.state.value, ("Unknown", "bad"))
    return {"summary": summary, "label": label, "tone": tone}


async def start_session(request: Request, response: Response, user_id: str) -> None:
    ctx = request.ctx
    token = await ctx.sessions.create(user_id, request.headers.get("user-agent", ""))
    response.set_cookie(ctx.session_cookie, token, max_age=ctx.sessions.absolute_seconds,
                        secure=ctx.settings.cookie_secure)


async def limit_check(request: Request, key: str, limit: int, window: int = WINDOW) -> int:
    """Seconds to wait if ``key`` is over its limit, else 0."""
    return await request.ctx.limiter.retry_after(key, limit, window, int(request.ctx.clock()))
