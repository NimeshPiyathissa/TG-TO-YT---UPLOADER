"""Telegram access control: /users (spec section 6)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from bot.notify import notify_role_change
from web import auth
from web.csrf import require_csrf
from web.flash import flash, pop_flashes

router = APIRouter()

VALID_ROLES = ("owner", "admin", "uploader", "blocked")


async def _notify_admin_grant(request: Request, tg_user_id: int, previous_role, role: str) -> None:
    """DM a newly promoted admin and flash the outcome. The role is already saved by now,
    so the user has access whether or not the DM could be delivered."""
    row = await request.app.state.store.get_tg_user(tg_user_id)
    sent = await notify_role_change(
        getattr(request.app.state, "bot", None), tg_user_id, previous_role, role,
        first_name=row["first_name"] if row else None,
    )
    if sent is True:
        flash(request, f"Notified {tg_user_id} on Telegram.", "info")
    elif sent is False:
        flash(request, f"Couldn't message {tg_user_id}: Telegram only lets the bot write to people who have "
                       "started it. They already have access; ask them to send /start to the bot once.", "warn")


@router.get("/users")
async def users_list(request: Request):
    templates = request.app.state.templates
    store = request.app.state.store
    tg_users = await store.list_tg_users()
    denied = [r for r in await store.list_audit_log(limit=500) if r["action"] == "tg_access_denied"][:50]
    return templates.TemplateResponse(request, "users.html", {
        "tg_users": tg_users, "denied": denied, "flashes": pop_flashes(request),
    })


@router.post("/users/add")
async def add_user(request: Request, tg_user_id: int = Form(...), role: str = Form("uploader"),
                    csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    if role not in VALID_ROLES:
        flash(request, "Invalid role.", "error")
        return RedirectResponse("/users", status_code=303)
    admin = auth.current_user(request)
    previous = await store.get_tg_user(tg_user_id)
    previous_role = previous["role"] if previous else None
    await store.ensure_tg_user(tg_user_id, None, None, default_role=role)
    await store.set_tg_user_role(tg_user_id, role, added_by=admin["username"] if admin else None)
    await store.write_audit("app_user", admin["username"] if admin else None, "tg_user_granted",
                             target=str(tg_user_id), detail={"role": role})
    flash(request, f"Granted {role} access to {tg_user_id}.", "success")
    await _notify_admin_grant(request, tg_user_id, previous_role, role)
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{tg_user_id}/role")
async def set_role(tg_user_id: int, request: Request, role: str = Form(...), csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    if role not in VALID_ROLES:
        flash(request, "Invalid role.", "error")
        return RedirectResponse("/users", status_code=303)
    admin = auth.current_user(request)
    previous = await store.get_tg_user(tg_user_id)
    previous_role = previous["role"] if previous else None
    await store.set_tg_user_role(tg_user_id, role, added_by=admin["username"] if admin else None)
    await store.write_audit("app_user", admin["username"] if admin else None, "tg_user_role_changed",
                             target=str(tg_user_id), detail={"role": role})
    flash(request, f"Updated role for {tg_user_id}.", "success")
    if previous is not None:  # no row -> nothing was saved, so nothing to announce
        await _notify_admin_grant(request, tg_user_id, previous_role, role)
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{tg_user_id}/block")
async def block_user(tg_user_id: int, request: Request, csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    admin = auth.current_user(request)
    await store.set_tg_user_role(tg_user_id, "blocked", added_by=admin["username"] if admin else None)
    await store.write_audit("app_user", admin["username"] if admin else None, "tg_user_blocked", target=str(tg_user_id))
    flash(request, f"Blocked {tg_user_id}.", "success")
    return RedirectResponse("/users", status_code=303)
