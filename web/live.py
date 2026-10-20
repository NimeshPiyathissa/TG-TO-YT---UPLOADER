"""What the UI shows for a job's progress, shared by the server-rendered pages and the
``/events`` stream so both always agree.

A job's journey is two legs: *pull* from Telegram, then *push* to YouTube. Each leg
is a fraction 0..1. Download progress comes from the in-memory hub (it is never in
SQLite); upload progress from the hub too, falling back to the persisted
``bytes_sent`` when the hub has nothing (for example right after a restart).
"""
from __future__ import annotations

from typing import Optional

from bot import progress_hub

PULL_STATES = ("QUEUED", "DOWNLOADING")
BETWEEN_STATES = ("AWAITING_CHANNEL", "AWAITING_CAPTION", "READY")
PUSH_STATES = ("UPLOADING", "VERIFYING")
MOVING_STATES = ("DOWNLOADING", "UPLOADING", "VERIFYING")


def _frac(current: int, total: int) -> float:
    return max(0.0, min(1.0, current / total)) if total else 0.0


def job_view(job) -> dict:
    size = job.size_bytes or 0
    pull = push = 0.0
    cur, total, rate = 0, size, 0.0
    leg: Optional[str] = None
    p = progress_hub.get(job.id)

    if job.state in PULL_STATES:
        leg = "pull"
        if job.state == "DOWNLOADING" and p and p.phase == "download" and p.total:
            pull, cur, total, rate = _frac(p.current, p.total), p.current, p.total, p.rate_bps
    elif job.state in BETWEEN_STATES:
        pull = 1.0
    elif job.state in PUSH_STATES:
        pull, leg = 1.0, "push"
        if p and p.phase == "upload" and p.total:
            push, cur, total, rate = _frac(p.current, p.total), p.current, p.total, p.rate_bps
        else:
            push, cur = _frac(job.bytes_sent or 0, size), job.bytes_sent or 0
        if job.state == "VERIFYING":
            push = 1.0
    elif job.state == "DONE":
        pull = push = 1.0
    else:  # FAILED / CANCELLED: show how far it got
        sent = job.bytes_sent or 0
        pull = 1.0 if (sent or job.local_path) else 0.0
        push, cur = _frac(sent, size), sent

    remaining = max(0, total - cur)
    eta = round(remaining / rate) if rate > 0 and remaining else None
    return {
        "id": job.id, "state": job.state, "leg": leg,
        "pull": round(pull, 4), "push": round(push, 4),
        "cur": cur, "total": total, "rate": round(rate), "eta": eta,
    }


def row_progress(view: dict) -> tuple[Optional[str], float]:
    """(label, fraction) for the single bar in a table row: the leg that is moving,
    or the whole journey once it is over."""
    if view["state"] == "DONE":
        return "Done", 1.0
    if view["leg"] == "pull":
        return "Pull", view["pull"]
    if view["leg"] == "push":
        return "Push", view["push"]
    if view["state"] in ("FAILED", "CANCELLED"):
        return None, view["push"]
    return None, 0.0
