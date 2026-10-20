"""Job queue behaviour that matters for multi-GB files: restarts must not throw
work away, retries must not run out mid-transfer, and abandoned or finished jobs
must give their disk back."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bot import queue as queue_mod
from bot.config import settings
from bot.downloader import retry_target
from bot.queue import AWAITING_EXPIRY_HOURS, JobQueue, _retry_delay
from yt.upload import UploadError

MIB = 1024 * 1024


def hours_ago(h: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    d = tmp_path / "jobs"
    d.mkdir()
    monkeypatch.setattr(settings, "jobs_dir", str(d))
    return d


@pytest.fixture
def queue(store):
    return JobQueue(AsyncMock(), None, store)


async def mk_job(store, jobs_dir, *, state, user=1, size=64 * MIB, with_file=True, with_channel=True, **fields):
    job_id = await store.create_job(user, user, source_chat_id=user, source_message_id=1,
                                    file_name="v.mp4", size_bytes=size, mime="video/mp4")
    path = jobs_dir / str(job_id) / "source.media"
    if with_file:
        path.parent.mkdir()
        with open(path, "wb") as f:
            f.truncate(size)
    updates = dict(state=state, title="t", local_path=str(path), **fields)
    if with_channel:
        await store.conn.execute(
            "INSERT OR IGNORE INTO yt_channels (id, channel_id, channel_title, refresh_token_enc, scopes, added_at) "
            "VALUES (1, 'UC1', 'Chan', x'00', 'scope', ?)", (hours_ago(0),))
        await store.conn.commit()
        updates["channel_pk"] = 1
    await store.update_job_fields(job_id, **updates)
    return job_id, path


# -- restart safety --------------------------------------------------------------

async def test_shutdown_mid_upload_keeps_job_and_file_for_resume(store, queue, jobs_dir, monkeypatch):
    job_id, path = await mk_job(store, jobs_dir, state="READY", bytes_sent=10 * MIB,
                                upload_session_uri="https://example/session/1")
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(queue_mod, "start_or_resume_upload", blocked)
    await store.claim_next_job(["READY", "UPLOADING"], queue.lease_owner)
    queue._tasks[job_id] = asyncio.create_task(queue._run_upload(job_id))
    await asyncio.wait_for(started.wait(), 5)

    await queue.stop()                                   # what SIGTERM / systemctl restart does

    job = await store.get_job(job_id)
    assert job.state == "UPLOADING"                      # not CANCELLED
    assert path.exists()                                 # file kept
    assert (job.upload_session_uri, job.bytes_sent) == ("https://example/session/1", 10 * MIB)
    assert job.lease_expires_at is None                  # immediately reclaimable
    reclaimed = await store.claim_next_job(["READY", "UPLOADING"], "next-process")
    assert reclaimed is not None and reclaimed.id == job_id
    queue.bot.send_message.assert_not_called()           # no "Cancelled job" message


async def test_shutdown_mid_download_requeues_instead_of_cancelling(store, queue, jobs_dir, monkeypatch):
    job_id, _ = await mk_job(store, jobs_dir, state="QUEUED", with_file=False, with_channel=False)
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(queue_mod, "download_job", blocked)
    await store.claim_next_job(["QUEUED", "DOWNLOADING"], queue.lease_owner)
    queue._tasks[job_id] = asyncio.create_task(queue._run_download(job_id))
    await asyncio.wait_for(started.wait(), 5)
    await queue.stop()
    job = await store.get_job(job_id)
    assert job.state == "DOWNLOADING" and job.lease_expires_at is None
    assert (await store.claim_next_job(["QUEUED", "DOWNLOADING"], "next-process")).id == job_id


async def test_user_cancel_still_cancels_and_frees_the_file(store, queue, jobs_dir, monkeypatch):
    job_id, path = await mk_job(store, jobs_dir, state="READY")
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(queue_mod, "start_or_resume_upload", blocked)
    await store.claim_next_job(["READY", "UPLOADING"], queue.lease_owner)
    task = asyncio.create_task(queue._run_upload(job_id))
    queue._tasks[job_id] = task
    await asyncio.wait_for(started.wait(), 5)
    assert queue.cancel_job(job_id)
    await asyncio.gather(task, return_exceptions=True)
    assert (await store.get_job(job_id)).state == "CANCELLED"
    assert not path.exists()


# -- retry budget ----------------------------------------------------------------

async def test_attempts_reset_once_an_attempt_makes_progress(store, queue, jobs_dir, monkeypatch):
    job_id, _ = await mk_job(store, jobs_dir, state="READY", attempts=6)

    async def moves_then_drops(store_, job, provider, on_progress):
        await on_progress(8 * MIB, 64 * MIB)
        raise UploadError("network down")

    monkeypatch.setattr(queue_mod, "start_or_resume_upload", moves_then_drops)
    await queue._run_upload(job_id)
    job = await store.get_job(job_id)
    assert job.attempts == 1                             # 6 earlier failures forgiven
    assert job.state == "READY"                          # retry scheduled, not given up


async def test_attempts_accumulate_when_nothing_moves(store, queue, jobs_dir, monkeypatch):
    job_id, _ = await mk_job(store, jobs_dir, state="READY", attempts=6)

    async def fails_at_once(store_, job, provider, on_progress):
        raise UploadError("network down")

    monkeypatch.setattr(queue_mod, "start_or_resume_upload", fails_at_once)
    await queue._run_upload(job_id)
    assert (await store.get_job(job_id)).attempts == 7


def test_retry_backoff_outlasts_a_real_outage():
    delays = [_retry_delay(n) for n in range(1, queue_mod.MAX_ATTEMPTS)]
    assert delays[0] == 30 and delays == sorted(delays) and max(delays) == 900
    assert sum(delays) > 30 * 60


async def test_manual_retry_resets_attempts_and_redownloads_when_the_file_was_swept(store, queue, jobs_dir):
    gone, _ = await mk_job(store, jobs_dir, state="FAILED", attempts=8, with_file=False)
    kept, _ = await mk_job(store, jobs_dir, state="FAILED", attempts=8)
    assert retry_target(await store.get_job(gone)) == "QUEUED"
    assert retry_target(await store.get_job(kept)) == "READY"
    assert await queue.retry_job(gone) and await queue.retry_job(kept)
    gone_job, kept_job = await store.get_job(gone), await store.get_job(kept)
    assert (gone_job.state, gone_job.attempts) == ("QUEUED", 0)
    assert (kept_job.state, kept_job.attempts) == ("READY", 0)


# -- sweeper ---------------------------------------------------------------------

async def test_sweeper_frees_large_files_and_spares_live_jobs(store, queue, jobs_dir):
    expired, expired_path = await mk_job(store, jobs_dir, state="AWAITING_CHANNEL", user=1,
                                         download_finished_at=hours_ago(AWAITING_EXPIRY_HOURS + 1))
    waiting, waiting_path = await mk_job(store, jobs_dir, state="AWAITING_CAPTION", user=2,
                                         download_finished_at=hours_ago(AWAITING_EXPIRY_HOURS - 1))
    uploading, uploading_path = await mk_job(store, jobs_dir, state="UPLOADING", user=3)
    old_done, old_done_path = await mk_job(store, jobs_dir, state="DONE", user=4,
                                           finished_at=hours_ago(settings.job_retention_hours + 1))
    new_done, new_done_path = await mk_job(store, jobs_dir, state="DONE", user=5, finished_at=hours_ago(1))
    orphan = jobs_dir / "9999"
    orphan.mkdir()
    (orphan / "source.media").write_bytes(b"x")
    # user 1's flow belongs to the expired job; user 2's to a live one
    await store.save_flow_session(1, expired, "awaiting_channel", {})
    await store.save_flow_session(2, waiting, "awaiting_caption", {})

    await queue.sweep_once()

    assert (await store.get_job(expired)).state == "CANCELLED"
    assert (await store.get_job(expired)).last_error.startswith("expired")
    assert not expired_path.exists() and not old_done_path.exists() and not orphan.exists()
    assert waiting_path.exists() and uploading_path.exists() and new_done_path.exists()
    assert (await store.get_job(waiting)).state == "AWAITING_CAPTION"
    assert (await store.get_job(uploading)).state == "UPLOADING"
    assert await store.get_flow_session(1) is None
    assert await store.get_flow_session(2) is not None
    queue.bot.send_message.assert_awaited_once()


async def test_sweeper_does_not_clear_a_newer_flow_of_the_same_user(store, queue, jobs_dir):
    old, _ = await mk_job(store, jobs_dir, state="AWAITING_CHANNEL", user=1,
                          download_finished_at=hours_ago(AWAITING_EXPIRY_HOURS + 5))
    newer, _ = await mk_job(store, jobs_dir, state="AWAITING_CAPTION", user=1, download_finished_at=hours_ago(1))
    await store.save_flow_session(1, newer, "awaiting_caption", {})
    await queue.sweep_once()
    assert (await store.get_job(old)).state == "CANCELLED"
    assert (await store.get_flow_session(1))["job_id"] == newer
