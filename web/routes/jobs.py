"""GET /jobs (filters, live partial), cancel/retry (spec section 6)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from bot.downloader import retry_target
from db.store import JOB_TERMINAL_STATES
from web.csrf import require_csrf
from web.flash import flash, pop_flashes

router = APIRouter()

ALL_STATES = ["QUEUED", "DOWNLOADING", "AWAITING_CHANNEL", "AWAITING_CAPTION", "READY",
              "UPLOADING", "VERIFYING", "DONE", "FAILED", "CANCELLED"]


@router.get("/jobs")
async def jobs_list(request: Request, state: Optional[str] = None, channel_pk: Optional[int] = None,
                     tg_user_id: Optional[int] = None):
    templates = request.app.state.templates
    store = request.app.state.store
    jobs = await store.list_jobs(state=state, channel_pk=channel_pk, tg_user_id=tg_user_id, limit=200)
    channels = await store.list_yt_channels(include_revoked=True)
    return templates.TemplateResponse(request, "jobs.html", {
        "jobs": jobs, "channels": channels, "all_states": ALL_STATES,
        "filter_state": state, "filter_channel_pk": channel_pk, "filter_tg_user_id": tg_user_id,
        "flashes": pop_flashes(request),
    })


@router.get("/partials/jobs-list")
async def jobs_list_partial(request: Request, state: Optional[str] = None, channel_pk: Optional[int] = None,
                             tg_user_id: Optional[int] = None):
    templates = request.app.state.templates
    store = request.app.state.store
    jobs = await store.list_jobs(state=state, channel_pk=channel_pk, tg_user_id=tg_user_id, limit=200)
    return templates.TemplateResponse(request, "partials/job_rows.html", {"jobs": jobs})


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request, csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    job = await store.get_job(job_id)
    if job is None:
        flash(request, "Job not found.", "error")
    elif job.state in JOB_TERMINAL_STATES:
        flash(request, "Job already finished.", "info")
    else:
        await store.transition_job(job_id, "CANCELLED")
        flash(request, f"Cancelled job #{job_id}.", "success")
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: int, request: Request, csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    job = await store.get_job(job_id)
    if job is None:
        flash(request, "Job not found.", "error")
    elif job.state != "FAILED":
        flash(request, "Only failed jobs can be retried.", "error")
    else:
        await store.transition_job(job_id, retry_target(job), attempts=0)
        flash(request, f"Retrying job #{job_id}.", "success")
    return RedirectResponse("/jobs", status_code=303)
