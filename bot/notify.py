"""Telegram notifications triggered from the web Admin Panel.

The web app runs in the same process as the bot, so ``main.py`` hands it the live
bot client (``app.state.bot``). Roles themselves need no push: ``bot.access`` reads
``tg_users.role`` from the shared DB on every update, so a change made in the panel
applies on the user's very next message. This module only tells the user about it.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pyrogram
import structlog
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, RPCError

from bot.access import ROLE_RANK
from bot.fmt import EMOJI_OK, b, emoji, esc

log = structlog.get_logger(__name__)

SEND_TIMEOUT_S = 10.0


def admin_granted_text(first_name: Optional[str]) -> str:
    from bot.handlers.start import HELP_TEXT

    greeting = f"Hi {esc(first_name)}, " if first_name else ""
    return (
        f"{emoji(EMOJI_OK)} {b('Admin access granted')}\n\n"
        f"{greeting}you've been given <b>Admin</b> access to this bot. "
        "You can now use every command and feature, including adding and removing "
        "YouTube channels. The change is already active.\n\n"
        f"{HELP_TEXT}\n\n"
        "Send /start to open the menu."
    )


async def notify_role_change(
    bot: Optional[pyrogram.Client], tg_user_id: int, previous_role: Optional[str], new_role: str,
    first_name: Optional[str] = None,
) -> Optional[bool]:
    """DM the user when they are newly promoted to admin.

    Returns None when no notification is due (not a promotion, no bot handle, or
    the role didn't change), True when delivered, False when Telegram refused or
    the call failed. Never raises: a failed DM must not break the role change.
    """
    # Only a genuine promotion counts: owner -> admin is a demotion and admin -> admin a no-op.
    if new_role != "admin" or bot is None or ROLE_RANK.get(previous_role or "blocked", 0) >= ROLE_RANK["admin"]:
        return None
    try:
        await asyncio.wait_for(
            bot.send_message(tg_user_id, admin_granted_text(first_name), parse_mode=ParseMode.HTML),
            timeout=SEND_TIMEOUT_S,
        )
    except FloodWait as e:
        log.warning("notify.flood_wait", tg_user_id=tg_user_id, seconds=e.value)
        return False
    except (RPCError, asyncio.TimeoutError, ValueError, KeyError) as e:
        # PeerIdInvalid / UserIsBlocked / InputUserDeactivated: bots can't open a chat with
        # someone who has never pressed Start. ValueError/KeyError: pyrogram can't resolve the id.
        log.info("notify.admin_granted_undelivered", tg_user_id=tg_user_id, error=type(e).__name__)
        return False
    except Exception:
        log.warning("notify.admin_granted_failed", tg_user_id=tg_user_id, exc_info=True)
        return False
    log.info("notify.admin_granted_sent", tg_user_id=tg_user_id)
    return True
