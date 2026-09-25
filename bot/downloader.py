"""Telegram -> disk download pipeline.

Hybrid shape: the bot client owns the UI and small (<=20MB) downloads; the
MTProto user-session client owns the bytes for anything bigger, via a relay
channel both accounts are members of (RELAY_CHAT_ID) -- see README "Generating
the string session". Files always land under JOBS_DIR, never /tmp (tmpfs would
OOM a small VM on a multi-GB download).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import pyrogram
import structlog

from bot.config import settings
from bot.ui import ProgressReporter
from db.store import Store

log = structlog.get_logger(__name__)

TWENTY_MB = 20 * 1024 * 1024


class DownloadTooLarge(Exception):
    pass


class InsufficientDisk(Exception):
    def __init__(self, need_bytes: int, free_bytes: int):
        self.need_bytes = need_bytes
        self.free_bytes = free_bytes
        super().__init__(f"need {need_bytes} bytes, have {free_bytes} free")


def check_disk_space(size_bytes: int, jobs_dir: Optional[str] = None) -> None:
    jobs_dir = jobs_dir or settings.jobs_dir
    Path(jobs_dir).mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(jobs_dir).free
    need = int(size_bytes * 1.2)
    if free < need:
        raise InsufficientDisk(need, free)


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

    check_disk_space(size)
    dest_dir = job_dir(job_id)
    dest_path = dest_dir / "source.media"

    def _progress_cb(current: int, total: int) -> None:
        progress.update("download", current, total or size)

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


def cleanup_job_dir(job_id: int) -> None:
    d = Path(settings.jobs_dir) / str(job_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
