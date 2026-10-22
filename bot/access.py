"""Telegram-side access control. Every handler checks this first; refusal is
one line with the user's numeric ID, no menu (spec section 5.1).

Roles: owner (settings.owner_tg_id, auto-provisioned) > admin > uploader > blocked
(default for anyone the owner/admin hasn't granted access to yet).
"""
from __future__ import annotations

import functools
from typing import Awaitable, Callable

import pyrogram
import structlog

from bot.config import settings
from db.store import Store

log = structlog.get_logger(__name__)

ROLE_RANK = {"blocked": 0, "uploader": 1, "admin": 2, "owner": 3}


def refusal_text(tg_user_id: int) -> str:
    return f"🚫 You're not authorised to use this bot.\nYour ID: {tg_user_id}"


async def check_access(store: Store, tg_user_id: int, username: str | None, first_name: str | None) -> str:
    """Ensures a tg_users row exists and returns the effective role.
    The configured owner is auto-provisioned as 'owner' on first sight."""
    default_role = "owner" if tg_user_id == settings.owner_tg_id else "blocked"
    row = await store.ensure_tg_user(tg_user_id, username, first_name, default_role=default_role)
    if tg_user_id == settings.owner_tg_id and row["role"] != "owner":
        await store.set_tg_user_role(tg_user_id, "owner", added_by="system")
        return "owner"
    return row["role"]


def is_authorized(role: str) -> bool:
    return ROLE_RANK.get(role, 0) >= ROLE_RANK["uploader"]


def is_admin(role: str) -> bool:
    return ROLE_RANK.get(role, 0) >= ROLE_RANK["admin"]


async def log_denied(store: Store, tg_user_id: int, username: str | None) -> None:
    log.info("access.denied", tg_user_id=tg_user_id)
    await store.write_audit("tg_user", str(tg_user_id), "tg_access_denied", detail={"username": username})


def guard(store: Store, admin_only: bool = False) -> Callable:
    """Decorator factory for message/callback handlers with pyrogram's actual
    call signature ``(client, update)`` -- ``store`` is bound via closure at
    registration time (``@access.guard(store)`` inside ``register(app, store)``),
    so handlers never need to thread it through pyrogram's dispatch themselves.
    """

    def decorator(handler: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        @functools.wraps(handler)
        async def wrapper(client: "pyrogram.Client", update, *args, **kwargs):
            user = getattr(update, "from_user", None)
            if user is None:
                return
            role = await check_access(store, user.id, user.username, user.first_name)
            allowed = is_admin(role) if admin_only else is_authorized(role)
            if not allowed:
                if is_authorized(role):
                    text = "🚫 This command is for admins only."
                else:
                    await log_denied(store, user.id, user.username)
                    text = refusal_text(user.id)
                try:
                    if hasattr(update, "answer") and hasattr(update, "data"):  # CallbackQuery
                        await update.answer(text, show_alert=True)
                    elif hasattr(update, "reply"):  # Message
                        await update.reply(text)
                except Exception:
                    pass
                return
            return await handler(client, update, *args, **kwargs)

        return wrapper

    return decorator
