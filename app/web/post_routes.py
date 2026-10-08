"""Posts pages: list, create/edit a draft (text + one image + target groups), preview, delete, owner-only image."""

from __future__ import annotations

import logging

from ..auth.ratelimit import key_part
from ..posts import composer
from ..posts.limits import MAX_BODY_INPUT_CHARS, MAX_TITLE_CHARS, MULTIPART_OVERHEAD_BYTES, max_image_bytes
from ..posts.service import PostInvalid, PostLocked, PostNotFound, Upload
from ..telegram.group_sync import MAX_SELECTED_GROUPS, GroupNotFound, NotSelectable, TooManySelected
from .errors import HttpError
from .http import Request, Response, redirect
from .pipeline import Auth, Route
from .views import limit_check, render

log = logging.getLogger(__name__)

SAVE_LIMIT, SAVE_WINDOW = 60, 10 * 60  # saves per user per 10 minutes
NOTICES = {"saved": "Draft saved.", "created": "Draft created.", "deleted": "Draft deleted."}
P = "posts.manage"


def _upload_limit(request: Request) -> int:
    return max_image_bytes(request.ctx.settings.max_upload_mb) + MULTIPART_OVERHEAD_BYTES


def _selected(request: Request) -> set[str]:
    """Checkbox names are sel_<group uuid>. Only the ids are used; nothing else in the form can influence targets."""
    return {k[4:] for k in request.form if k.startswith("sel_")}


def _upload(request: Request) -> Upload | None:
    f = request.files.get("image")
    return Upload(f.filename, f.data) if f is not None else None


async def _rate(request: Request) -> None:
    key = f"post-save:{key_part(request.user.id)}"
    retry = await limit_check(request, key, SAVE_LIMIT, SAVE_WINDOW)
    if retry:
        raise HttpError(429, retry)
    await request.ctx.limiter.record(key, SAVE_WINDOW, int(request.ctx.clock()))


