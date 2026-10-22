"""The channel-picker and caption-prompt steps of the upload flow (spec 5.4
steps 2-3): callback handlers for picking/changing a channel, the caption
text handler, "use filename", and "cancel". Once a job reaches READY, the
upload worker in bot/queue.py picks it up on its next poll -- no direct call
needed here.
"""
from __future__ import annotations

from pathlib import Path

import pyrogram
import structlog
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from bot import access, flow
from bot.fmt import b, esc
from bot.downloader import cleanup_job_dir
from bot.queue import JobQueue
from db.store import Store

log = structlog.get_logger(__name__)


async def _require_owner_job(store: Store, tg_user_id: int, job_id: int):
    job = await store.get_job(job_id)
    if job is None or job.tg_user_id != tg_user_id:
        return None
    return job


def register(app: pyrogram.Client, store: Store, queue: JobQueue) -> None:
    @app.on_callback_query(filters.regex(r"^pickch:(\d+):(\d+)$"))
    async def pick_channel(client: pyrogram.Client, cq: CallbackQuery) -> None:
        _, job_id_s, channel_pk_s = cq.data.split(":")
        job_id, channel_pk = int(job_id_s), int(channel_pk_s)
        job = await _require_owner_job(store, cq.from_user.id, job_id)
        if job is None:
            await cq.answer("Job not found.", show_alert=True)
            return
        channel = await store.get_yt_channel(channel_pk)
        if channel is None or channel["status"] != "active":
            await cq.answer("That channel isn't available anymore.", show_alert=True)
            return

        if job.state == "AWAITING_CHANNEL":
            await store.transition_job(job_id, "AWAITING_CAPTION", channel_pk=channel_pk)
        else:
            await store.update_job_fields(job_id, channel_pk=channel_pk)
        await store.save_flow_session(cq.from_user.id, job_id, "awaiting_caption", {})

        await cq.message.edit_text(
            flow.caption_prompt_text(channel["channel_title"]),
            parse_mode=ParseMode.HTML,
            reply_markup=flow.caption_prompt_keyboard(job_id, single_channel=False),
        )
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^changechannel:(\d+)$"))
    async def change_channel(client: pyrogram.Client, cq: CallbackQuery) -> None:
        job_id = int(cq.data.split(":")[1])
        job = await _require_owner_job(store, cq.from_user.id, job_id)
        if job is None:
            await cq.answer("Job not found.", show_alert=True)
            return
        channels = await store.list_yt_channels()
        active = [c for c in channels if c["status"] == "active"]
        await store.save_flow_session(cq.from_user.id, job_id, "awaiting_channel", {})
        text = flow.channel_picker_text(job.file_name or "video", job.size_bytes or 0,
                                         job.duration_s, job.width, job.height)
        await cq.message.edit_text(text, parse_mode=ParseMode.HTML,
                                    reply_markup=flow.channel_picker_keyboard(active, job_id))
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^usefilename:(\d+)$"))
    async def use_filename(client: pyrogram.Client, cq: CallbackQuery) -> None:
        job_id = int(cq.data.split(":")[1])
        job = await _require_owner_job(store, cq.from_user.id, job_id)
        if job is None or job.state != "AWAITING_CAPTION" or not job.channel_pk:
            await cq.answer("This job isn't waiting for a caption.", show_alert=True)
            return
        title = Path(job.file_name or "video").stem[:100] or "Untitled"
        await store.transition_job(job_id, "READY", title=title, description="")
        await store.clear_flow_session(cq.from_user.id)
        await cq.message.edit_text(f"🎬 Title set to {b(title)}.\n\n⏳ Starting upload…", parse_mode=ParseMode.HTML)
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^canceljob:(\d+)$"))
    async def cancel_job_cb(client: pyrogram.Client, cq: CallbackQuery) -> None:
        job_id = int(cq.data.split(":")[1])
        job = await _require_owner_job(store, cq.from_user.id, job_id)
        if job is None:
            await cq.answer("Job not found.", show_alert=True)
            return
        await store.clear_flow_session(cq.from_user.id)
        if job.state in ("AWAITING_CHANNEL", "AWAITING_CAPTION"):
            await store.transition_job(job_id, "CANCELLED")
            cleanup_job_dir(job_id)
        else:
            queue.cancel_job(job_id)
        await cq.message.edit_text(f"❌ Cancelled job #{job_id}.")
        await cq.answer()

    @app.on_message(filters.text & filters.private & ~filters.via_bot)
    @access.guard(store)
    async def caption_text(client: pyrogram.Client, message: Message) -> None:
        session = await store.get_flow_session(message.from_user.id)
        if session is None or session["step"] != "awaiting_caption" or session["job_id"] is None:
            return  # not in the caption step -- let other handlers/plain chatter pass through
        job_id = session["job_id"]
        job = await _require_owner_job(store, message.from_user.id, job_id)
        if job is None or job.state != "AWAITING_CAPTION" or not job.channel_pk:
            await store.clear_flow_session(message.from_user.id)
            return
        try:
            title, description = flow.validate_caption(message.text)
        except flow.ValidationError as exc:
            channel = await store.get_yt_channel(job.channel_pk)
            await message.reply(
                f"⚠️ {esc(exc)}\n\n" + flow.caption_prompt_text(channel["channel_title"] if channel else ""),
                parse_mode=ParseMode.HTML,
                reply_markup=flow.caption_prompt_keyboard(job_id, single_channel=False),
            )
            return
        await store.transition_job(job_id, "READY", title=title, description=description)
        await store.clear_flow_session(message.from_user.id)
        try:
            await client.edit_message_text(
                job.chat_id, job.status_message_id,
                f"🎬 Title set to {b(title)}.\n\n⏳ Starting upload…", parse_mode=ParseMode.HTML,
            )
        except Exception:
            log.warning("caption.status_edit_failed", job_id=job_id, exc_info=True)
