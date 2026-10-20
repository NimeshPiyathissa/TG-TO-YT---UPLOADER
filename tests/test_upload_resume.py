"""Resumable upload against a fake Google endpoint: interruption, restart, dead
session, and 4 GiB+ offsets. Chunks are 1 MiB (4 x 256 KiB, as Google requires)."""
import asyncio
import os

import pytest
import pytest_asyncio

from bot.config import settings
from tests.fake_google import FakeGoogle
from yt import upload
from yt.upload import UploadError, start_or_resume_upload

CHUNK = 1024 * 1024


async def _token(force: bool) -> str:
    return "tok"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def no_sleep(attempt):
        return None
    monkeypatch.setattr(upload, "_backoff_sleep", no_sleep)
    monkeypatch.setattr(settings, "upload_chunk_bytes", CHUNK)


@pytest_asyncio.fixture
async def google(monkeypatch):
    g = FakeGoogle()
    monkeypatch.setattr(upload, "UPLOAD_INIT_URL", await g.start())
    yield g
    await g.stop()


async def _make_job(store, tmp_path, size=int(5.5 * CHUNK)):
    payload = os.urandom(size)
    path = tmp_path / "source.media"
    path.write_bytes(payload)
    job_id = await store.create_job(1, 1, source_chat_id=1, source_message_id=1,
                                    file_name="v.mp4", size_bytes=size, mime="video/mp4")
    await store.update_job_fields(job_id, local_path=str(path), title="t")
    return job_id, payload


async def test_clean_upload(store, tmp_path, google):
    job_id, payload = await _make_job(store, tmp_path)
    video_id = await start_or_resume_upload(store, await store.get_job(job_id), _token)
    assert video_id == "vid123"
    assert bytes(google.data) == payload
    assert google.inits == 1
    assert google.max_body <= CHUNK            # never more than one chunk in flight/in memory


@pytest.mark.parametrize("fault", ["503", "drop"])
async def test_midchunk_failure_resumes_without_restarting(store, tmp_path, google, fault):
    job_id, payload = await _make_job(store, tmp_path)
    google.fail_on_put = {3: fault}            # half of chunk 3 lands, then the request dies
    video_id = await start_or_resume_upload(store, await store.get_job(job_id), _token)
    assert video_id == "vid123"
    assert bytes(google.data) == payload       # no gap and no duplicated bytes
    assert google.inits == 1                   # same session
    assert google.chunk_starts.count(0) == 1   # never went back to zero
    if fault == "503":
        # a 5xx reaches us, so we must ask Google for its offset instead of guessing;
        # on a dropped connection aiohttp may retry the PUT itself and Google's 308
        # answer realigns it, so the end state above is the contract there.
        assert google.probes >= 1


async def test_process_restart_resumes_from_confirmed_offset(store, tmp_path, google):
    job_id, payload = await _make_job(store, tmp_path)
    reached = asyncio.Event()

    async def stall_after_third_chunk(n):
        if n == 3:
            reached.set()
            await asyncio.sleep(30)            # response never arrives: process "dies" here

    google.after_commit = stall_after_third_chunk
    task = asyncio.create_task(start_or_resume_upload(store, await store.get_job(job_id), _token))
    await asyncio.wait_for(reached.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stale = await store.get_job(job_id)        # what a fresh process would load from the DB
    assert stale.upload_session_uri
    assert stale.bytes_sent < google.committed # DB lags Google by the in-flight chunk

    google.after_commit = None
    video_id = await start_or_resume_upload(store, stale, _token)
    assert video_id == "vid123"
    assert bytes(google.data) == payload
    assert google.inits == 1
    assert google.chunk_starts.count(0) == 1


async def test_dead_session_restarts_from_zero(store, tmp_path, google):
    job_id, payload = await _make_job(store, tmp_path)

    async def expire_after_two(n):
        if n == 2:
            google.killed.add(google.inits)
    google.after_commit = expire_after_two
    video_id = await start_or_resume_upload(store, await store.get_job(job_id), _token)
    assert video_id == "vid123"
    assert bytes(google.data) == payload
    assert google.inits == 2                   # a fresh session, uploaded in full


async def test_session_google_keeps_rejecting_does_not_burn_the_daily_quota(store, tmp_path, google):
    job_id, _ = await _make_job(store, tmp_path)
    google.dead = True
    with pytest.raises(UploadError):
        await start_or_resume_upload(store, await store.get_job(job_id), _token)
    assert google.inits <= 1 + upload._MAX_SESSION_RESTARTS
    assert await store.quota_used("uploads") == google.inits


async def test_finished_upload_found_on_resume(store, tmp_path, google):
    """Last chunk reached Google but the process died before reading the answer."""
    job_id, payload = await _make_job(store, tmp_path)
    google.inits = 1
    google.data, google.committed, google.complete = bytearray(payload), len(payload), True
    await store.update_job_fields(job_id, upload_session_uri=f"{google.base}/session/1",
                                  bytes_sent=len(payload) - CHUNK)
    video_id = await start_or_resume_upload(store, await store.get_job(job_id), _token)
    assert video_id == "vid123"
    assert google.inits == 1 and google.data_puts == 0


async def test_server_that_never_acknowledges_bytes_fails_instead_of_spinning(store, tmp_path, google):
    job_id, _ = await _make_job(store, tmp_path)
    google.always_308_no_range = True
    with pytest.raises(UploadError):
        await asyncio.wait_for(start_or_resume_upload(store, await store.get_job(job_id), _token), 20)


@pytest.mark.slow
async def test_offsets_past_4gib(store, monkeypatch):
    """Content-Range arithmetic and chunk sizing for a 4.2 GB file, without moving
    4 GB of real data: the file object is faked and the server only counts bytes."""
    total = 4_400_000_000
    chunk = 64 * 1024 * 1024
    monkeypatch.setattr(settings, "upload_chunk_bytes", chunk)
    g = FakeGoogle(keep_data=False)
    monkeypatch.setattr(upload, "UPLOAD_INIT_URL", await g.start())

    class FakeFile:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def seek(self, pos):
            self.pos = pos

        async def read(self, n):
            return b"\0" * n

    monkeypatch.setattr(upload.aiofiles, "open", lambda *a, **k: FakeFile())
    try:
        job_id = await store.create_job(1, 1, source_chat_id=1, source_message_id=1,
                                        file_name="big.mp4", size_bytes=total, mime="video/mp4")
        await store.update_job_fields(job_id, local_path="ignored", title="t")
        assert await start_or_resume_upload(store, await store.get_job(job_id), _token) == "vid123"
        assert g.committed == total
        assert max(g.chunk_starts) > 2 ** 32       # offsets beyond 32 bits are sent correctly
        assert g.max_body <= chunk
    finally:
        await g.stop()
