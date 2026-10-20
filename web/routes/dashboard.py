"""GET / dashboard + GET /partials/dashboard-jobs (HTMX poll target)."""
from __future__ import annotations

import shutil
import time

from fastapi import APIRouter, Request

from bot.config import settings
from web.flash import pop_flashes
from web.live import MOVING_STATES, job_view, row_progress
from yt.quota import BUCKET_LIMITS, remaining

router = APIRouter()

_PROCESS_STARTED_AT = time.monotonic()


RING_CIRCUMFERENCE = 263.89  # 2 * pi * r, with r = 42 in the quota ring's SVG


def _uptime_str() -> str:
    seconds = int(time.monotonic() - _PROCESS_STARTED_AT)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


# Groups the job state machine's 10 states into 4 bands for the dashboard's
# pipeline strip -- fine-grained enough to be informative, coarse enough to
# read as a single glance rather than a wall of slivers.
_PIPELINE_BANDS = [
    ("waiting", "Waiting", ("QUEUED", "AWAITING_CHANNEL", "AWAITING_CAPTION", "READY")),
    ("active", "In flight", ("DOWNLOADING", "UPLOADING", "VERIFYING")),
    ("done", "Done", ("DONE",)),
    ("failed", "Failed", ("FAILED", "CANCELLED")),
]


async def _pipeline_bands(store) -> list[dict]:
    counts = await store.count_jobs_by_state()
    total = sum(counts.values())
    bands = []
    for key, label, states in _PIPELINE_BANDS:
        n = sum(counts.get(s, 0) for s in states)
        bands.append({
            "key": key,
            "label": label,
            "count": n,
            "pct": round((n / total) * 100, 1) if total else 0,
        })
    return bands


async def _dashboard_context(request: Request) -> dict:
    store = request.app.state.store
    channels = await store.count_channels()
    uploads_today = await store.count_uploads_today()
    uploads_remaining = await remaining(store, "uploads")
    quota_summary = await store.quota_summary()
    active_jobs = await store.list_active_jobs()
    pipeline = await _pipeline_bands(store)
    try:
        disk = shutil.disk_usage(settings.jobs_dir)
        disk_free_gb = disk.free / 1024**3
    except OSError:
        disk_free_gb = None
    channel_titles = {c["id"]: c["channel_title"] for c in await store.list_yt_channels(include_revoked=True)}
    rows = []
    for job in active_jobs:
        view = job_view(job)
        label, frac = row_progress(view)
        rows.append({"job": job, "view": view, "label": label, "frac": frac})
    monitor = next((r for r in rows if r["job"].state in MOVING_STATES), None)
    general_used = quota_summary.get("general", 0)
    return {
        "rows": rows,
        "channel_titles": channel_titles,
        "monitor": monitor,
        "in_flight": sum(1 for r in rows if r["job"].state in MOVING_STATES),
        "waiting": sum(1 for r in rows if r["job"].state not in MOVING_STATES),
        "uptime_s": int(time.monotonic() - _PROCESS_STARTED_AT),
        "ring_dash": round(min(1.0, general_used / BUCKET_LIMITS["general"]) * RING_CIRCUMFERENCE, 2),
        "channels": channels,
        "uploads_today": uploads_today,
        "uploads_bucket_limit": BUCKET_LIMITS["uploads"],
        "uploads_remaining": uploads_remaining,
        "general_used": quota_summary.get("general", 0),
        "general_limit": BUCKET_LIMITS["general"],
        "search_used": quota_summary.get("search", 0),
        "search_limit": BUCKET_LIMITS["search"],
        "jobs": active_jobs,
        "pipeline": pipeline,
        "pipeline_total": sum(b["count"] for b in pipeline),
        "disk_free_gb": disk_free_gb,
        "uptime": _uptime_str(),
        "bot_up": True,  # in-process with the bot; if this route answers, the process is alive
    }


@router.get("/")
async def dashboard(request: Request):
    templates = request.app.state.templates
    ctx = await _dashboard_context(request)
    ctx["flashes"] = pop_flashes(request)
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/partials/dashboard-jobs")
async def dashboard_jobs_partial(request: Request):
    templates = request.app.state.templates
    ctx = await _dashboard_context(request)
    return templates.TemplateResponse(request, "partials/dashboard_live.html", ctx)
