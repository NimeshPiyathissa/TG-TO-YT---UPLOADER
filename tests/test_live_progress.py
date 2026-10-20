"""Live progress: bot side feeds progress_hub, web.live turns it into a view, /events streams it."""
import json

import httpx
import pytest

from bot import progress_hub
from web.app import create_app
from web.live import job_view
from web.routes.events import event_stream


@pytest.fixture(autouse=True)
def _clean_hub():
    progress_hub._jobs.clear()
    yield
    progress_hub._jobs.clear()


class _Req:
    """Stands in for starlette Request: never disconnects, so max_seconds ends the stream."""
    async def is_disconnected(self):
        return False


async def _collect(store, **kw):
    return [chunk async for chunk in event_stream(_Req(), store, **kw)]


def _jobs_events(chunks):
    return [json.loads(c.split("data: ", 1)[1]) for c in chunks if c.startswith("event: jobs")]


async def _job(store, size=1000):
    return await store.create_job(1, 1, file_name="a.mp4", size_bytes=size)


def test_hub_rate_needs_a_window(monkeypatch):
    t = [100.0]
    monkeypatch.setattr(progress_hub.time, "monotonic", lambda: t[0])
    progress_hub.record(1, "download", 0, 1000)
    t[0] += 0.4
    progress_hub.record(1, "download", 100, 1000)
    assert progress_hub.get(1).rate_bps == 0          # window not elapsed yet
    t[0] += 0.7
    progress_hub.record(1, "download", 300, 1000)
    assert progress_hub.get(1).rate_bps == pytest.approx(300 / 1.1)


def test_hub_phase_change_resets_and_clear_removes():
    progress_hub.record(1, "download", 900, 1000)
    progress_hub.record(1, "upload", 10, 1000)
    assert progress_hub.get(1).phase == "upload" and progress_hub.get(1).current == 10
    progress_hub.clear(1)
    assert progress_hub.get(1) is None


async def test_job_view_download_reads_hub(store):
    jid = await _job(store)
    await store.transition_job(jid, "DOWNLOADING")
    progress_hub.record(jid, "download", 250, 1000)
    v = job_view(await store.get_job(jid))
    assert v["leg"] == "pull" and v["pull"] == 0.25 and v["cur"] == 250 and v["push"] == 0.0


async def test_job_view_upload_falls_back_to_bytes_sent(store):
    jid = await _job(store)
    await store.update_job_fields(jid, state="UPLOADING", bytes_sent=400)
    v = job_view(await store.get_job(jid))
    assert v["leg"] == "push" and v["pull"] == 1.0 and v["push"] == 0.4
    progress_hub.record(jid, "upload", 800, 1000)
    assert job_view(await store.get_job(jid))["push"] == 0.8


async def test_stream_emits_only_changes_and_heartbeats(store):
    jid = await _job(store)
    await store.transition_job(jid, "DOWNLOADING")
    progress_hub.record(jid, "download", 100, 1000)
    chunks = await _collect(store, tick=0.05, heartbeat=0.12, max_seconds=0.4)
    assert chunks[0] == "retry: 3000\n\n"
    events = _jobs_events(chunks)
    assert len(events) == 1                              # unchanged state is not re-sent
    assert events[0]["jobs"][0]["pull"] == 0.1
    assert ": ping\n\n" in chunks


async def test_stream_pushes_new_progress(store):
    jid = await _job(store)
    await store.transition_job(jid, "DOWNLOADING")
    progress_hub.record(jid, "download", 100, 1000)
    got = []

    async def consume():
        async for c in event_stream(_Req(), store, tick=0.05, max_seconds=0.6):
            got.append(c)
            if c.startswith("event: jobs") and len(_jobs_events(got)) == 1:
                progress_hub.record(jid, "download", 600, 1000)   # progress arrives mid-stream

    await consume()
    pulls = [e["jobs"][0]["pull"] for e in _jobs_events(got)]
    assert pulls == [0.1, 0.6]


async def test_events_endpoint_requires_login_with_401(store):
    app = create_app(store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/events")
    assert r.status_code == 401
