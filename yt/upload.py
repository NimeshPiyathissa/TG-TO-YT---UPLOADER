"""Hand-rolled resumable upload against the raw YouTube Data API v3 endpoint (spec section 7).

``google-api-python-client``'s ``MediaFileUpload`` is synchronous and would block
the event loop for the whole upload, so this talks to the resumable-upload
protocol directly with ``aiohttp`` + ``aiofiles``, streaming from disk in
``UPLOAD_CHUNK_BYTES``-sized pieces and never holding the whole file in memory.

Callers are responsible for job *state* transitions (READY -> UPLOADING ->
VERIFYING); this module only persists ``upload_session_uri`` / ``bytes_sent`` /
``video_id`` onto the job row as bookkeeping so a restart can resume.
"""
from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional

import aiofiles
import aiohttp
import structlog

from bot.config import settings
from db.store import Job, Store
from yt import quota

log = structlog.get_logger(__name__)

UPLOAD_INIT_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
_TRANSIENT_STATUSES = {500, 502, 503, 504}
_MAX_ATTEMPTS = 5

AccessTokenProvider = Callable[[bool], Awaitable[str]]
ProgressCallback = Callable[[int, int], Awaitable[None]]


class UploadError(Exception):
    pass


def _session_uri_prefix(uri: Optional[str]) -> str:
    if not uri:
        return ""
    return uri[:48] + "…redacted"


def _video_body(job: Job) -> dict:
    return {
        "snippet": {
            "title": (job.title or "Untitled")[:100],
            "description": (job.description or "")[:5000],
            "categoryId": job.category_id or "22",
        },
        "status": {
            "privacyStatus": job.privacy,
            "selfDeclaredMadeForKids": bool(job.made_for_kids),
        },
    }


async def _backoff_sleep(attempt: int) -> None:
    delay = min(32, 2 ** attempt) + random.uniform(0, 1)
    await asyncio.sleep(delay)


async def _init_session(store: Store, job: Job, access_token: str) -> str:
    await quota.check_and_spend(store, "uploads", 1)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(job.size_bytes),
        "X-Upload-Content-Type": job.mime or "video/mp4",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(UPLOAD_INIT_URL, headers=headers, json=_video_body(job)) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise UploadError(f"Failed to start upload session (HTTP {resp.status}): {body[:300]}")
            location = resp.headers.get("Location")
            if not location:
                raise UploadError("Google didn't return a resumable session URI.")
    await store.update_job_fields(job.id, upload_session_uri=location, bytes_sent=0)
    log.info("upload.session_started", job_id=job.id, session=_session_uri_prefix(location))
    return location


async def _probe_offset(session_uri: str, total: int) -> Optional[int]:
    """PUT with an empty body and Content-Range: bytes */<total> to ask Google
    what it already has. Returns the confirmed byte count, or None if the
    session is dead (404/410) and must be restarted from zero."""
    headers = {"Content-Range": f"bytes */{total}", "Content-Length": "0"}
    async with aiohttp.ClientSession() as session:
        async with session.put(session_uri, headers=headers) as resp:
            if resp.status in (404, 410):
                return None
            if resp.status in (200, 201):
                return total
            if resp.status == 308:
                rng = resp.headers.get("Range")
                if rng and "-" in rng:
                    return int(rng.split("-")[1]) + 1
                return 0
            raise UploadError(f"Unexpected status probing upload offset: HTTP {resp.status}")


async def _put_chunk(session_uri: str, access_token: str, local_path: str, start: int, total: int,
                      chunk_bytes: int) -> tuple[Optional[int], Optional[dict]]:
    """Returns (confirmed_offset, final_video_resource). Exactly one is non-None
    on success; raises UploadError (caller decides retry) on failure."""
    end = min(start + chunk_bytes, total) - 1
    length = end - start + 1
    async with aiofiles.open(local_path, "rb") as f:
        await f.seek(start)
        data = await f.read(length)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{total}",
    }
    async with aiohttp.ClientSession() as session:
        async with session.put(session_uri, headers=headers, data=data) as resp:
            if resp.status == 308:
                rng = resp.headers.get("Range")
                if rng and "-" in rng:
                    return int(rng.split("-")[1]) + 1, None
                return start, None  # no bytes acknowledged yet; caller retries same window
            if resp.status in (200, 201):
                return None, await resp.json()
            if resp.status == 401:
                raise _Http401()
            if resp.status == 404:
                raise _Http404()
            if resp.status in _TRANSIENT_STATUSES:
                raise _HttpTransient(resp.status)
            body = await resp.text()
            raise UploadError(f"Upload rejected (HTTP {resp.status}): {body[:300]}")


class _Http401(Exception):
    pass


class _Http404(Exception):
    pass


class _HttpTransient(Exception):
    def __init__(self, status: int):
        super().__init__(str(status))
        self.status = status


async def start_or_resume_upload(
    store: Store, job: Job, access_token_provider: AccessTokenProvider,
    on_progress: Optional[ProgressCallback] = None,
) -> str:
    """Drives ``job.local_path`` through init -> chunked PUT -> completion.
    Returns the resulting ``video_id``. Persists progress to the job row as it
    goes so a process restart can resume from the confirmed server-side offset."""
    if not job.local_path or not job.size_bytes:
        raise UploadError("Job has no local file to upload.")

    total = job.size_bytes
    access_token = await access_token_provider(False)

    session_uri = job.upload_session_uri
    start = job.bytes_sent or 0
    if session_uri:
        probed = await _probe_offset(session_uri, total)
        if probed is None:
            session_uri = None  # dead session -- restart from zero, costs one more 'uploads' unit
        else:
            start = probed
            await store.update_job_fields(job.id, bytes_sent=start)

    if session_uri is None:
        session_uri = await _init_session(store, job, access_token)
        start = 0

    chunk_bytes = settings.upload_chunk_bytes
    attempt = 0
    while start < total:
        try:
            confirmed, final = await _put_chunk(session_uri, access_token, job.local_path, start, total, chunk_bytes)
        except _Http401:
            access_token = await access_token_provider(True)
            attempt += 1
            if attempt >= _MAX_ATTEMPTS:
                raise UploadError("Upload failed after refreshing credentials -- please retry.")
            continue
        except _Http404:
            session_uri = await _init_session(store, job, access_token)
            start = 0
            attempt = 0
            continue
        except (_HttpTransient, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            attempt += 1
            if attempt >= _MAX_ATTEMPTS:
                raise UploadError(f"Upload failed after {_MAX_ATTEMPTS} attempts: {exc}")
            await _backoff_sleep(attempt)
            continue

        attempt = 0
        if final is not None:
            video_id = final.get("id")
            if not video_id:
                raise UploadError("Upload completed but Google didn't return a video id.")
            await store.update_job_fields(job.id, bytes_sent=total, video_id=video_id)
            log.info("upload.complete", job_id=job.id, video_id=video_id)
            return video_id

        start = confirmed if confirmed is not None else start
        await store.update_job_fields(job.id, bytes_sent=start)
        if on_progress:
            await on_progress(start, total)

    raise UploadError("Upload loop exited without a video id -- this should not happen.")
