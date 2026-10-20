"""Telegram -> disk download pipeline.

Hybrid shape: the bot client owns the UI and small (<=20MB) downloads; the
MTProto user-session client owns the bytes for anything bigger, via a relay
channel both accounts are members of (RELAY_CHAT_ID) -- see README "Generating
the string session". Files always land under JOBS_DIR, never /tmp (tmpfs would
OOM a small VM on a multi-GB download).
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Optional

import pyrogram
import structlog

from bot import progress_hub
from bot.config import settings
from bot.ui import ProgressReporter
from db.store import Store

log = structlog.get_logger(__name__)

TWENTY_MB = 20 * 1024 * 1024
DISK_WAIT_POLL_S = 30
DISK_WAIT_MAX_S = 6 * 3600

# job_id -> (expected size, destination path) for downloads running right now.
# check_disk_space subtracts what these still have to write, otherwise two
# downloads starting together would each see the same free space and both pass.
_inflight: dict[int, tuple[int, Path]] = {}


class DownloadTooLarge(Exception):
    pass


class InsufficientDisk(Exception):
    def __init__(self, need_bytes: int, free_bytes: int):
        self.need_bytes = need_bytes
        self.free_bytes = free_bytes
        super().__init__(f"need {need_bytes} bytes, have {free_bytes} free")


def _inflight_remaining(exclude_job_id: Optional[int] = None) -> int:
    total = 0
    for jid, (size, dest) in _inflight.items():
        if jid == exclude_job_id:
            continue
        written = 0
        for candidate in (Path(f"{dest}.temp"), dest):
            try:
                written = max(written, candidate.stat().st_size)
            except OSError:
                pass
        total += max(0, size - written)
    return total


def check_disk_space(size_bytes: int, jobs_dir: Optional[str] = None, *,
                     committed_bytes: int = 0, exclude_job_id: Optional[int] = None) -> None:
    """Raises InsufficientDisk unless ``size_bytes`` x1.2 plus the MIN_FREE_DISK_BYTES
    floor fits in what's free after setting aside ``committed_bytes`` (jobs accepted
    but not started) and what in-flight downloads still have to write."""
    jobs_dir = jobs_dir or settings.jobs_dir
    Path(jobs_dir).mkdir(parents=True, exist_ok=True)
    available = shutil.disk_usage(jobs_dir).free - committed_bytes - _inflight_remaining(exclude_job_id)
    need = int(size_bytes * 1.2) + settings.min_free_disk_bytes
    if available < need:
        raise InsufficientDisk(need, max(0, available))


def _other_job_dirs_exist(job_id: int) -> bool:
    """True if some other job's files are on disk, i.e. space may free up on its own."""
    root = Path(settings.jobs_dir)
    try:
        return any(c.is_dir() and c.name.isdigit() and int(c.name) != job_id for c in root.iterdir())
    except OSError:
        return False


async def wait_for_disk_space(bot_client: pyrogram.Client, job, size: int) -> None:
    """Holds a download until it fits instead of failing it, as long as other jobs
    are using the disk and might finish. Gives up after DISK_WAIT_MAX_S."""
    waited = 0
    notified = False
    while True:
        try:
            check_disk_space(size, exclude_job_id=job.id)
            return
        except InsufficientDisk:
            if waited >= DISK_WAIT_MAX_S or not _other_job_dirs_exist(job.id):
                raise
        if not notified and job.status_message_id:
            notified = True
            try:
                await bot_client.edit_message_text(
                    job.chat_id, job.status_message_id,
                    "💾 Waiting for disk space to free up -- other videos are still being "
                    "processed. This will start automatically.",
                )
            except Exception:
                log.warning("downloader.disk_wait_notify_failed", job_id=job.id, exc_info=True)
        log.info("downloader.waiting_for_disk", job_id=job.id, waited_s=waited)
        await asyncio.sleep(DISK_WAIT_POLL_S)
        waited += DISK_WAIT_POLL_S


def job_dir(job_id: int) -> Path:
    d = Path(settings.jobs_dir) / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def can_download_without_relay(size_bytes: int) -> bool:
    return size_bytes <= TWENTY_MB


async def download_job(
    bot_client: pyrogram.Client,
    user_client: Optional[pyrogram.Client],
    store: Store,
    job_id: int,
    progress: ProgressReporter,
) -> Path:
    """Downloads the job's source media to <jobs_dir>/<job_id>/source.media and
    persists local_path on the job row. Raises on failure; the caller (queue.py)
    is responsible for transitioning the job to FAILED with a clean message."""
    job = await store.get_job(job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")

    size = job.size_bytes or 0
    if size > settings.max_file_bytes:
        raise DownloadTooLarge(f"{size} exceeds MAX_FILE_BYTES={settings.max_file_bytes}")

    await wait_for_disk_space(bot_client, job, size)
    dest_dir = job_dir(job_id)
    dest_path = dest_dir / "source.media"
    _inflight[job_id] = (size, dest_path)  # no await since the check above: nothing can slip in between

    try:
        def _progress_cb(current: int, total: int) -> None:
            progress.update("download", current, total or size)
            progress_hub.record(job_id, "download", current, total or size)

        if can_download_without_relay(size):
            message = await bot_client.get_messages(job.source_chat_id, job.source_message_id)
            await bot_client.download_media(message, file_name=str(dest_path), progress=_progress_cb)
        elif user_client is not None and settings.relay_chat_id:
            relay_msg = await bot_client.forward_messages(
                settings.relay_chat_id, from_chat_id=job.source_chat_id, message_ids=job.source_message_id
            )
            relay_id = relay_msg[0].id if isinstance(relay_msg, list) else relay_msg.id
            try:
                um = await user_client.get_messages(settings.relay_chat_id, relay_id)
                await user_client.download_media(um, file_name=str(dest_path), progress=_progress_cb)
            finally:
                try:
                    await bot_client.delete_messages(settings.relay_chat_id, relay_id)
                except Exception:
                    log.warning("downloader.relay_cleanup_failed", job_id=job_id, exc_info=True)
        else:
            raise DownloadTooLarge(
                f"{size} bytes exceeds the 20MB Bot API limit and no USER_SESSION/RELAY_CHAT_ID relay is configured"
            )

        if not dest_path.exists():
            raise RuntimeError("download completed but file missing on disk")

        actual_size = dest_path.stat().st_size
        if job.size_bytes and abs(actual_size - job.size_bytes) > 4096:
            log.warning("downloader.size_mismatch", job_id=job_id, expected=job.size_bytes, actual=actual_size)

        await store.update_job_fields(job_id, local_path=str(dest_path), size_bytes=actual_size)
        return dest_path
    finally:
        _inflight.pop(job_id, None)


def retry_target(job) -> str:
    """Where a FAILED job re-enters the pipeline: straight to the upload if its file
    and channel are still there, otherwise QUEUED so the video is downloaded again
    from the original Telegram message (the sweeper deletes files after retention)."""
    if job.local_path and job.channel_pk and Path(job.local_path).is_file():
        return "READY"
    return "QUEUED"


def cleanup_job_dir(job_id: int) -> None:
    d = Path(settings.jobs_dir) / str(job_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
