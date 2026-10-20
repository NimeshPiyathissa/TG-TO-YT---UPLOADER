import asyncio
from collections import namedtuple
from pathlib import Path

import pytest

from bot import downloader
from bot.config import settings
from bot.downloader import InsufficientDisk, check_disk_space, wait_for_disk_space

GIB = 1024 ** 3
Usage = namedtuple("Usage", "total used free")


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "jobs_dir", str(tmp_path / "jobs"))
    monkeypatch.setattr(settings, "min_free_disk_bytes", 5 * GIB)
    downloader._inflight.clear()
    yield
    downloader._inflight.clear()


def set_free(monkeypatch, gib):
    box = {"free": gib * GIB}
    monkeypatch.setattr(downloader.shutil, "disk_usage", lambda p: Usage(0, 0, int(box["free"])))
    return box


def test_floor_and_headroom_apply_to_a_4gb_file(monkeypatch):
    set_free(monkeypatch, 9)                  # 4 * 1.2 + 5 = 9.8 GiB needed
    with pytest.raises(InsufficientDisk):
        check_disk_space(4 * GIB)
    set_free(monkeypatch, 10)
    check_disk_space(4 * GIB)


def test_running_downloads_reserve_what_they_have_left_to_write(monkeypatch, tmp_path):
    set_free(monkeypatch, 12)
    check_disk_space(4 * GIB)
    dest = tmp_path / "jobs" / "1" / "source.media"
    dest.parent.mkdir(parents=True)
    downloader._inflight[1] = (4 * GIB, dest)
    with pytest.raises(InsufficientDisk):     # 12 - 4 (still to come) = 8 < 9.8
        check_disk_space(4 * GIB)
    check_disk_space(4 * GIB, exclude_job_id=1)
    # bytes already on disk are already reflected in free space, so only the rest is reserved
    Path(f"{dest}.temp").write_bytes(b"x" * 1024)
    assert downloader._inflight_remaining() == 4 * GIB - 1024


def test_queued_but_not_started_jobs_count(monkeypatch):
    set_free(monkeypatch, 12)
    with pytest.raises(InsufficientDisk):
        check_disk_space(4 * GIB, committed_bytes=4 * GIB)


async def test_download_waits_for_space_that_other_jobs_free(monkeypatch):
    box = set_free(monkeypatch, 6)
    (Path(settings.jobs_dir) / "7").mkdir(parents=True)     # another job's files are on disk
    monkeypatch.setattr(downloader, "DISK_WAIT_POLL_S", 0.01)
    job = type("J", (), {"id": 1, "chat_id": 1, "status_message_id": None})()
    waiter = asyncio.create_task(wait_for_disk_space(None, job, 4 * GIB))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    box["free"] = 20 * GIB
    await asyncio.wait_for(waiter, 2)


async def test_download_fails_fast_when_nothing_could_free_space(monkeypatch):
    set_free(monkeypatch, 6)
    job = type("J", (), {"id": 1, "chat_id": 1, "status_message_id": None})()
    with pytest.raises(InsufficientDisk):
        await asyncio.wait_for(wait_for_disk_space(None, job, 4 * GIB), 2)
