"""/start, /help, /id, /status (spec section 5.1)."""
from __future__ import annotations

import pyrogram
import structlog
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import access
from bot.config import settings
from bot.fmt import (
    BTN_HELP, BTN_MY_CHANNELS, BTN_STATUS, EMOJI_CHANNELS, EMOJI_LINKED, EMOJI_OK, EMOJI_OTHER,
    EMOJI_PLAY, EMOJI_QUOTA, EMOJI_STATUS, EMOJI_UPLOADS, EMOJI_WARN, b, emoji, esc, user_link,
)
from db.store import Store
from yt.quota import BUCKET_LIMITS, remaining

log = structlog.get_logger(__name__)

AUDIT_WARNING = (
    f"\n\n{emoji(EMOJI_WARN)} <b>Heads up:</b> this API project hasn't passed Google's compliance audit "
    f"yet, so every upload lands as <b>Private</b> until it does {emoji(EMOJI_OK)}."
)

HELP_TEXT = (
    f"{emoji(EMOJI_CHANNELS)} <b>Channels</b>\n"
    "/addyt — Add a YouTube channel\n"
    "/removeyt — Remove a channel\n"
    "/channels — List linked channels\n\n"
    f"{emoji(EMOJI_UPLOADS)} <b>Uploads</b>\n"
    "Just send a video to begin\n"
    "/jobs — Active &amp; recent uploads\n"
    "/cancel — Cancel an upload\n\n"
    f"{emoji(EMOJI_OTHER)} <b>Other</b>\n"
    "/status — Bot health &amp; quota\n"
    "/help — How it all works\n"
    "/id — Show your Telegram ID"
)


def register(app: pyrogram.Client, store: Store) -> None:
    @app.on_message(filters.command("start") & filters.private)
    @access.guard(store)
    async def start_handler(client: pyrogram.Client, message: Message) -> None:
        channels = await store.list_yt_channels()
        remaining_uploads = await remaining(store, "uploads")
        text = (
            f"👋 Welcome, {user_link(message.from_user.id, message.from_user.first_name)}!\n\n"
            f"{emoji(EMOJI_PLAY)} <b>TG → YouTube Uploader</b>\n"
            f"Send me any video and I'll upload it straight to your YouTube channel.\n\n"
            f"{HELP_TEXT}\n\n"
            f"{emoji(EMOJI_LINKED)} Linked channels: {b(len(channels))}\n"
            f"{emoji(EMOJI_QUOTA)} Uploads left today: {b(str(remaining_uploads) + '/' + str(BUCKET_LIMITS['uploads']))}"
        )
        if not settings.project_audited:
            text += AUDIT_WARNING
        # Button labels are plain text (no bold/custom-emoji entities in kurigram 2.1.38 / layer 198),
        # so the unicode fallback glyph is used and the caps carry the emphasis.
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"{BTN_MY_CHANNELS[1]} MY CHANNELS", callback_data="menu:channels"),
            InlineKeyboardButton(f"{BTN_STATUS[1]} STATUS", callback_data="menu:status"),
            InlineKeyboardButton(f"{BTN_HELP[1]} HELP", callback_data="menu:help"),
        ]])
        await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)

    @app.on_message(filters.command("help") & filters.private)
    @access.guard(store)
    async def help_handler(client: pyrogram.Client, message: Message) -> None:
        await message.reply(HELP_TEXT, parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("id") & filters.private)
    async def id_handler(client: pyrogram.Client, message: Message) -> None:
        await message.reply(f"Your Telegram ID: <code>{message.from_user.id}</code>", parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("status") & filters.private)
    @access.guard(store)
    async def status_handler(client: pyrogram.Client, message: Message) -> None:
        channels = await store.list_yt_channels()
        active = [c for c in channels if c["status"] == "active"]
        needs_reauth = [c for c in channels if c["status"] == "needs_reauth"]
        jobs = await store.list_jobs_for_user(message.from_user.id, limit=5)
        quota_summary = await store.quota_summary()
        remaining_uploads = await remaining(store, "uploads")
        lines = [
            f"{emoji(EMOJI_STATUS)} <b>Bot status</b>",
            f"Channels: {len(active)} active, {len(needs_reauth)} need re-link",
            f"Uploads left today: {remaining_uploads}/{BUCKET_LIMITS['uploads']}",
            f"Shared quota used: {quota_summary.get('general', 0)}/{BUCKET_LIMITS['general']}",
            f"Recent jobs: {len(jobs)}",
        ]
        for j in jobs:
            lines.append(f"  #{j.id} {esc(j.state)}")
        if not settings.project_audited:
            lines.append(AUDIT_WARNING.strip())
        await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)

    @app.on_callback_query(filters.regex(r"^menu:"))
    async def menu_callback(client: pyrogram.Client, cq: CallbackQuery) -> None:
        role = await access.check_access(store, cq.from_user.id, cq.from_user.username, cq.from_user.first_name)
        if not access.is_authorized(role):
            await cq.answer(access.refusal_text(cq.from_user.id), show_alert=True)
            return
        action = cq.data.split(":", 1)[1]
        if action == "help":
            await cq.message.reply(HELP_TEXT, parse_mode=ParseMode.HTML)
        elif action == "status":
            await cq.message.reply("Run /status for the full picture.")
        elif action == "channels":
            channels = await store.list_yt_channels()
            if not channels:
                await cq.message.reply("No channels linked yet. Use /addyt to add one.")
            else:
                lines = [f"{emoji(EMOJI_LINKED)} <b>Linked channels</b>"]
                for c in channels:
                    star = "⭐ " if c["is_default"] else ""
                    status_icon = {"active": "✅", "needs_reauth": "⚠️", "revoked": "🚫"}.get(c["status"], "")
                    lines.append(f"{star}{status_icon} {esc(c['channel_title'])}")
                await cq.message.reply("\n".join(lines), parse_mode=ParseMode.HTML)
        await cq.answer()
