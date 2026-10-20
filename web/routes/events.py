"""GET /events -- Server-Sent Events stream of live job progress.

One-way push over plain HTTP, behind the same login as every other page. The browser
applies each event to the bars already on the page, so they can animate instead of
being replaced. The page's htmx polling remains as the fallback if this stream is
unavailable.
"""
from __future__ import annotations

import asyncio
import json
import time

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from web.live import job_view

log = structlog.get_logger(__name__)
router = APIRouter()

TICK_S = 1.0
HEARTBEAT_S = 15.0       # keeps proxies (nginx read timeout is 60s) from closing an idle stream
MAX_STREAM_S = 30 * 60   # reconnect now and then so the session is re-checked


async def event_stream(request: Request, store, *, tick: float = TICK_S, heartbeat: float = HEARTBEAT_S,
                       max_seconds: float = MAX_STREAM_S):
    started = last_sent = time.monotonic()
    last_payload = None
    yield "retry: 3000\n\n"
    try:
        while time.monotonic() - started < max_seconds:
            if await request.is_disconnected():
                return
            jobs = [job_view(j) for j in await store.list_active_jobs()]
            payload = json.dumps({"jobs": jobs}, separators=(",", ":"))
            now = time.monotonic()
            if payload != last_payload:
                yield f"event: jobs\ndata: {payload}\n\n"
                last_payload, last_sent = payload, now
            elif now - last_sent >= heartbeat:
                yield ": ping\n\n"
                last_sent = now
            await asyncio.sleep(tick)
    finally:
        log.debug("events.closed")


@router.get("/events")
async def events(request: Request):
    return StreamingResponse(
        event_stream(request, request.app.state.store),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
