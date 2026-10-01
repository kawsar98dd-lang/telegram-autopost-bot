"""Page controllers for Step 2: first-run setup, login/logout, license activation, dashboard shell."""

from __future__ import annotations

import asyncio
import hmac
import logging

from ..auth.ratelimit import key_part
from ..auth.redirects import safe_next
from ..auth.users import SetupAlreadyDone, is_valid_email, normalize_email
from ..licensing.client import LicenseError
from ..security.password_policy import password_problems
from ..security.passwords import hash_password, verify_password
from .errors import HttpError, error_response
from .http import Request, Response, redirect
from .pipeline import Auth, Route
from .views import NAV, WINDOW, license_view, limit_check, render, start_session  # noqa: F401

log = logging.getLogger(__name__)

LOGIN_IP_LIMIT, LOGIN_ACCOUNT_IP_LIMIT, LOGIN_ACCOUNT_LIMIT = 20, 5, 50
SETUP_LIMIT, ACTIVATE_LIMIT = 10, 10

ACTIVATION_ERRORS = {
    "invalid_key": (400, "That license key was not recognised. Check it and try again."),
    "product_mismatch": (400, "This license key is for a different product."),
    "activation_limit": (409, "This license has reached its activation limit. Ask the seller to reset or transfer it."),
    "expired": (403, "This license has expired."),
    "revoked": (403, "This license has been revoked."),
    "disabled": (403, "This license has been disabled."),
    "unreachable": (503, "The license server could not be reached. Check the internet connection and try again."),
    "invalid_response": (502, "The license server's answer could not be verified. Please try again later."),
    "not_configured": (503, "This build has no license server configured. Please contact the seller."),
}
_DEFAULT_ACTIVATION_ERROR = (400, "The license could not be activated.")

_dummy_hash: str | None = None


# ---- first-run setup ---------------------------------------------------------------------------
async def setup_get(request: Request) -> Response:
    return render(request, "setup.html", token_required=bool(request.ctx.settings.setup_token),
                  errors=[], email="", display_name="")


async def setup_post(request: Request) -> Response:
    ctx = request.ctx
    key = f"setup:{key_part(request.client_ip)}"
    retry = await limit_check(request, key, SETUP_LIMIT)
    if retry:
        raise HttpError(429, retry)
    await ctx.limiter.record(key, WINDOW, int(ctx.clock()))

    form = request.form
    email = normalize_email(form.get("email", ""))
    display_name = form.get("display_name", "").strip()[:80]
    password, confirm = form.get("password", ""), form.get("password_confirm", "")
    errors: list[str] = []
    if ctx.settings.setup_token and not hmac.compare_digest(
        form.get("setup_token", "").encode("utf-8"), ctx.settings.setup_token.encode("utf-8")
    ):
        errors.append("The setup token is not correct.")
    if not is_valid_email(email):
        errors.append("Enter a valid email address.")
    errors += password_problems(password, email)
    if password != confirm:
        errors.append("The two passwords do not match.")
    if errors:
        return render(request, "setup.html", 400, token_required=bool(ctx.settings.setup_token),
                      errors=errors, email=email, display_name=display_name)

    password_hash = await asyncio.to_thread(hash_password, password)
    try:
        user_id = await ctx.users.create_initial_admin(email, password_hash, display_name)
    except SetupAlreadyDone:
        raise HttpError(409)
    destination = "/" if ctx.license.status().enabled else "/activate"
    response = redirect(request, destination)
    await start_session(request, response, user_id)
    return response


# ---- login / logout -----------------------------------------------------------------------------
async def login_get(request: Request) -> Response:
    return render(request, "login.html", error="", email="", next=safe_next(request.query.get("next")))


async def login_post(request: Request) -> Response:
    global _dummy_hash
    ctx = request.ctx
    email = normalize_email(request.form.get("email", ""))[:254]
    password = request.form.get("password", "")[:256]
    next_url = safe_next(request.form.get("next"))

    ip_key = f"login:ip:{key_part(request.client_ip)}"
    acct_ip_key = f"login:acct-ip:{key_part(email)}:{key_part(request.client_ip)}"
    acct_key = f"login:acct:{key_part(email)}"
    checks = ((ip_key, LOGIN_IP_LIMIT), (acct_ip_key, LOGIN_ACCOUNT_IP_LIMIT), (acct_key, LOGIN_ACCOUNT_LIMIT))
    retry = max([await limit_check(request, k, n) for k, n in checks])
    if retry:
        raise HttpError(429, retry)

    row = await ctx.users.find_by_email(email) if email else None
    if row is not None and row["is_active"]:
        valid = await asyncio.to_thread(verify_password, password, row["password_hash"])
    else:
        # Same amount of work whether or not the account exists (no user enumeration by timing).
        if _dummy_hash is None:
            _dummy_hash = hash_password("dummy-password-for-timing-only")
        await asyncio.to_thread(verify_password, password, _dummy_hash)
        valid = False

    if not valid:
        now = int(ctx.clock())
        for key, _ in checks:
            await ctx.limiter.record(key, WINDOW, now)
        return render(request, "login.html", 401, error="Invalid email or password.", email=email, next=next_url)

    await ctx.limiter.reset(acct_ip_key)
    # Session fixation defence: the login route is anonymous-only (signed-in users are redirected by
    # the pipeline) and ALWAYS issues a brand-new random token; a cookie planted by an attacker never
    # matches a stored session and stays worthless.
    await ctx.users.touch_login(str(row["id"]))
    if bool(row["is_admin"]) and not ctx.license.status().enabled:
        next_url = "/activate"
    response = redirect(request, next_url)
    await start_session(request, response, str(row["id"]))
    return response


