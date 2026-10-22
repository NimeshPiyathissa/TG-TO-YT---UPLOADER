"""Video ingest: video messages, video-mime documents, and forwards (spec 5.4
step 1). Runs the pre-flight checks, creates the job row, and posts the
initial status message -- the download worker in bot/queue.py picks the job
up from QUEUED on its next poll and takes it from there.
"""
from __future__ import annotations

import pyrogram
import structlog
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from bot import access
from bot.config import settings
from bot.downloader import InsufficientDisk, check_disk_space
from bot.queue import JobQueue
from db.store import Store
from yt.quota import BUCKET_LIMITS, remaining, seconds_until_pt_midnight

log = structlog.get_logger(__name__)

VIDEO_MIME_PREFIX = "video/"
OCTET_STREAM = "application/octet-stream"

INITIAL_TEXT = "📥 <b>Downloading from Telegram</b>\n<code>░░░░░░░░░░░░░░░░</code> 0%\nStarting…"


def _extract_media(message: Message):
    if message.video:
        return message.video
    if message.document and (
        (message.document.mime_type or "").startswith(VIDEO_MIME_PREFIX)
        or message.document.mime_type == OCTET_STREAM
    ):
        return message.document
    return None


def register(app: pyrogram.Client, store: Store, queue: JobQueue) -> None:
    @app.on_message(filters.private & (filters.video | filters.document) & ~filters.via_bot)
    @access.guard(store)
    async def video_handler(client: pyrogram.Client, message: Message) -> None:
        await _handle(client, store, message, message)

    async def _handle(client: pyrogram.Client, store: Store, message: Message, source: Message) -> None:
        user_id = message.from_user.id
        media = _extract_media(source)
        if media is None:
            await message.reply("🚫 That file isn't a video YouTube will accept.")
            return

        channels = await store.list_yt_channels()
        active = [c for c in channels if c["status"] == "active"]
        if not active:
            await message.reply("📺 No YouTube channels linked yet. Ask an admin to run /addyt first.")
            return

        size = media.file_size or 0
        if size > settings.max_file_bytes:
            got_mb = size / 1024 / 1024
            max_mb = settings.max_file_bytes / 1024 / 1024
            await message.reply(f"📦 That file is {got_mb:.0f} MB, over the {max_mb:.0f} MB limit.")
            return

        if size > 20 * 1024 * 1024 and not settings.user_session:
            await message.reply(
                "📦 That file is over 20 MB and no relay account (USER_SESSION) is configured, "
                "so I can't download it. Ask the owner to set one up."
            )
            return

        try:
            check_disk_space(size, committed_bytes=await store.queued_bytes())
        except InsufficientDisk as exc:
            need_gb, free_gb = exc.need_bytes / 1024**3, exc.free_bytes / 1024**3
            await message.reply(f"💾 Need {need_gb:.1f} GB free, only {free_gb:.1f} GB available.")
            return

        left = await remaining(store, "uploads")
        if left <= 0:
            hours = seconds_until_pt_midnight() / 3600
            await message.reply(
                f"📊 Daily upload limit reached (100/day). Resets midnight PT -- about {hours:.1f}h. "
                "Your video will stay queued until then.",
            )

        # Settings-page values win over the env file; the job freezes them at
        # creation so a later change doesn't alter a video already in flight.
        privacy = await store.get_setting("default_privacy", settings.default_privacy)
        category_id = await store.get_setting("default_category_id", "") or None

        job_id = await store.create_job(
            privacy=privacy,
            category_id=category_id,
            tg_user_id=user_id,
            chat_id=message.chat.id,
            tg_file_id=media.file_id,
            tg_file_unique_id=media.file_unique_id,
            source_chat_id=source.chat.id,
            source_message_id=source.id,
            file_name=getattr(media, "file_name", None) or "video.mp4",
            size_bytes=size,
            mime=getattr(media, "mime_type", None) or "video/mp4",
        )
        status_msg = await message.reply(INITIAL_TEXT, parse_mode=ParseMode.HTML)
        await store.update_job_fields(job_id, status_message_id=status_msg.id)
        log.info("ingest.job_created", job_id=job_id, user_id=user_id, size=size)
