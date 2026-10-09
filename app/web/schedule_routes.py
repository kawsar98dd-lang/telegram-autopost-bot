"""Schedules and jobs pages (Step 6): create a schedule for a saved draft, list, pause / resume / cancel, job history.

The web process only writes schedules. Sending is done by the worker process; nothing here talks to Telegram.
"""

from __future__ import annotations

import logging

from ..auth.ratelimit import key_part
from ..posts import composer
from ..posts.service import PostLocked, PostNotFound
from ..scheduler import recurrence
from ..scheduler.policy import NEEDS_ATTENTION
from ..scheduler.service import ScheduleConflict, ScheduleInput, ScheduleInvalid, ScheduleNotFound
from .errors import HttpError
from .http import Request, Response, redirect
from .pipeline import Auth, Route
from .views import limit_check, render

log = logging.getLogger(__name__)

P = "posts.manage"
WRITE_LIMIT, WRITE_WINDOW = 60, 10 * 60
NOTICES = {"created": "Schedule saved. The worker sends the post at the chosen time.", "paused": "Schedule paused.",
           "resumed": "Schedule resumed.", "cancelled": "Schedule cancelled.", "retried": "The job was queued again.",
           "confirmed": "Marked as delivered."}
COMMON_TIMEZONES = ("Asia/Dhaka", "UTC", "Asia/Kolkata", "Asia/Karachi", "Asia/Dubai", "Asia/Singapore", "Asia/Tokyo",
                    "Europe/London", "Europe/Berlin", "America/New_York", "America/Chicago", "America/Los_Angeles",
                    "Australia/Sydney")


async def _rate(request: Request) -> None:
    key = f"schedule-write:{key_part(request.user.id)}"
    retry = await limit_check(request, key, WRITE_LIMIT, WRITE_WINDOW)
    if retry:
        raise HttpError(429, retry)
    await request.ctx.limiter.record(key, WRITE_WINDOW, int(request.ctx.clock()))


def _input(request: Request, post_id: str) -> ScheduleInput:
    f = request.form
    return ScheduleInput(
        post_id=post_id, group_ids={k[4:] for k in f if k.startswith("sel_")}, kind=f.get("kind", ""),
        timezone=f.get("timezone", ""), once_at=f.get("once_at", ""), times=f.get("times", ""),
        weekdays=[k[3:] for k in f if k.startswith("wd_")], every=f.get("every", ""), unit=f.get("unit", "hours"),
        starts=f.get("starts", ""), ends=f.get("ends", ""))


async def _form(request: Request, post_id: str, *, values: dict, selected: set[str] | None, errors: list[str],
                review: dict | None = None, status: int = 200) -> Response:
    info = await request.ctx.schedules.draft_for_scheduling(request.user.id, post_id)
    chosen = {t["id"].lower() for t in info["targets"]} if selected is None else {s.lower() for s in selected}
    return render(request, "schedule_form.html", status, active="schedules", post=info["post"], message=info["message"],
                  targets=info["targets"], stale=info["stale"], chosen=chosen, values=values, errors=errors,
                  review=review, timezones=COMMON_TIMEZONES, weekday_names=recurrence.WEEKDAY_NAMES,
                  min_interval=recurrence.MIN_INTERVAL_MINUTES)


def _not_found(exc: Exception):
    if isinstance(exc, (PostNotFound, ScheduleNotFound)):
        raise HttpError(404) from None
    if isinstance(exc, (PostLocked, ScheduleConflict)):
        raise HttpError(409) from None
    raise exc


DEFAULT_VALUES = {"kind": "once", "timezone": recurrence.DEFAULT_TIMEZONE, "once_at": "", "times": "09:00", "every": "6",
                  "unit": "hours", "starts": "", "ends": "", "weekdays": []}


async def schedules_index(request: Request) -> Response:
    return render(request, "schedules.html", active="schedules",
                  schedules=await request.ctx.schedules.list_schedules(request.user.id),
                  notice=NOTICES.get(request.query.get("notice", ""), ""))


