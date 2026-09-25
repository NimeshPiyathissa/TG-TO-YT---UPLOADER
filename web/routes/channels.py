"""Channel management + both OAuth linking modes (spec sections 1.1, 1.2, 6)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from bot import crypto, security
from bot.config import settings
from web import auth
from web.csrf import require_csrf
from web.flash import flash, pop_flashes
from yt.credentials import (InvalidCredentialFile, OfflineToken, ServiceAccountRejected,
                             parse_client_secret, parse_token_json)
from yt.oauth import (LoopbackCallbackServer, OAuthError, build_authorization_url,
                       exchange_code, generate_pkce_pair, refresh_access_token, revoke_token)
from yt.videos import list_my_channels

log = structlog.get_logger(__name__)
router = APIRouter()

STATUS_LABEL = {"active": "✅ Active", "needs_reauth": "⚠️ Needs re-auth", "revoked": "🚫 Revoked"}

# In-memory tracker for Mode B's background linking task. Single-process
# deployment (one uvicorn worker), so a module-level dict is sufficient --
# it does not need to survive a restart, unlike everything in the DB.
_loopback_sessions: dict[str, dict] = {}


async def finalize_channel_from_code(store, code: str, state_row, redirect_uri: str) -> dict:
    """Shared by the Mode A `/oauth/callback` route and Mode B's background
    task: exchanges the code, looks up channel identity, encrypts + stores it."""
    client_row = await store.get_oauth_client()
    if client_row is None:
        raise RuntimeError("No OAuth client is configured.")
    client_id = client_row["client_id"]
    client_secret = crypto.decrypt(client_row["client_secret_enc"])
    code_verifier = crypto.decrypt(state_row["code_verifier_enc"])
    tok = await exchange_code(client_id, client_secret, code, code_verifier, redirect_uri)
    if not tok.refresh_token:
        raise RuntimeError(
            "Google didn't return a refresh token. This usually means the account already "
            "granted access without 'prompt=consent' sticking -- try removing app access at "
            "https://myaccount.google.com/permissions and linking again."
        )
    channels = await list_my_channels(tok.access_token)
    if not channels:
        raise RuntimeError("That Google account has no YouTube channel to link.")
    ch = channels[0]
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=tok.expires_in)).isoformat()
    channel_pk = await store.upsert_yt_channel(
        ch["channel_id"], ch["title"], ch["handle"], ch["thumbnail_url"],
        crypto.encrypt(tok.refresh_token), crypto.encrypt(tok.access_token), expires_at,
        tok.scope, state_row["created_by"],
    )
    return {"channel_pk": channel_pk, "channel_title": ch["title"]}


async def _run_loopback_link(store, state: str) -> None:
    server = LoopbackCallbackServer(settings.loopback_port)
    try:
        await server.start()
    except OSError as exc:
        _loopback_sessions[state] = {"status": "error", "error": f"Couldn't bind the loopback listener: {exc}"}
        return
    try:
        code, returned_state = await server.wait_for_callback(timeout=300)
        if returned_state != state:
            raise OAuthError("State mismatch -- the callback didn't match this linking session.")
        state_row = await store.consume_oauth_state(state)
        if state_row is None:
            raise OAuthError("This linking session expired or was already used.")
        channel = await finalize_channel_from_code(store, code, state_row, settings.redirect_uri)
        _loopback_sessions[state] = {"status": "done", "channel": channel}
    except Exception as exc:
        log.warning("channels.loopback_link_failed", error=str(exc))
        _loopback_sessions[state] = {"status": "error", "error": str(exc)}
    finally:
        await server.stop()


def _web_base_url() -> str:
    host = settings.public_host if settings.oauth_mode == "hostname" else (settings.public_ip or "<vps-ip>")
    return f"https://{host}:{settings.web_port}"


@router.get("/channels")
async def channels_list(request: Request):
    templates = request.app.state.templates
    store = request.app.state.store
    channels = await store.list_yt_channels()
    return templates.TemplateResponse(request, "channels.html", {
        "channels": channels, "status_label": STATUS_LABEL, "flashes": pop_flashes(request),
    })


@router.get("/channels/add")
async def add_channel_page(request: Request):
    templates = request.app.state.templates
    store = request.app.state.store
    client_row = await store.get_oauth_client()
    return templates.TemplateResponse(request, "channels_add.html", {
        "client_row": client_row,
        "redirect_uri": settings.redirect_uri,
        "oauth_mode": settings.oauth_mode,
        "loopback_port": settings.loopback_port,
        "ssh_cmd": f"ssh -L {settings.loopback_port}:127.0.0.1:{settings.loopback_port} azureuser@{settings.public_ip or '<vps-ip>'}",
        "flashes": pop_flashes(request),
    })


@router.post("/channels/add/client-secret")
async def add_client_secret(request: Request, file: UploadFile = File(...), csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    raw = await file.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        flash(request, "File too large (max 1MB).", "error")
        return RedirectResponse("/channels/add", status_code=303)
    try:
        cs = parse_client_secret(raw)
    except ServiceAccountRejected as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/channels/add", status_code=303)
    except InvalidCredentialFile as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/channels/add", status_code=303)

    user = auth.current_user(request)
    await store.set_oauth_client(cs.client_id, crypto.encrypt(cs.client_secret), cs.client_type,
                                  cs.project_id, user["username"] if user else None)
    await store.write_audit("app_user", user["username"] if user else None, "oauth_client_uploaded")
    flash(request, f"OAuth client saved. Register this redirect URI in Google Cloud Console: {settings.redirect_uri}",
          "success")
    return RedirectResponse("/channels/add", status_code=303)


@router.post("/channels/link/start")
async def link_start(request: Request, csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    client_row = await store.get_oauth_client()
    if client_row is None:
        flash(request, "Upload a client_secret.json first.", "error")
        return RedirectResponse("/channels/add", status_code=303)
    verifier, challenge = generate_pkce_pair()
    user = auth.current_user(request)
    state = await store.create_oauth_state(user["username"] if user else "unknown", "hostname", crypto.encrypt(verifier))
    url = build_authorization_url(client_row["client_id"], settings.redirect_uri, state, challenge)
    return RedirectResponse(url, status_code=303)


@router.post("/channels/link/loopback/start")
async def loopback_start(request: Request, csrf_ok: None = Depends(require_csrf)):
    templates = request.app.state.templates
    store = request.app.state.store
    client_row = await store.get_oauth_client()
    if client_row is None:
        flash(request, "Upload a client_secret.json first.", "error")
        return RedirectResponse("/channels/add", status_code=303)
    verifier, challenge = generate_pkce_pair()
    user = auth.current_user(request)
    state = await store.create_oauth_state(user["username"] if user else "unknown", "loopback", crypto.encrypt(verifier))
    auth_url = build_authorization_url(client_row["client_id"], settings.redirect_uri, state, challenge)
    _loopback_sessions[state] = {"status": "pending"}
    asyncio.create_task(_run_loopback_link(store, state))
    return templates.TemplateResponse(request, "channels_loopback.html", {
        "state": state, "auth_url": auth_url, "status": "pending",
        "ssh_cmd": f"ssh -L {settings.loopback_port}:127.0.0.1:{settings.loopback_port} azureuser@{settings.public_ip or '<vps-ip>'}",
    })


@router.get("/channels/link/loopback/status")
async def loopback_status(request: Request, state: str):
    templates = request.app.state.templates
    store = request.app.state.store
    session = _loopback_sessions.get(state)
    if session is None:
        return templates.TemplateResponse(request, "partials/loopback_status.html", {"status": "unknown", "state": state})
    if session["status"] == "done":
        user = auth.current_user(request)
        await store.write_audit("app_user", user["username"] if user else None, "channel_linked",
                                 target=session["channel"]["channel_title"])
        del _loopback_sessions[state]
    elif session["status"] == "error":
        del _loopback_sessions[state]
    return templates.TemplateResponse(request, "partials/loopback_status.html", {
        "status": session["status"], "channel": session.get("channel"), "error": session.get("error"), "state": state,
    })


@router.get("/channels/link/download-helper")
async def download_helper():
    path = Path(__file__).resolve().parents[2] / "tools" / "link_channel.py"
    return FileResponse(path, filename="link_channel.py", media_type="text/x-python")


@router.post("/channels/link/upload-token")
async def upload_token(request: Request, file: UploadFile = File(...), csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    raw = await file.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        flash(request, "File too large (max 1MB).", "error")
        return RedirectResponse("/channels/add", status_code=303)
    try:
        offline: OfflineToken = parse_token_json(raw)
    except InvalidCredentialFile as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/channels/add", status_code=303)

    client_id, client_secret = offline.client_id, offline.client_secret
    if not client_id or not client_secret:
        client_row = await store.get_oauth_client()
        if client_row is None:
            flash(request, "token.json has no embedded client credentials and no OAuth client is configured.", "error")
            return RedirectResponse("/channels/add", status_code=303)
        client_id = client_row["client_id"]
        client_secret = crypto.decrypt(client_row["client_secret_enc"])

    try:
        tok = await refresh_access_token(client_id, client_secret, offline.refresh_token)
        channels = await list_my_channels(tok.access_token)
    except OAuthError as exc:
        flash(request, f"Couldn't validate token.json: {exc}", "error")
        return RedirectResponse("/channels/add", status_code=303)
    if not channels:
        flash(request, "That token.json's Google account has no YouTube channel.", "error")
        return RedirectResponse("/channels/add", status_code=303)

    ch = channels[0]
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=tok.expires_in)).isoformat()
    user = auth.current_user(request)
    scopes = " ".join(offline.scopes) if offline.scopes else "https://www.googleapis.com/auth/youtube.upload"
    await store.upsert_yt_channel(
        ch["channel_id"], ch["title"], ch["handle"], ch["thumbnail_url"],
        crypto.encrypt(offline.refresh_token), crypto.encrypt(tok.access_token), expires_at,
        scopes, user["username"] if user else None,
    )
    await store.write_audit("app_user", user["username"] if user else None, "channel_linked", target=ch["title"])
    flash(request, f"Linked channel: {ch['title']}", "success")
    return RedirectResponse("/channels", status_code=303)


@router.post("/channels/{channel_pk}/set-default")
async def set_default(channel_pk: int, request: Request, csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    await store.set_default_channel(channel_pk)
    flash(request, "Default channel updated.", "success")
    return RedirectResponse("/channels", status_code=303)


@router.post("/channels/{channel_pk}/reauthorize")
async def reauthorize(channel_pk: int, request: Request, csrf_ok: None = Depends(require_csrf)):
    if settings.oauth_mode == "hostname":
        return await link_start(request, csrf_ok)
    return await loopback_start(request, csrf_ok)


@router.get("/channels/{channel_pk}/remove-confirm")
async def remove_confirm(channel_pk: int, request: Request):
    templates = request.app.state.templates
    store = request.app.state.store
    channel = await store.get_yt_channel(channel_pk)
    return templates.TemplateResponse(request, "partials/channel_remove_confirm.html", {"channel": channel})


@router.post("/channels/{channel_pk}/remove")
async def remove_channel(channel_pk: int, request: Request, csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    channel = await store.get_yt_channel(channel_pk)
    if channel is None:
        flash(request, "Already removed.", "info")
        return RedirectResponse("/channels", status_code=303)
    try:
        await revoke_token(crypto.decrypt(channel["refresh_token_enc"]))
    except Exception:
        log.warning("channels.revoke_failed", channel_pk=channel_pk, exc_info=True)
    await store.delete_channel(channel_pk)
    user = auth.current_user(request)
    await store.write_audit("app_user", user["username"] if user else None, "channel_removed",
                             target=channel["channel_title"])
    flash(request, f"Removed {channel['channel_title']}.", "success")
    return RedirectResponse("/channels", status_code=303)