async def logout_post(request: Request) -> Response:
    ctx = request.ctx
    if request.session is not None:
        await ctx.sessions.destroy(request.session.token)
    response = redirect(request, "/login")
    response.delete_cookie(ctx.session_cookie, secure=ctx.settings.cookie_secure)
    return response


# ---- license activation --------------------------------------------------------------------------
async def activate_get(request: Request) -> Response:
    return render(request, "activate.html", active="license", lic=license_view(request), error="")


async def activate_post(request: Request) -> Response:
    ctx = request.ctx
    key = f"activate:{key_part(request.user.id)}"
    retry = await limit_check(request, key, ACTIVATE_LIMIT)
    if retry:
        raise HttpError(429, retry)
    await ctx.limiter.record(key, WINDOW, int(ctx.clock()))

    try:
        await ctx.license.activate(request.form.get("license_key", "")[:100])
    except LicenseError as exc:
        # Only OUR fixed messages are shown; nothing the server sent is echoed back.
        status, message = ACTIVATION_ERRORS.get(exc.code, _DEFAULT_ACTIVATION_ERROR)
        log.info("license activation refused (%s)", exc.code)
        if request.is_htmx:
            return error_response(request, status) if status == 429 else _flash(request, status, message)
        return render(request, "activate.html", status, active="license", lic=license_view(request), error=message)
    return redirect(request, "/")


def _flash(request: Request, status: int, message: str) -> Response:
    body = request.ctx.templates.get_template("partials/flash.html").render(message=message).encode("utf-8")
    return Response(status, body).header("HX-Retarget", "#flash").header("HX-Reswap", "innerHTML")


async def license_verify_post(request: Request) -> Response:
    try:
        await request.ctx.license.verify()
    except LicenseError:
        pass  # no activation yet; the card shows the state
    if request.is_htmx:
        return render(request, "partials/license_card.html", lic=license_view(request))
    return redirect(request, "/")


async def license_card_get(request: Request) -> Response:
    return render(request, "partials/license_card.html", lic=license_view(request))


# ---- dashboard shell -------------------------------------------------------------------------------
async def _count(request: Request, table: str) -> int:
    # Every user-owned query is scoped by the authenticated user's id.
    row = await request.ctx.db.fetchrow(f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = $1", request.user.id)  # noqa: S608 (fixed table names)
    return int(row["n"])


async def dashboard(request: Request) -> Response:
    counts = {t: await _count(request, t) for t in ("telegram_accounts", "telegram_groups", "schedules", "posting_jobs")}
    return render(request, "dashboard.html", active="dashboard", counts=counts, lic=license_view(request))


def placeholder(name: str, title: str, description: str):
    async def handler(request: Request) -> Response:
        return render(request, "placeholder.html", active=name, title=title, description=description)

    return handler


async def account_get(request: Request) -> Response:
    ctx = request.ctx
    rows = await ctx.sessions.list_for_user(request.user.id)
    from ..auth.sessions import hash_token

    current = hash_token(request.session.token)
    sessions = [{**r, "current": r["token_hash"] == current} for r in rows]
    return render(request, "account.html", active="account", sessions=sessions, session=request.session)


async def account_revoke_others(request: Request) -> Response:
    await request.ctx.sessions.destroy_others(request.user.id, request.session.token)
    return redirect(request, "/account")


from .telegram_routes import TELEGRAM_ROUTES  # noqa: E402  (imports views, not this module)

ROUTES = [
    Route("setup", "GET", "/setup", setup_get, Auth.ANONYMOUS),
    Route("setup", "POST", "/setup", setup_post, Auth.ANONYMOUS),
    Route("login", "GET", "/login", login_get, Auth.ANONYMOUS),
    Route("login", "POST", "/login", login_post, Auth.ANONYMOUS),
    Route("logout", "POST", "/logout", logout_post, Auth.USER),
    Route("activate", "GET", "/activate", activate_get, Auth.ADMIN),
    Route("activate", "POST", "/activate", activate_post, Auth.ADMIN),
    Route("license-verify", "POST", "/license/verify", license_verify_post, Auth.ADMIN),
    Route("license-card", "GET", "/ui/license-card", license_card_get, Auth.USER),
    Route("dashboard", "GET", "/", dashboard, Auth.USER, "dashboard.view"),
    Route("groups", "GET", "/groups", placeholder("groups", "Groups", "Choose the groups you are allowed to post in. Available in an upcoming release."), Auth.USER, "dashboard.view"),
    Route("schedules", "GET", "/schedules", placeholder("schedules", "Schedules", "Create and manage scheduled posts. Available in an upcoming release."), Auth.USER, "dashboard.view"),
    Route("jobs", "GET", "/jobs", placeholder("jobs", "Posting jobs", "Follow queued, running and finished posts. Available in an upcoming release."), Auth.USER, "dashboard.view"),
    Route("logs", "GET", "/logs", placeholder("logs", "Logs", "Posting history and errors. Available in an upcoming release."), Auth.USER, "dashboard.view"),
    Route("account", "GET", "/account", account_get, Auth.USER, "account.manage"),
    Route("account-revoke", "POST", "/account/sessions/revoke-others", account_revoke_others, Auth.USER, "account.manage"),
] + TELEGRAM_ROUTES
