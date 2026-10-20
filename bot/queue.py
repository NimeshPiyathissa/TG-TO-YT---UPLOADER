"""Job queue: lease-based worker pool driving the job state machine (spec section 5.4-5.6, 7).

Download and upload are separate worker pools (separate concurrency knobs).
AWAITING_CHANNEL / AWAITING_CAPTION are pure conversation states advanced by
`bot/handlers/caption.py` directly (not polled here) -- once a handler
transitions a job to READY, the upload worker picks it up on its next poll.
A lease heartbeat keeps a claim alive while work is in flight; an expired
lease left over from a crash (or a systemctl restart mid-upload) is exactly
what lets `yt.upload.start_or_resume_upload`'s offset-probe resume the job
(acceptance criteria #7/#8).
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pyrogram
import structlog
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import flow
from bot.config import settings
from bot.downloader import DownloadTooLarge, InsufficientDisk, cleanup_job_dir, download_job, retry_target
from bot.ui import ProgressReporter, human_bytes, human_elapsed
from db.store import Job, Store
from yt.metadata import probe_video
from yt.oauth import OAuthError
from yt.quota import QuotaExceeded
from yt.tokens import NeedsReauth, get_valid_access_token, refresh_now
from yt.upload import UploadError, start_or_resume_upload
from yt.videos import get_video_status

log = structlog.get_logger(__name__)

LEASE_SECONDS = 60
HEARTBEAT_INTERVAL = 30
POLL_INTERVAL = 2
# Consecutive failed upload attempts before giving up. The count resets as soon as
# an attempt makes progress (and on a manual retry), so a long upload that hiccups
# now and then isn't killed by hiccups that each recovered.
MAX_ATTEMPTS = 8
UNVERIFIED_DURATION_WARN_S = 15 * 60
# A job parked at channel/caption selection this long is abandoned; its file is freed.
AWAITING_EXPIRY_HOURS = 48


def _retry_delay(attempts: int) -> int:
    """30s, 1m, 2m, 4m, 8m, then 15m: long enough to outlast a real outage."""
    return min(900, 30 * 2 ** (attempts - 1))


class JobQueue:
    def __init__(self, bot_client: pyrogram.Client, user_client: Optional[pyrogram.Client], store: Store):
        self.bot = bot_client
        self.user_client = user_client
        self.store = store
        node = os.uname().nodename if hasattr(os, "uname") else "host"
        self.lease_owner = f"{node}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._tasks: dict[int, asyncio.Task] = {}
        self._workers: list[asyncio.Task] = []
        self._stopping = False

    async def start(self) -> None:
        for i in range(settings.max_concurrent_downloads):
            self._workers.append(asyncio.create_task(self._download_worker(i)))
        for i in range(settings.max_concurrent_uploads):
            self._workers.append(asyncio.create_task(self._upload_worker(i)))
        self._workers.append(asyncio.create_task(self._sweeper()))
        log.info("queue.started", lease_owner=self.lease_owner)

    async def stop(self) -> None:
        self._stopping = True
        for t in self._workers:
            t.cancel()
        running = list(self._tasks.values())
        for t in running:
            t.cancel()
        # Wait for the job tasks too, so each one has released its lease before the
        # DB connection is closed.
        await asyncio.gather(*self._workers, *running, return_exceptions=True)

    def cancel_job(self, job_id: int) -> bool:
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
            return True
        return False

    # -- worker loops --------------------------------------------------

    async def _download_worker(self, idx: int) -> None:
        while not self._stopping:
            try:
                job = await self.store.claim_next_job(["QUEUED", "DOWNLOADING"], self.lease_owner, LEASE_SECONDS)
            except asyncio.CancelledError:
                return
            if job is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            task = asyncio.create_task(self._run_download(job.id))
            self._tasks[job.id] = task
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("queue.download_worker_error", job_id=job.id)
            finally:
                self._tasks.pop(job.id, None)

    async def _upload_worker(self, idx: int) -> None:
        while not self._stopping:
            try:
                job = await self.store.claim_next_job(["READY", "UPLOADING"], self.lease_owner, LEASE_SECONDS)
            except asyncio.CancelledError:
                return
            if job is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            task = asyncio.create_task(self._run_upload(job.id))
            self._tasks[job.id] = task
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("queue.upload_worker_error", job_id=job.id)
            finally:
                self._tasks.pop(job.id, None)

    async def _sweeper(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(600)
                await self.sweep_once()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("queue.sweeper_error")

    async def sweep_once(self) -> None:
        """Expires jobs abandoned at channel/caption selection, then deletes job dirs
        for terminal jobs older than JOB_RETENTION_HOURS and any orphaned dir with no
        matching row. rmtree runs in a thread so freeing a multi-GB file can't stall
        the event loop (and with it the lease heartbeats)."""
        now = datetime.now(timezone.utc)
        await self._expire_stale_awaiting(now)
        jobs_root = Path(settings.jobs_dir)
        if not jobs_root.exists():
            return
        for child in jobs_root.iterdir():
            if not child.is_dir() or not child.name.isdigit():
                continue
            job = await self.store.get_job(int(child.name))
            stale = job is None
            if job is not None and job.state in ("DONE", "FAILED", "CANCELLED") and job.finished_at:
                age = now - datetime.fromisoformat(job.finished_at)
                stale = age.total_seconds() > settings.job_retention_hours * 3600
            if stale:
                await asyncio.to_thread(cleanup_job_dir, int(child.name))

    async def _expire_stale_awaiting(self, now: datetime) -> None:
        cutoff = timedelta(hours=AWAITING_EXPIRY_HOURS)
        for job in await self.store.list_active_jobs():
            if job.state not in ("AWAITING_CHANNEL", "AWAITING_CAPTION"):
                continue
            since = datetime.fromisoformat(job.download_finished_at or job.queued_at)
            if now - since < cutoff:
                continue
            try:
                await self.store.transition_job(
                    job.id, "CANCELLED", last_error=f"expired: no reply within {AWAITING_EXPIRY_HOURS}h")
            except Exception:
                log.warning("queue.expire_failed", job_id=job.id, exc_info=True)
                continue
            session = await self.store.get_flow_session(job.tg_user_id)
            if session is not None and session["job_id"] == job.id:
                await self.store.clear_flow_session(job.tg_user_id)
            await asyncio.to_thread(cleanup_job_dir, job.id)
            log.info("queue.job_expired", job_id=job.id, state=job.state)
            try:
                await self.bot.send_message(
                    job.chat_id,
                    f"⌛ Job #{job.id} expired: nothing was chosen within {AWAITING_EXPIRY_HOURS} hours, "
                    "so the downloaded file was removed. Send the video again to start over.",
                )
            except Exception:
                pass

    async def _heartbeat(self, job_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await self.store.heartbeat_lease(job_id, self.lease_owner, LEASE_SECONDS)
        except asyncio.CancelledError:
            return

    # -- download stage: QUEUED -> DOWNLOADING -> AWAITING_CHANNEL/AWAITING_CAPTION --

    async def _run_download(self, job_id: int) -> None:
        hb = asyncio.create_task(self._heartbeat(job_id))
        reporter: Optional[ProgressReporter] = None
        try:
            job = await self.store.get_job(job_id)
            if job is None:
                return
            if job.state == "QUEUED":
                job = await self.store.transition_job(job_id, "DOWNLOADING")

            reporter = ProgressReporter(self.bot, job.chat_id, job.status_message_id)
            reporter.start()
            await download_job(self.bot, self.user_client, self.store, job_id, reporter)
            await reporter.stop()
            reporter = None

            job = await self.store.get_job(job_id)
            probe = await probe_video(job.local_path)
            if not probe["is_video"]:
                await self._fail(job_id, "🚫 That file isn't a video YouTube will accept.")
                return
            await self.store.update_job_fields(
                job_id, duration_s=probe["duration_s"], width=probe["width"],
                height=probe["height"], mime=probe["mime"] or "video/mp4",
            )
            job = await self.store.transition_job(job_id, "AWAITING_CHANNEL")
            await self._post_channel_or_caption_prompt(job)

        except asyncio.CancelledError:
            await self._handle_cancel(job_id)
            raise
        except InsufficientDisk as exc:
            need_gb = exc.need_bytes / 1024**3
            free_gb = exc.free_bytes / 1024**3
            await self._fail(job_id, f"💾 Need {need_gb:.1f} GB free, only {free_gb:.1f} GB available.")
        except DownloadTooLarge as exc:
            max_mb = settings.max_file_bytes / 1024**2
            await self._fail(job_id, f"📦 File is too large (max {max_mb:.0f} MB). {exc}")
        except Exception:
            log.exception("queue.download_failed", job_id=job_id)
            await self._fail(job_id, "Download failed unexpectedly. Try sending it again.")
        finally:
            hb.cancel()
            if reporter:
                await reporter.stop()
            await self.store.release_lease(job_id)

    async def _post_channel_or_caption_prompt(self, job: Job) -> None:
        channels = await self.store.list_yt_channels()
        if len(channels) == 1:
            channel = channels[0]
            await self.store.update_job_fields(job.id, channel_pk=channel["id"])
            job = await self.store.transition_job(job.id, "AWAITING_CAPTION")
            await self.store.save_flow_session(job.tg_user_id, job.id, "awaiting_caption", {})
            text = (
                f"✅ Auto-selected your only linked channel: *{channel['channel_title']}*\n\n"
                + flow.caption_prompt_text(channel["channel_title"])
            )
            kb = flow.caption_prompt_keyboard(job.id, single_channel=True)
        else:
            await self.store.save_flow_session(job.tg_user_id, job.id, "awaiting_channel", {})
            text = flow.channel_picker_text(job.file_name or "video", job.size_bytes or 0,
                                             job.duration_s, job.width, job.height)
            kb = flow.channel_picker_keyboard(channels, job.id)
        try:
            await self.bot.edit_message_text(job.chat_id, job.status_message_id, text,
                                              parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            log.warning("queue.prompt_send_failed", job_id=job.id, exc_info=True)

    # -- upload stage: READY -> UPLOADING -> VERIFYING -> DONE ------------------

    async def _run_upload(self, job_id: int) -> None:
        hb = asyncio.create_task(self._heartbeat(job_id))
        reporter: Optional[ProgressReporter] = None
        delayed = False
        upload_started = time.monotonic()
        try:
            job = await self.store.get_job(job_id)
            if job is None:
                return
            channel = await self.store.get_yt_channel(job.channel_pk) if job.channel_pk else None
            if channel is None:
                await self._fail(job_id, "No YouTube channel is linked to this job anymore.")
                return

            if job.state == "READY":
                job = await self.store.transition_job(job_id, "UPLOADING")

            if job.duration_s and job.duration_s > UNVERIFIED_DURATION_WARN_S:
                # We can't cheaply check verification status via the API, so warn
                # unconditionally per spec 9's "warn before, not after" rule.
                try:
                    await self.bot.send_message(
                        job.chat_id,
                        "⚠️ This video is over 15 minutes. Unverified YouTube channels are capped at "
                        "15-minute uploads -- if yours isn't verified, this upload may fail.",
                    )
                except Exception:
                    pass

            channel_title = channel["channel_title"]
            title = job.title or "Untitled"

            async def _access_token_provider(force: bool) -> str:
                return await (refresh_now(self.store, job.channel_pk) if force
                              else get_valid_access_token(self.store, job.channel_pk))

            reporter = ProgressReporter(self.bot, job.chat_id, job.status_message_id)
            reporter.start()

            progress_seen = False

            async def _on_progress(current: int, total: int) -> None:
                nonlocal progress_seen
                if not progress_seen:
                    progress_seen = True
                    if job.attempts:
                        # Real forward progress: earlier failures were recoverable, so
                        # they shouldn't count against the retry budget any more.
                        await self.store.update_job_fields(job_id, attempts=0)
                elapsed = human_elapsed(time.monotonic() - upload_started)
                reporter.update(
                    "upload", current, total,
                    header_lines=(f"📺 {channel_title}", f"🎬 {title}"),
                    footer_lines=(f"⏱ Elapsed {elapsed}",),
                )

            video_id = await start_or_resume_upload(self.store, job, _access_token_provider, _on_progress)
            await reporter.stop()
            reporter = None

            job = await self.store.transition_job(job_id, "VERIFYING", video_id=video_id)
            access_token = await _access_token_provider(False)
            status = await get_video_status(access_token, video_id)
            actual_privacy = status.get("privacyStatus", job.privacy)

            job = await self.store.transition_job(job_id, "DONE")
            elapsed_s = time.monotonic() - upload_started
            await self.store.create_video(
                video_id, job_id, job.channel_pk, title, actual_privacy, job.tg_user_id,
                job.duration_s, job.size_bytes, elapsed_s,
            )
            await self.store.increment_tg_user_uploads(job.tg_user_id)
            await self.store.touch_channel_used(job.channel_pk)
            await self.store.clear_flow_session(job.tg_user_id)

            await self._send_success(job, channel_title, title, actual_privacy, elapsed_s)
            cleanup_job_dir(job_id)

        except asyncio.CancelledError:
            await self._handle_cancel(job_id)
            raise
        except NeedsReauth as exc:
            await self._needs_reauth(job_id, exc.channel_title)
        except QuotaExceeded as exc:
            job = await self.store.get_job(job_id)
            marker = f"quota:{exc.bucket}"
            if job and job.last_error != marker:
                hours = exc.reset_in_seconds / 3600
                await self.bot.send_message(
                    job.chat_id,
                    f"📊 Daily upload limit reached (100/day). Resets midnight PT — about {hours:.1f}h. "
                    f"Your video stays queued.",
                )
            await self.store.update_job_fields(job_id, last_error=marker)
            await self.store.delay_reclaim(job_id, max(60, exc.reset_in_seconds))
            delayed = True
        except OAuthError as exc:
            await self._retry_or_fail(job_id, str(exc))
        except UploadError as exc:
            await self._retry_or_fail(job_id, str(exc))
        except Exception:
            log.exception("queue.upload_failed", job_id=job_id)
            await self._retry_or_fail(job_id, "Upload failed unexpectedly.")
        finally:
            hb.cancel()
            if reporter:
                await reporter.stop()
            if not delayed:
                await self.store.release_lease(job_id)

    async def _retry_or_fail(self, job_id: int, reason: str) -> None:
        job = await self.store.get_job(job_id)
        if job is None:
            return
        attempts = job.attempts + 1
        title = job.title or "Untitled"
        # UPLOADING can only reach FAILED via VERIFYING/FAILED/CANCELLED per the
        # state machine, so always land on FAILED first, then hop back to READY
        # for an automatic retry -- FAILED -> READY is an explicitly allowed edge.
        if job.state not in ("FAILED", "CANCELLED", "DONE"):
            await self.store.transition_job(job_id, "FAILED", attempts=attempts, last_error=reason[:2000])
        else:
            await self.store.update_job_fields(job_id, attempts=attempts, last_error=reason[:2000])

        if attempts < MAX_ATTEMPTS:
            delay = _retry_delay(attempts)
            try:
                await self.bot.send_message(
                    job.chat_id,
                    f"❌ *Upload failed*\n🎬 {title}\n⚠️ {reason}\n🔁 Attempt {attempts}/{MAX_ATTEMPTS} — retrying in {delay}s",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            await self.store.transition_job(job_id, "READY")
            await self.store.delay_reclaim(job_id, delay)
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔁 Retry", callback_data=f"retryjob:{job_id}"),
                InlineKeyboardButton("🗑 Discard", callback_data=f"discardjob:{job_id}"),
            ]])
            try:
                await self.bot.send_message(
                    job.chat_id,
                    f"❌ *Upload failed*\n🎬 {title}\n⚠️ {reason}\n\nGave up after {MAX_ATTEMPTS} attempts.",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
                )
            except Exception:
                pass

    async def _send_success(self, job: Job, channel_title: str, title: str, actual_privacy: str, elapsed_s: float) -> None:
        privacy_line = actual_privacy.capitalize()
        if not settings.project_audited and actual_privacy == "private" and job.privacy != "private":
            privacy_line += " ⚠️ _(locked — project not audited)_"
        dur = job.duration_s or 0
        dur_str = f"{int(dur // 60)}:{int(dur % 60):02d}"
        avg = (job.size_bytes or 0) / elapsed_s / 1024 / 1024 if elapsed_s > 0 else 0
        text = (
            f"✅ *Upload complete!*\n\n"
            f"🎬 {title}\n"
            f"📺 {channel_title}\n"
            f"🔗 https://youtu.be/{job.video_id}\n\n"
            f"👁 Status: {privacy_line}\n"
            f"📦 {human_bytes(job.size_bytes or 0)} · ⏱ {dur_str}\n"
            f"⌛ Took *{human_elapsed(elapsed_s)}* (avg {avg:.1f} MB/s)"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Open on YouTube", url=f"https://youtu.be/{job.video_id}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"deletevideo:{job.video_id}"),
        ], [InlineKeyboardButton("⬆️ Upload another", callback_data="noop")]])
        try:
            await self.bot.edit_message_text(job.chat_id, job.status_message_id, text,
                                              parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            try:
                await self.bot.send_message(job.chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            except Exception:
                log.warning("queue.success_notify_failed", job_id=job.id, exc_info=True)

    # -- shared failure/cancel handling -----------------------------------

    async def _fail(self, job_id: int, user_message: str) -> None:
        job = await self.store.get_job(job_id)
        if job is None:
            return
        await self.store.fail_job(job_id, user_message)
        try:
            await self.bot.send_message(job.chat_id, user_message)
        except Exception:
            log.warning("queue.fail_notify_failed", job_id=job_id, exc_info=True)
        await self.store.clear_flow_session(job.tg_user_id)
        cleanup_job_dir(job_id)

    async def _needs_reauth(self, job_id: int, channel_title: str) -> None:
        job = await self.store.get_job(job_id)
        if job is None:
            return
        await self.store.fail_job(job_id, f"needs_reauth:{channel_title}")
        web_url = (f"https://{settings.public_host}:{settings.web_port}/channels" if settings.oauth_mode == "hostname"
                   else f"https://{settings.public_ip}:{settings.web_port}/channels")
        try:
            await self.bot.send_message(
                job.chat_id,
                f"🔑 *{channel_title}* needs to be re-authorised.\nOpen {web_url} to relink it, then tap Retry.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            log.warning("queue.reauth_notify_failed", job_id=job_id, exc_info=True)

    async def _handle_cancel(self, job_id: int) -> None:
        job = await self.store.get_job(job_id)
        if job is None:
            return
        if self._stopping:
            # Service shutdown/restart, not a user cancel: leave the job and its file
            # alone. _run_* releases the lease on the way out, so the next start
            # reclaims it -- an upload resumes from Google's confirmed offset and a
            # download starts over -- instead of the work being thrown away.
            log.info("queue.job_left_for_resume", job_id=job_id, state=job.state)
            return
        if job.state not in ("DONE", "FAILED", "CANCELLED"):
            try:
                await self.store.transition_job(job_id, "CANCELLED")
            except Exception:
                pass
        await self.store.clear_flow_session(job.tg_user_id)
        cleanup_job_dir(job_id)
        try:
            await self.bot.send_message(job.chat_id, f"Cancelled job #{job_id}.")
        except Exception:
            pass

    async def retry_job(self, job_id: int) -> bool:
        job = await self.store.get_job(job_id)
        if job is None or job.state != "FAILED":
            return False
        await self.store.transition_job(job_id, retry_target(job), attempts=0)
        return True
