"""Session-stored flash messages, popped once at render time."""
from __future__ import annotations

from fastapi import Request


def flash(request: Request, text: str, level: str = "info") -> None:
    request.session.setdefault("flash", []).append({"level": level, "text": text})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("flash", [])