async def schedule_new(request: Request) -> Response:
    try:
        return await _form(request, request.path_params["post_id"], values=dict(DEFAULT_VALUES), selected=None, errors=[])
    except Exception as exc:  # noqa: BLE001
        _not_found(exc)


async def schedule_submit(request: Request) -> Response:
    await _rate(request)
    post_id, svc = request.path_params["post_id"], request.ctx.schedules
    f = request.form
    values = {k: f.get(k, DEFAULT_VALUES[k]) for k in DEFAULT_VALUES if k != "weekdays"}
    values["weekdays"] = [k[3:] for k in f if k.startswith("wd_")]
    data = _input(request, post_id)
    try:
        if f.get("action") == "save":
            schedule_id = await svc.create(request.user.id, data)
            return redirect(request, f"/schedules/{schedule_id}?notice=created")
        review = await svc.review(request.user.id, data)
        return await _form(request, post_id, values=values, selected=data.group_ids, errors=[], review=review)
    except ScheduleInvalid as exc:
        return await _form(request, post_id, values=values, selected=data.group_ids, errors=exc.errors, status=400)
    except Exception as exc:  # noqa: BLE001
        _not_found(exc)


async def schedule_detail(request: Request) -> Response:
    try:
        data = await request.ctx.schedules.get(request.user.id, request.path_params["schedule_id"])
    except Exception as exc:  # noqa: BLE001
        _not_found(exc)
    return render(request, "schedule_detail.html", active="schedules", s=data, attention=NEEDS_ATTENTION,
                  notice=NOTICES.get(request.query.get("notice", ""), ""))


def _action(name: str, notice: str):
    async def handler(request: Request) -> Response:
        await _rate(request)
        schedule_id = request.path_params["schedule_id"]
        try:
            await getattr(request.ctx.schedules, name)(request.user.id, schedule_id)
        except ScheduleInvalid as exc:
            raise HttpError(409) from exc
        except Exception as exc:  # noqa: BLE001
            _not_found(exc)
        return redirect(request, f"/schedules/{schedule_id}?notice={notice}")

    handler.__name__ = f"schedule_{name}"
    return handler


async def jobs_index(request: Request) -> Response:
    return render(request, "jobs.html", active="jobs", jobs=await request.ctx.schedules.list_jobs(request.user.id),
                  attention=NEEDS_ATTENTION, notice=NOTICES.get(request.query.get("notice", ""), ""))


async def job_resolve(request: Request) -> Response:
    await _rate(request)
    action = request.form.get("action", "")
    try:
        await request.ctx.schedules.resolve_job(request.user.id, request.path_params["job_id"], action)
    except Exception as exc:  # noqa: BLE001
        _not_found(exc)
    return redirect(request, "/jobs?notice=" + ("confirmed" if action == "confirm_sent" else "retried"))


SCHEDULE_ROUTES = [
    Route("schedules", "GET", "/schedules", schedules_index, Auth.USER, P),
    Route("schedules-new", "GET", "/schedules/new/{post_id}", schedule_new, Auth.USER, P),
    Route("schedules-submit", "POST", "/schedules/new/{post_id}", schedule_submit, Auth.USER, P),
    Route("schedules-detail", "GET", "/schedules/{schedule_id}", schedule_detail, Auth.USER, P),
    Route("schedules-pause", "POST", "/schedules/{schedule_id}/pause", _action("pause", "paused"), Auth.USER, P),
    Route("schedules-resume", "POST", "/schedules/{schedule_id}/resume", _action("resume", "resumed"), Auth.USER, P),
    Route("schedules-cancel", "POST", "/schedules/{schedule_id}/cancel", _action("cancel", "cancelled"), Auth.USER, P),
    Route("jobs", "GET", "/jobs", jobs_index, Auth.USER, P),
    Route("jobs-resolve", "POST", "/jobs/{job_id}/resolve", job_resolve, Auth.USER, P),
]
