"""FastAPI app factory for the admin web UI (spec section 6).

``bot/main.py`` is the only caller: ``from web.app import create_app; app =
create_app(store)``. It runs uvicorn itself (TLS in loopback mode, or plain
HTTP on 127.0.0.1:8001 behind nginx in hostname mode) -- this module only
builds the app object.
"""
from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bot.config import settings
from db.store import Store
from web import auth
from web.csrf import csrf_token
from web.flash import flash, pop_flashes

log = structlog.get_logger(__name__)

BASE_DIR = Path(__file__).parent
PUBLIC_PATHS = {"/login", "/oauth/callback"}


def create_app(store: Store) -> FastAPI:
    app = FastAPI(title="TG->YT Uploader", docs_url=None, redoc_url=None)
    app.state.store = store

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.globals["csrf_token"] = csrf_token
    templates.env.globals["current_user"] = auth.current_user
    templates.env.globals["oauth_mode"] = settings.oauth_mode
    templates.env.globals["project_audited"] = lambda: settings.project_audited
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    # Starlette's `add_middleware` inserts at the front of the internal list, and
    # the stack is built by wrapping in *that* order -- so the middleware added
    # LAST ends up OUTERMOST and runs FIRST on a request. require_login reads
    # request.session, so SessionMiddleware must be added AFTER it (i.e. end up
    # outermost) or request.session doesn't exist yet when require_login runs.
    # Registering these in the "wrong" order threw
    # `AssertionError: SessionMiddleware must be installed to access request.session`
    # on every request in production -- don't reorder these two without re-testing.
    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path not in PUBLIC_PATHS and not path.startswith("/static"):
            if auth.current_user(request) is None:
                return RedirectResponse(f"/login?next={path}", status_code=303)
            if auth.must_enroll_totp(request) and not path.startswith(auth.ENROLLMENT_PATHS):
                return RedirectResponse("/account/totp", status_code=303)
        return await call_next(request)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="tgyt_session",
        https_only=True,
        same_site="strict",
        max_age=12 * 3600,
    )

    from web.routes import account, channels, dashboard, jobs, logs, users, videos
    from web.routes import settings as settings_routes

    for module in (dashboard, channels, users, jobs, videos, logs, settings_routes, account):
        app.include_router(module.router)

    @app.get("/login")
    async def login_form(request: Request):
        if auth.current_user(request) is not None:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": None, "needs_totp": False})

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        submitted_csrf = form.get("csrf_token", "")
        expected = request.session.get("csrf_token")
        if not expected or submitted_csrf != expected:
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Session expired, please try again.", "needs_totp": False}, status_code=403,
            )
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        totp_code = (form.get("totp_code") or "").strip() or None
        ip = auth.client_ip(request)
        result = await auth.attempt_login(store, ip, username, password, totp_code, settings.require_totp)
        if not result.ok:
            if result.needs_totp:
                return templates.TemplateResponse(
                    request, "login.html",
                    {"error": result.reason or "Enter your authentication code.", "needs_totp": True,
                     "username": username}, status_code=401,
                )
            return templates.TemplateResponse(
                request, "login.html", {"error": result.reason, "needs_totp": False}, status_code=401,
            )
        auth.log_in_session(request, result.user_row, enroll_totp=result.enroll_required)
        await store.write_audit("app_user", username, "login_success", ip=ip)
        if result.enroll_required:
            return RedirectResponse("/account/totp", status_code=303)
        next_url = request.query_params.get("next") or "/"
        return RedirectResponse(next_url if next_url.startswith("/") else "/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        user = auth.current_user(request)
        auth.log_out_session(request)
        if user:
            await store.write_audit("app_user", user["username"], "logout")
        return RedirectResponse("/login", status_code=303)

    @app.get("/oauth/callback")
    async def oauth_callback(request: Request, code: str | None = None, state: str | None = None,
                              error: str | None = None):
        from web.routes.channels import finalize_channel_from_code

        if error:
            flash(request, f"Linking cancelled: {error}", "error")
            return RedirectResponse("/channels", status_code=303)
        if not code or not state:
            flash(request, "Google didn't return an authorization code.", "error")
            return RedirectResponse("/channels", status_code=303)

        state_row = await store.consume_oauth_state(state)
        if state_row is None:
            flash(request, "That linking session expired or was already used. Try again.", "error")
            return RedirectResponse("/channels", status_code=303)
        if state_row["mode"] != "hostname":
            flash(request, "Unexpected linking mode for this callback.", "error")
            return RedirectResponse("/channels", status_code=303)

        try:
            channel = await finalize_channel_from_code(store, code, state_row, settings.redirect_uri)
        except Exception as exc:
            log.warning("oauth_callback.failed", error=str(exc))
            flash(request, f"Linking failed: {exc}", "error")
            return RedirectResponse("/channels", status_code=303)

        user = auth.current_user(request)
        await store.write_audit("app_user", user["username"] if user else None, "channel_linked",
                                 target=channel["channel_title"])
        flash(request, f"Linked channel: {channel['channel_title']}", "success")
        return RedirectResponse("/channels", status_code=303)

    return app
