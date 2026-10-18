"""Customer-facing, user-scoped posting audit log."""
from __future__ import annotations
import json
from datetime import date, datetime, time, timezone, timedelta
from .http import Request, Response
from .pipeline import Auth, Route
from .views import render
from .errors import HttpError

LEVELS = {"info", "warning", "error"}
PAGE_SIZE = 25
MAX_PAGE = 1000000

async def logs_index(request: Request) -> Response:
    f = request.form if request.method == "POST" else request.query
    try:
        page = int(f.get("page", "1"))
        if page < 1 or page > MAX_PAGE: raise ValueError()
    except (TypeError, ValueError):
        page = 1
    level = f.get("level", "").lower()
    if level not in LEVELS: level = ""
    event = f.get("event", "")[:60]
    status = f.get("status", "")[:30]
    start = f.get("start", "")
    end = f.get("end", "")
    try:
        start_date = date.fromisoformat(start) if start else None
        end_date = date.fromisoformat(end) if end else None
        if start_date and end_date and start_date > end_date: raise ValueError()
    except ValueError:
        return render(request, "logs.html", 400, active="logs", rows=[], page=1, pages=1, total=0,
                      filters={"level":level,"event":event,"status":status,"start":start,"end":end},
                      events=[], error="Invalid date range. Check the dates and try again.")
    where = ["l.user_id = $1"]
    args = [request.user.id]
    def add(clause, value):
        args.append(value); where.append(clause.replace("?", f"${len(args)}"))
    if level: add("l.level = ?", level)
    if event: add("l.event = ?", event)
    if start_date: add("l.created_at >= ?", datetime.combine(start_date, time.min, tzinfo=timezone.utc))
    if end_date: add("l.created_at < ?", datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc))
    if status: add("COALESCE(j.status, '') = ?", status)
    clause = " AND ".join(where)
    count = await request.ctx.db.fetchrow(
        "SELECT COUNT(*) AS n FROM posting_logs l LEFT JOIN posting_jobs j ON j.id=l.job_id AND j.user_id=l.user_id WHERE "+clause, *args)
    total = int(count["n"]); pages = max(1, (total + PAGE_SIZE - 1)//PAGE_SIZE)
    page = min(page, pages)
    qargs = args + [PAGE_SIZE, (page-1)*PAGE_SIZE]
    rows = await request.ctx.db.fetch(
        "SELECT l.id, l.created_at, l.level, l.event, l.message, l.details, l.job_id, "
        "j.schedule_id, j.group_id, j.group_title, j.status AS job_status, j.delivery_state, "
        "s.status AS schedule_status, g.title AS current_group_title "
        "FROM posting_logs l LEFT JOIN posting_jobs j ON j.id=l.job_id AND j.user_id=l.user_id "
        "LEFT JOIN schedules s ON s.id=j.schedule_id AND s.user_id=l.user_id "
        "LEFT JOIN telegram_groups g ON g.id=j.group_id AND g.user_id=l.user_id "
        "WHERE "+clause+" ORDER BY l.created_at DESC, l.id DESC LIMIT $"+str(len(args)+1)+" OFFSET $"+str(len(args)+2), *qargs)
    clean=[]
    for r in rows:
        d=dict(r)
        # Details contain only safe structured identifiers/codes/counters written by the queue.
        raw=d.get("details")
        if isinstance(raw, str):
            try: raw=json.loads(raw)
            except (ValueError, TypeError): raw={}
        raw = raw if isinstance(raw, dict) else {}
        safe_keys = {"schedule_id", "group_id", "post_id", "attempt", "delivery", "error_code", "kind", "targets",
                     "cancelled_pending_jobs", "reason", "delivery_before", "message_ids"}
        d["details"] = {k: v for k, v in raw.items() if k in safe_keys and isinstance(v, (str, int, float, bool, type(None)))}
        d["id"]=str(d["id"])
        if not d.get("schedule_id") and d["details"].get("schedule_id"):
            owned = await request.ctx.db.fetchrow("SELECT id FROM schedules WHERE id=$1 AND user_id=$2", d["details"]["schedule_id"], request.user.id)
            if owned is not None: d["schedule_id"] = str(owned["id"])
        clean.append(d)
    events = await request.ctx.db.fetch("SELECT DISTINCT event FROM posting_logs WHERE user_id=$1 ORDER BY event LIMIT 100", request.user.id)
    return render(request, "logs.html", active="logs", rows=clean, page=page, pages=pages, total=total,
                  filters={"level":level,"event":event,"status":status,"start":start,"end":end},
                  events=[r["event"] for r in events], error="")

async def log_detail(request: Request) -> Response:
    log_id=request.path_params["log_id"]
    row=await request.ctx.db.fetchrow(
        "SELECT l.id,l.created_at,l.level,l.event,l.message,l.details,l.job_id,j.schedule_id,j.group_title,j.status AS job_status,"
        "j.delivery_state FROM posting_logs l LEFT JOIN posting_jobs j ON j.id=l.job_id AND j.user_id=l.user_id "
        "WHERE l.id=$1 AND l.user_id=$2", log_id, request.user.id)
    if row is None: raise HttpError(404)
    d=dict(row); raw=d.get("details")
    if isinstance(raw,str):
        try: raw=json.loads(raw)
        except (ValueError,TypeError): raw={}
    raw = raw if isinstance(raw, dict) else {}
    safe_keys = {"schedule_id", "group_id", "post_id", "attempt", "delivery", "error_code", "kind", "targets",
                 "cancelled_pending_jobs", "reason", "delivery_before", "message_ids"}
    d["details"] = {k: v for k, v in raw.items() if k in safe_keys and isinstance(v, (str, int, float, bool, type(None)))}
    if not d.get("schedule_id") and d["details"].get("schedule_id"):
        owned = await request.ctx.db.fetchrow("SELECT id FROM schedules WHERE id=$1 AND user_id=$2", d["details"]["schedule_id"], request.user.id)
        if owned is not None: d["schedule_id"] = str(owned["id"])
    return render(request,"log_detail.html",active="logs",entry=d)

LOG_ROUTES=[
    Route("logs","GET","/logs",logs_index,Auth.USER,"dashboard.view"),
    Route("logs-filter","POST","/logs",logs_index,Auth.USER,"dashboard.view"),
    Route("log-detail","GET","/logs/{log_id}",log_detail,Auth.USER,"dashboard.view"),
]
