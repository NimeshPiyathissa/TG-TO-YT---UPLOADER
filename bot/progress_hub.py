"""Live transfer progress for running jobs, shared between the bot side (which knows
how far a download/upload has got) and the web UI (which streams it to the browser).

The bot and the web app run in one process, so this is a plain in-memory registry --
nothing is written to SQLite. Only upload progress was ever persisted (``bytes_sent``);
download progress lived solely in the Telegram status message, so the dashboard had
nothing to show during a download.

Rate is measured over windows of at least a second and smoothed, because the
callbacks fire per chunk (download chunks arrive from a worker thread, hence the lock).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

_WINDOW_S = 1.0
_SMOOTHING = 0.35        # weight of the newest window in the rate estimate
_STALE_AFTER_S = 30.0    # drop a rate that hasn't been refreshed (transfer stalled)


@dataclass
class Progress:
    phase: str               # "download" | "upload"
    current: int
    total: int
    rate_bps: float
    updated: float
    _win_start: float
    _win_bytes: int


_lock = threading.Lock()
_jobs: dict[int, Progress] = {}


def record(job_id: int, phase: str, current: int, total: int) -> None:
    now = time.monotonic()
    with _lock:
        prev = _jobs.get(job_id)
        if prev is None or prev.phase != phase or current < prev._win_bytes:
            _jobs[job_id] = Progress(phase, current, total, 0.0, now, now, current)
            return
        rate = prev.rate_bps
        win_start, win_bytes = prev._win_start, prev._win_bytes
        if now - win_start >= _WINDOW_S:
            inst = (current - win_bytes) / (now - win_start)
            rate = inst if rate == 0 else rate * (1 - _SMOOTHING) + inst * _SMOOTHING
            win_start, win_bytes = now, current
        _jobs[job_id] = Progress(phase, current, total, rate, now, win_start, win_bytes)


def get(job_id: int) -> Optional[Progress]:
    now = time.monotonic()
    with _lock:
        p = _jobs.get(job_id)
        if p is None:
            return None
        if now - p.updated > _STALE_AFTER_S and p.rate_bps:
            return Progress(p.phase, p.current, p.total, 0.0, p.updated, p._win_start, p._win_bytes)
        return p


def clear(job_id: int) -> None:
    with _lock:
        _jobs.pop(job_id, None)
