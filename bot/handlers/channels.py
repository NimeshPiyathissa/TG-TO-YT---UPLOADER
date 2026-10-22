"""/addyt, /removeyt, /channels (spec section 5.2-5.3). Owner/admin only for
add/remove -- linking itself happens in the web UI, not in chat (spec 1.2/1.3
credential handling is web-only by design)."""
from __future__ import annotations

import pyrogram
import structlog
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import access, crypto
from bot.fmt import (
    EMOJI_ADD, EMOJI_CHANNEL_OK, EMOJI_LINKED, EMOJI_POINT, EMOJI_SEP, EMOJI_STAR, b, emoji, esc,
)
from bot.config import settings
from db.store import Store
from yt.oauth import revoke_token

log = structlog.get_logger(__name__)

# Active channels get the premium check at the end of the row; other states keep a plain icon.
STATUS_ICON = {"active": emoji(EMOJI_CHANNEL_OK), "needs_reauth": "⚠️", "revoked": "🚫"}


def web_base_url() -> str:
    return settings.ui_base_url


def register(app: pyrogram.Client, store: Store) -> None:
    @app.on_message(filters.command("addyt") & filters.private)
    @access.guard(store, admin_only=True)
    async def addyt_handler(client: pyrogram.Client, message: Message) -> None:
        base = web_base_url()
        client_row = await store.get_oauth_client()
        if client_row is None:
            await message.reply(
                "⚙️ No OAuth client is configured yet.\n\n"
                f"Open {base}/settings, upload your <code>client_secret_*.json</code> from Google Cloud "
                "Console, then come back and run /addyt again.",
                parse_mode=ParseMode.HTML,
            )
            return
        await message.reply(
            f"{emoji(EMOJI_ADD)} <b>Add a YouTube Channel</b>\n\n"
            "Open the web panel to link a channel:\n"
            f"{emoji(EMOJI_POINT)} {base}/channels/add\n\n"
            "You'll need the OAuth Client ID JSON from Google Cloud Console.\n"
            "Step-by-step guide: /help addyt",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )

    @app.on_message(filters.command("channels") & filters.private)
    @access.guard(store)
    async def channels_handler(client: pyrogram.Client, message: Message) -> None:
        await message.reply(await _render_channel_list(store), parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("removeyt") & filters.private)
    @access.guard(store, admin_only=True)
    async def removeyt_handler(client: pyrogram.Client, message: Message) -> None:
        channels = await store.list_yt_channels()
        if not channels:
            await message.reply("No channels linked yet.")
            return
        rows = [[InlineKeyboardButton(f"📺 {c['channel_title']}", callback_data=f"rmyt:{c['id']}")] for c in channels]
        await message.reply("Pick a channel to remove:", reply_markup=InlineKeyboardMarkup(rows))

    @app.on_callback_query(filters.regex(r"^rmyt:(\d+)$"))
    async def rmyt_pick(client: pyrogram.Client, cq: CallbackQuery) -> None:
        role = await access.check_access(store, cq.from_user.id, cq.from_user.username, cq.from_user.first_name)
        if not access.is_admin(role):
            await cq.answer("Admins only.", show_alert=True)
            return
        channel_pk = int(cq.data.split(":")[1])
        channel = await store.get_yt_channel(channel_pk)
        if channel is None:
            await cq.answer("Already gone.", show_alert=True)
            return
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, remove", callback_data=f"rmyt_confirm:{channel_pk}"),
            InlineKeyboardButton("❌ Cancel", callback_data="rmyt_cancel"),
        ]])
        await cq.message.edit_text(
            f"⚠️ Remove {b(channel['channel_title'])}? Videos already uploaded stay on YouTube.",
            parse_mode=ParseMode.HTML, reply_markup=kb,
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^rmyt_confirm:(\d+)$"))
    async def rmyt_confirm(client: pyrogram.Client, cq: CallbackQuery) -> None:
        role = await access.check_access(store, cq.from_user.id, cq.from_user.username, cq.from_user.first_name)
        if not access.is_admin(role):
            await cq.answer("Admins only.", show_alert=True)
            return
        channel_pk = int(cq.data.split(":")[1])
        channel = await store.get_yt_channel(channel_pk)
        if channel is None:
            await cq.answer("Already gone.", show_alert=True)
            return
        try:
            await revoke_token(crypto.decrypt(channel["refresh_token_enc"]))
        except Exception:
            log.warning("channels.revoke_failed", channel_pk=channel_pk, exc_info=True)
        await store.delete_channel(channel_pk)
        await store.write_audit("tg_user", str(cq.from_user.id), "channel_removed", target=channel["channel_title"])
        await cq.message.edit_text(f"🗑 Removed {b(channel['channel_title'])}.", parse_mode=ParseMode.HTML)
        await cq.answer("Removed.")

    @app.on_callback_query(filters.regex(r"^rmyt_cancel$"))
    async def rmyt_cancel(client: pyrogram.Client, cq: CallbackQuery) -> None:
        await cq.message.edit_text("Cancelled.")
        await cq.answer()


async def _render_channel_list(store: Store) -> str:
    channels = await store.list_yt_channels()
    if not channels:
        return "No channels linked yet. Ask an admin to run /addyt."
    lines = [f"{emoji(EMOJI_LINKED)} <b>Linked channels</b>"]
    for n, c in enumerate(channels, start=1):
        star = f"{emoji(EMOJI_STAR)} " if c["is_default"] else ""
        icon = STATUS_ICON.get(c["status"], "")
        row = f"{n}. {star}{b(c['channel_title'])} {emoji(EMOJI_SEP)} {b(c['uploads_count'])} uploads"
        lines.append(f"{row} {icon}" if icon else row)
    return "\n".join(lines)
