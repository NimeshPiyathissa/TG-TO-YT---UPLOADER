"""GET/POST /account/totp: TOTP enrollment and removal for the logged-in admin.

A session that logged in with REQUIRE_TOTP on but no secret stored is flagged
``totp_enroll_required`` by ``web/auth.py`` and the login middleware keeps it on
this page until a code from the authenticator is confirmed.
"""
from __future__ import annotations

import io

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from bot import crypto, security
from bot.config import settings as app_settings
from web import auth
from web.csrf import require_csrf
from web.flash import flash, pop_flashes

router = APIRouter()

MAX_ATTEMPTS = auth.MAX_ATTEMPTS
LOCKOUT_MINUTES = auth.LOCKOUT_MINUTES


def _qr_svg(uri: str) -> str:
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    # Drop the XML prolog so the markup can be inlined in HTML.
    return svg[svg.index("<svg"):]


def _redirect() -> RedirectResponse:
    return RedirectResponse("/account/totp", status_code=303)


@router.get("/account/totp")
async def totp_page(request: Request):
    store = request.app.state.store
    user = auth.current_user(request)
    row = await store.get_app_user(user["id"])
    enabled = row is not None and row["totp_secret_enc"] is not None

    ctx = {
        "enabled": enabled,
        "enroll_required": auth.must_enroll_totp(request),
        "require_totp": app_settings.require_totp,
        "secret": None,
        "qr_svg": None,
        "flashes": pop_flashes(request),
    }
    if not enabled:
        secret = request.session.get(auth.SESSION_PENDING_TOTP)
        if not secret:
            secret = security.generate_totp_secret()
            request.session[auth.SESSION_PENDING_TOTP] = secret
        ctx["secret"] = secret
        ctx["qr_svg"] = _qr_svg(security.totp_provisioning_uri(secret, user["username"]))
    return request.app.state.templates.TemplateResponse(request, "account_totp.html", ctx)


@router.post("/account/totp/confirm")
async def totp_confirm(request: Request, code: str = Form(...), csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    user = auth.current_user(request)
    secret = request.session.get(auth.SESSION_PENDING_TOTP)
    if not secret:
        flash(request, "That enrollment session expired. Scan the new code and try again.", "error")
        return _redirect()
    if not security.verify_totp(secret, code.strip()):
        await store.record_login_failure(user["username"], MAX_ATTEMPTS, LOCKOUT_MINUTES)
        flash(request, "That code didn't match. Check the time on your phone and try again.", "error")
        return _redirect()

    await store.set_totp_secret(user["id"], crypto.encrypt(secret))
    request.session.pop(auth.SESSION_PENDING_TOTP, None)
    request.session.pop(auth.SESSION_ENROLL_TOTP, None)
    await store.write_audit("app_user", user["username"], "totp_enabled", ip=auth.client_ip(request))
    flash(request, "Two-factor authentication is on. You'll be asked for a code at every login.", "success")
    return _redirect()


@router.post("/account/totp/disable")
async def totp_disable(request: Request, password: str = Form(...), code: str = Form(...),
                       csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    user = auth.current_user(request)
    if app_settings.require_totp:
        flash(request, "REQUIRE_TOTP is set in the env file, so two-factor can't be turned off here.", "error")
        return _redirect()

    row = await store.get_app_user(user["id"])
    if row is None or row["totp_secret_enc"] is None:
        return _redirect()
    ok = security.verify_password(row["password_hash"], password) and security.verify_totp(
        crypto.decrypt(row["totp_secret_enc"]), code.strip())
    if not ok:
        await store.record_login_failure(user["username"], MAX_ATTEMPTS, LOCKOUT_MINUTES)
        flash(request, "Password or authentication code was wrong.", "error")
        return _redirect()

    await store.set_totp_secret(user["id"], None)
    await store.write_audit("app_user", user["username"], "totp_disabled", ip=auth.client_ip(request))
    flash(request, "Two-factor authentication is off.", "success")
    return _redirect()
