"""Read-only, authenticated safety center and user-scoped notification acknowledgement."""
from __future__ import annotations

from ..auth.ratelimit import key_part
from ..auth.ratelimit import RateLimiter
from .errors import HttpError
from .http import Request, Response, redirect
from .pipeline import Auth, Route
from .views import limit_check, render
from ..safety.engine import MESSAGES, answer_question, language_for, localize_notification

QUESTION_LIMIT = 10
QUESTION_WINDOW = 15 * 60


async def safety_index(request: Request) -> Response:
    user_id = request.user.id
    lang = "en"
    question = ""
    answer = ""
    if request.method == "POST":
        question = request.form.get("question", "")[:500].strip()
        selected = request.form.get("language", "")[:2].lower()
        lang = language_for(question, selected)
        if not question:
            answer = MESSAGES[lang]["unknown"]
        else:
            key = f"safety-question:{key_part(user_id)}"
            retry = await limit_check(request, key, QUESTION_LIMIT, QUESTION_WINDOW)
            if retry:
                raise HttpError(429, f"Please wait {retry} seconds before asking another question.")
            await request.ctx.limiter.record(key, QUESTION_WINDOW, int(request.ctx.clock()))
            answer = answer_question(question, lang)
            q = question.casefold()
            if any(term in q for term in ("risk", "warning", "schedule", "failed", "floodwait", "post", "ঝুঁকি", "সতর্কতা", "শিডিউল", "ব্যর্থ", "পোস্ট", "ফ্লাডওয়েট")):
                findings = await request.ctx.db.fetch(
                    "SELECT rule_key,title,evidence,recommendation FROM safety_notifications "
                    "WHERE user_id=$1 AND status='new' ORDER BY created_at DESC LIMIT 3", user_id)
                if findings:
                    localized = [localize_notification(row, lang) for row in findings]
                    answer += "\n\n" + "\n".join(
                        f"• {item['title']}: {item['evidence']} {item['recommendation']}" for item in localized)
                else:
                    answer += "\n\n" + MESSAGES[lang]["empty"]
    if lang not in MESSAGES:
        lang = "en"
    rows = await request.ctx.db.fetch(
        "SELECT id,title,severity,evidence,recommendation,status,created_at,rule_key "
        "FROM safety_notifications WHERE user_id=$1 ORDER BY created_at DESC LIMIT 100", user_id)
    rows = [localize_notification(row, lang) for row in rows]
    active_count = sum(1 for row in rows if row["status"] == "new")
    counts = {"schedules": 0, "recent_jobs": 0, "failed_jobs": 0}
    stats = await request.ctx.db.fetchrow(
        "SELECT (SELECT COUNT(*) FROM schedules WHERE user_id=$1 AND status='active') AS schedules, "
        "(SELECT COUNT(*) FROM posting_jobs WHERE user_id=$1 AND created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days') AS recent_jobs, "
        "(SELECT COUNT(*) FROM posting_jobs WHERE user_id=$1 AND status='failed' AND updated_at >= CURRENT_TIMESTAMP - INTERVAL '7 days') AS failed_jobs", user_id)
    if stats:
        counts = {k: int(stats[k]) for k in counts}
    return render(request, "safety.html", active="safety", rows=rows, active_count=active_count,
                  counts=counts, lang=lang, messages=MESSAGES[lang], question=question, answer=answer)


async def safety_acknowledge(request: Request) -> Response:
    notification_id = request.path_params["notification_id"]
    result = await request.ctx.db.fetchrow(
        "UPDATE safety_notifications SET status='read',updated_at=CURRENT_TIMESTAMP "
        "WHERE id=$1 AND user_id=$2 AND status='new' RETURNING id", notification_id, request.user.id)
    # Do not reveal whether an ID belongs to another user.
    if result is None:
        exists = await request.ctx.db.fetchrow(
            "SELECT id FROM safety_notifications WHERE id=$1 AND user_id=$2", notification_id, request.user.id)
        if exists is None:
            raise HttpError(404)
    return redirect(request, "/safety")


SAFETY_ROUTES = [
    Route("safety", "GET", "/safety", safety_index, Auth.USER, "dashboard.view"),
    Route("safety-question", "POST", "/safety", safety_index, Auth.USER, "dashboard.view"),
    Route("safety-acknowledge", "POST", "/safety/notifications/{notification_id}/ack", safety_acknowledge, Auth.USER, "dashboard.view"),
]
