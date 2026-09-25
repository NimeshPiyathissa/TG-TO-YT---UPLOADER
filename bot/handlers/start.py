"""/start, /help, /id, /status (spec section 5.1)."""
from __future__ import annotations

import pyrogram
import structlog
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import access
from bot.config import settings
from db.store import Store
from yt.quota import BUCKET_LIMITS, remaining

log = structlog.get_logger(__name__)

AUDIT_WARNING = (
    "\n\n⚠️ *Heads up:* this API project hasn't passed Google's compliance audit "
    "yet, so every upload lands as *Private* until it does."
)

HELP_TEXT = (
    "📺 *Channels*\n"
    "/addyt — Add a YouTube channel\n"
    "/removeyt — Remove a channel\n"
    "/channels — List linked channels\n\n"
    "⚡ *Uploads*\n"
    "Just send a video to begin\n"
    "/jobs — Active & recent uploads\n"
    "/cancel — Cancel an upload\n\n"
    "⚙️ *Other*\n"
    "/status — Bot health & quota\n"
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
            f"👋 Welcome, {message.from_user.first_name}!\n\n"
            f"🎬 *TG → YouTube Uploader*\n"
            f"Send me any video and I'll upload it straight to your YouTube channel.\n\n"
            f"{HELP_TEXT}\n\n"
            f"🔗 Linked channels: *{len(channels)}*\n"
            f"📊 Uploads left today: *{remaining_uploads}/{BUCKET_LIMITS['uploads']}*"
        )
        if not settings.project_audited:
            text += AUDIT_WARNING
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📺 My Channels", callback_data="menu:channels"),
            InlineKeyboardButton("⚡ Status", callback_data="menu:status"),
            InlineKeyboardButton("❓ Help", callback_data="menu:help"),
        ]])
        await message.reply(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True)

    @app.on_message(filters.command("help") & filters.private)
    @access.guard(store)
    async def help_handler(client: pyrogram.Client, message: Message) -> None:
        await message.reply(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

    @app.on_message(filters.command("id") & filters.private)
    async def id_handler(client: pyrogram.Client, message: Message) -> None:
        await message.reply(f"Your Telegram ID: `{message.from_user.id}`", parse_mode=ParseMode.MARKDOWN)

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
            "⚡ *Bot status*",
            f"Channels: {len(active)} active, {len(needs_reauth)} need re-link",
            f"Uploads left today: {remaining_uploads}/{BUCKET_LIMITS['uploads']}",
            f"Shared quota used: {quota_summary.get('general', 0)}/{BUCKET_LIMITS['general']}",
            f"Recent jobs: {len(jobs)}",
        ]
        for j in jobs:
            lines.append(f"  #{j.id} {j.state}")
        if not settings.project_audited:
            lines.append(AUDIT_WARNING.strip())
        await message.reply("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    @app.on_callback_query(filters.regex(r"^menu:"))
    async def menu_callback(client: pyrogram.Client, cq: CallbackQuery) -> None:
        role = await access.check_access(store, cq.from_user.id, cq.from_user.username, cq.from_user.first_name)
        if not access.is_authorized(role):
            await cq.answer(access.refusal_text(cq.from_user.id), show_alert=True)
            return
        action = cq.data.split(":", 1)[1]
        if action == "help":
            await cq.message.reply(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
        elif action == "status":
            await cq.message.reply("Run /status for the full picture.")
        elif action == "channels":
            channels = await store.list_yt_channels()
            if not channels:
                await cq.message.reply("No channels linked yet. Use /addyt to add one.")
            else:
                lines = ["📺 *Linked channels*"]
                for c in channels:
                    star = "⭐ " if c["is_default"] else ""
                    status_icon = {"active": "✅", "needs_reauth": "⚠️", "revoked": "🚫"}.get(c["status"], "")
                    lines.append(f"{star}{status_icon} {c['channel_title']}")
                await cq.message.reply("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        await cq.answer()
