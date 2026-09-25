"""GET/POST /settings (spec section 6): OAuth client, PROJECT_AUDITED toggle,
defaults, concurrency/chunk overrides, retention, quota, restart button, redacted env view."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Form, Request

from bot.config import settings as app_settings
from web import auth, envfile
from web.csrf import require_csrf
from web.flash import flash, pop_flashes
from yt.quota import BUCKET_LIMITS

router = APIRouter()


@router.get("/settings")
async def settings_page(request: Request):
    templates = request.app.state.templates
    store = request.app.state.store
    db_settings = await store.all_settings()
    quota_summary = await store.quota_summary()
    return templates.TemplateResponse(request, "settings.html", {
        "audited": app_settings.project_audited,
        "db_settings": db_settings,
        "quota_summary": quota_summary,
        "bucket_limits": BUCKET_LIMITS,
        "env_lines": envfile.redacted_env_lines(),
        "flashes": pop_flashes(request),
    })


@router.post("/settings/project-audited")
async def toggle_audited(request: Request, value: str = Form(...), csrf_ok: None = Depends(require_csrf)):
    new_value = "true" if value == "true" else "false"
    ok = envfile.set_env_value("PROJECT_AUDITED", new_value)
    user = auth.current_user(request)
    if ok:
        await request.app.state.store.write_audit("app_user", user["username"] if user else None,
                                                    "settings_changed", target="PROJECT_AUDITED", detail={"value": new_value})
        flash(request, f"PROJECT_AUDITED set to {new_value}. Restart the service for it to take effect.", "success")
    else:
        flash(request, "Couldn't write the env file. Check permissions.", "error")
    return _redirect()


@router.post("/settings/defaults")
async def save_defaults(request: Request, default_privacy: str = Form("private"),
                         default_category_id: str = Form(""), csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    await store.set_setting("default_privacy", default_privacy)
    await store.set_setting("default_category_id", default_category_id)
    flash(request, "Defaults saved.", "success")
    return _redirect()


@router.post("/settings/limits")
async def save_limits(request: Request, max_concurrent_downloads: int = Form(2),
                       max_concurrent_uploads: int = Form(1), upload_chunk_bytes: int = Form(8388608),
                       job_retention_hours: int = Form(24), csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    # Persisted as an override layer for operator visibility/tuning; the running
    # queue/upload workers read their concurrency and chunk size from the env-driven
    # `bot.config.settings` at process start, so a change here takes effect on the
    # next `systemctl restart tgyt` rather than live -- out of this route's scope
    # to hot-reload worker pools.
    await store.set_setting("max_concurrent_downloads", max_concurrent_downloads)
    await store.set_setting("max_concurrent_uploads", max_concurrent_uploads)
    await store.set_setting("upload_chunk_bytes", upload_chunk_bytes)
    await store.set_setting("job_retention_hours", job_retention_hours)
    flash(request, "Saved. Restart the service for these to take effect.", "success")
    return _redirect()


@router.post("/settings/restart")
async def restart_service(request: Request, csrf_ok: None = Depends(require_csrf)):
    user = auth.current_user(request)
    await request.app.state.store.write_audit("app_user", user["username"] if user else None, "service_restart_requested")
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "/bin/systemctl", "restart", "tgyt.service",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            flash(request, "Restart requested. The panel will be briefly unreachable.", "success")
        else:
            flash(request, "Restart command failed. Check the sudoers rule (deploy/install.sh) and try `systemctl restart tgyt` by hand.", "error")
    except (FileNotFoundError, asyncio.TimeoutError) as exc:
        flash(request, f"Couldn't run the restart command: {exc}", "error")
    return _redirect()


def _redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/settings", status_code=303)