async def _editor(request: Request, *, account: dict, post: dict | None, form: dict, selected: set[str],
                  errors: list[str], status: int = 200) -> Response:
    svc = request.ctx.posts
    groups = await svc.eligible_groups(request.user.id, account["id"])
    eligible = {g["id"].lower() for g in groups}
    stale = [t for t in (post["targets"] if post else []) if t["id"].lower() not in eligible]
    has_image = bool(post and post["media"])
    return render(request, "post_edit.html", status, active="posts", account=account, post=post, form=form,
                  groups=groups, selected={s.lower() for s in selected}, errors=errors, stale_targets=stale,
                  footer=composer.footer_block(), footer_units=len(composer.footer_block().encode("utf-16-le")) // 2 + 2,
                  text_limit=composer.limit_for(False)[0], caption_limit=composer.limit_for(True)[0],
                  has_image=has_image, max_targets=MAX_SELECTED_GROUPS, max_title=MAX_TITLE_CHARS,
                  max_input=MAX_BODY_INPUT_CHARS, max_image_mb=max(1, svc.max_image_bytes // (1024 * 1024)),
                  notice=NOTICES.get(request.query.get("notice", ""), ""))


def _fail(request: Request, exc: Exception):
    """Map service errors to HTTP answers. Foreign/unknown ids are 404 (same as missing); others re-render the form."""
    if isinstance(exc, (PostNotFound, GroupNotFound)):
        raise HttpError(404) from None
    if isinstance(exc, PostLocked):
        raise HttpError(409) from None
    raise exc


async def posts_index(request: Request) -> Response:
    svc = request.ctx.posts
    return render(request, "posts.html", active="posts", posts=await svc.list_posts(request.user.id),
                  accounts=await svc.connected_accounts(request.user.id),
                  notice=NOTICES.get(request.query.get("notice", ""), ""))


async def post_new(request: Request) -> Response:
    svc = request.ctx.posts
    accounts = await svc.connected_accounts(request.user.id)
    if not accounts:
        return render(request, "posts.html", 200, active="posts", posts=[], accounts=[], notice="")
    wanted = request.path_params.get("account_id", "")
    if wanted:
        try:
            account = await svc.account(request.user.id, wanted)
        except PostNotFound:
            raise HttpError(404) from None
    elif len(accounts) == 1:
        account = accounts[0]
    else:
        return render(request, "post_choose_account.html", active="posts", accounts=accounts)
    return await _editor(request, account=account, post=None, form={"title": "", "body": ""}, selected=set(), errors=[])


async def post_create(request: Request) -> Response:
    await _rate(request)
    svc, user_id = request.ctx.posts, request.user.id
    form = {"title": request.form.get("title", ""), "body": request.form.get("body", "")}
    account_id = request.form.get("account_id", "")
    try:
        account = await svc.account(user_id, account_id)
        post_id = await svc.create(user_id, account["id"], form["title"], form["body"], _selected(request), _upload(request))
    except PostInvalid as exc:
        return await _editor(request, account=account, post=None, form=form, selected=_selected(request), errors=exc.errors, status=400)
    except (NotSelectable, TooManySelected) as exc:
        msg = ("One of the chosen groups cannot be used for posting. Refresh the groups and try again."
               if isinstance(exc, NotSelectable) else f"You can select at most {MAX_SELECTED_GROUPS} groups.")
        return await _editor(request, account=account, post=None, form=form, selected=set(), errors=[msg], status=409)
    except Exception as exc:  # noqa: BLE001
        _fail(request, exc)
    return redirect(request, f"/posts/{post_id}?notice=created")


async def post_get(request: Request) -> Response:
    svc = request.ctx.posts
    try:
        post = await svc.get(request.user.id, request.path_params["post_id"])
        account = await svc.account(request.user.id, post["account_id"])
    except PostNotFound:
        raise HttpError(404) from None
    form = {"title": post["title"] or "", "body": post["body"]}
    return await _editor(request, account=account, post=post, form=form, selected={t["id"] for t in post["targets"] if t["eligible"]}, errors=[])


async def post_save(request: Request) -> Response:
    await _rate(request)
    svc, user_id, post_id = request.ctx.posts, request.user.id, request.path_params["post_id"]
    form = {"title": request.form.get("title", ""), "body": request.form.get("body", "")}
    try:
        await svc.update(user_id, post_id, form["title"], form["body"], _selected(request), _upload(request),
                         request.form.get("remove_image") == "1")
    except (PostInvalid, NotSelectable, TooManySelected) as exc:
        try:
            post = await svc.get(user_id, post_id)
            account = await svc.account(user_id, post["account_id"])
        except PostNotFound:
            raise HttpError(404) from None
        if isinstance(exc, PostInvalid):
            errors, status = exc.errors, 400
        else:
            errors, status = ["One of the chosen groups cannot be used for posting, or too many were chosen. Refresh the groups and try again."], 409
        return await _editor(request, account=account, post=post, form=form, selected=_selected(request), errors=errors, status=status)
    except Exception as exc:  # noqa: BLE001
        _fail(request, exc)
    return redirect(request, f"/posts/{post_id}?notice=saved")


async def post_preview(request: Request) -> Response:
    try:
        data = await request.ctx.posts.preview(request.user.id, request.path_params["post_id"])
    except PostNotFound:
        raise HttpError(404) from None
    return render(request, "post_preview.html", active="posts", **data)


async def post_delete(request: Request) -> Response:
    try:
        await request.ctx.posts.delete(request.user.id, request.path_params["post_id"])
    except Exception as exc:  # noqa: BLE001
        _fail(request, exc)
    return redirect(request, "/posts?notice=deleted")


async def post_image(request: Request) -> Response:
    try:
        content_type, data = await request.ctx.posts.image(request.user.id, request.path_params["post_id"])
    except PostNotFound:
        raise HttpError(404) from None
    return Response(200, data, content_type).header("Content-Disposition", "inline")


POST_ROUTES = [
    Route("posts", "GET", "/posts", posts_index, Auth.USER, P),
    Route("posts-new", "GET", "/posts/new", post_new, Auth.USER, P),
    Route("posts-new-account", "GET", "/posts/new/{account_id}", post_new, Auth.USER, P),
    Route("posts-create", "POST", "/posts", post_create, Auth.USER, P, max_body=max_image_bytes(200) + MULTIPART_OVERHEAD_BYTES),
    Route("posts-edit", "GET", "/posts/{post_id}", post_get, Auth.USER, P),
    Route("posts-save", "POST", "/posts/{post_id}", post_save, Auth.USER, P, max_body=max_image_bytes(200) + MULTIPART_OVERHEAD_BYTES),
    Route("posts-preview", "GET", "/posts/{post_id}/preview", post_preview, Auth.USER, P),
    Route("posts-delete", "POST", "/posts/{post_id}/delete", post_delete, Auth.USER, P),
    Route("posts-image", "GET", "/posts/{post_id}/image", post_image, Auth.USER, P),
]
