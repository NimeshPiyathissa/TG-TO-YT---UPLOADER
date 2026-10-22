"""/jobs, /cancel, and the inline-button callbacks from the done/failure
messages: retry, discard, delete-video, upload-another (spec 5.4 steps 5-6)."""
from __future__ import annotations

import pyrogram
import structlog
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from bot import access
from bot.fmt import esc
from bot.downloader import cleanup_job_dir
from bot.queue import JobQueue
from db.store import Store
from yt.tokens import NeedsReauth, get_valid_access_token
from yt.oauth import can_delete
from yt.videos import delete_video

log = structlog.get_logger(__name__)

ACTIVE_STATES = {"QUEUED", "DOWNLOADING", "AWAITING_CHANNEL", "AWAITING_CAPTION", "READY", "UPLOADING", "VERIFYING"}
STATE_ICON = {"DONE": "✅", "FAILED": "⚫️", "CANCELLED": "⚫️"}


def register(app: pyrogram.Client, store: Store, queue: JobQueue) -> None:
    @app.on_message(filters.command("jobs") & filters.private)
    @access.guard(store)
    async def jobs_handler(client: pyrogram.Client, message: Message) -> None:
        jobs = await store.list_jobs_for_user(message.from_user.id, limit=10)
        if not jobs:
            await message.reply("No jobs yet -- send a video to get started.")
            return
        lines = []
        for j in jobs:
            marker = "🟢" if j.state in ACTIVE_STATES else STATE_ICON.get(j.state, "⚫️")
            lines.append(f"{marker} #{j.id} {esc(j.state)} {esc(j.title or '')}".rstrip())
        await message.reply("\n".join(lines) + "\n\nUse /cancel &lt;id&gt; to cancel an active job.")

    @app.on_message(filters.command("cancel") & filters.private)
    @access.guard(store)
    async def cancel_handler(client: pyrogram.Client, message: Message) -> None:
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.reply("Usage: /cancel &lt;job id&gt; -- see /jobs for the list.")
            return
        job_id = int(parts[1])
        job = await store.get_job(job_id)
        if job is None or job.tg_user_id != message.from_user.id:
            await message.reply("Job not found.")
            return
        if job.state not in ACTIVE_STATES:
            await message.reply(f"Job #{job_id} is already {job.state.lower()}.")
            return

        if not queue.cancel_job(job_id):
            try:
                await store.transition_job(job_id, "CANCELLED")
            except Exception:
                pass
            cleanup_job_dir(job_id)
            await store.clear_flow_session(message.from_user.id)
        await message.reply(f"Cancelling job #{job_id}…")

    @app.on_callback_query(filters.regex(r"^retryjob:(\d+)$"))
    async def retry_cb(client: pyrogram.Client, cq: CallbackQuery) -> None:
        job_id = int(cq.data.split(":")[1])
        job = await store.get_job(job_id)
        if job is None or job.tg_user_id != cq.from_user.id:
            await cq.answer("Job not found.", show_alert=True)
            return
        ok = await queue.retry_job(job_id)
        if not ok:
            await cq.answer("Can't retry this job anymore.", show_alert=True)
            return
        await cq.message.edit_text(f"🔁 Retrying job #{job_id}…")
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^discardjob:(\d+)$"))
    async def discard_cb(client: pyrogram.Client, cq: CallbackQuery) -> None:
        job_id = int(cq.data.split(":")[1])
        job = await store.get_job(job_id)
        if job is None or job.tg_user_id != cq.from_user.id:
            await cq.answer("Job not found.", show_alert=True)
            return
        cleanup_job_dir(job_id)
        await cq.message.edit_text(f"🗑 Discarded job #{job_id}.")
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^deletevideo:(.+)$"))
    async def delete_video_cb(client: pyrogram.Client, cq: CallbackQuery) -> None:
        video_id = cq.data.split(":", 1)[1]
        row = await store.get_video(video_id)
        if row is None or row["deleted_at"] is not None:
            await cq.answer("Already deleted.", show_alert=True)
            return
        if row["uploaded_by"] != cq.from_user.id:
            role = await access.check_access(store, cq.from_user.id, cq.from_user.username, cq.from_user.first_name)
            if not access.is_admin(role):
                await cq.answer("Only the uploader or an admin can delete this.", show_alert=True)
                return
        channel = await store.get_yt_channel(row["channel_pk"]) if row["channel_pk"] else None
        if channel is not None and not can_delete(channel["scopes"]):
            # Linked before delete permission was requested: YouTube would answer 403, so say why.
            await cq.answer("This channel needs to be re-authorised once (all permissions ticked) before "
                            "videos can be deleted. An admin can do that on the web panel.", show_alert=True)
            return
        try:
            token = await get_valid_access_token(store, row["channel_pk"])
        except NeedsReauth:
            await cq.answer("That channel needs to be re-authorised first.", show_alert=True)
            return
        try:
            await delete_video(token, video_id)
        except Exception:
            log.warning("jobs.delete_video_failed", video_id=video_id, exc_info=True)
            await cq.answer("Delete failed -- try again from the web panel.", show_alert=True)
            return
        await store.soft_delete_video(video_id, deleted_by=f"tg:{cq.from_user.id}")
        await store.write_audit("tg_user", str(cq.from_user.id), "video_deleted", target=video_id)
        await cq.message.edit_text(f"🗑 Deleted from YouTube: {esc(row['title'])}")
        await cq.answer("Deleted.")

    @app.on_callback_query(filters.regex(r"^acct$"))
    async def account_header_cb(client: pyrogram.Client, cq: CallbackQuery) -> None:
        await cq.answer()  # the Google-account label rows in the channel picker aren't actions

    @app.on_callback_query(filters.regex(r"^noop"))
    async def noop_cb(client: pyrogram.Client, cq: CallbackQuery) -> None:
        await cq.answer("Send another video whenever you're ready!")
