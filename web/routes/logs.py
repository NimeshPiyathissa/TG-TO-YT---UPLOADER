"""GET /logs: journalctl tail (structlog JSON) + audit-log tab + download (spec section 6)."""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse

from web.flash import pop_flashes

router = APIRouter()


async def _journal_lines(n: int) -> list[dict]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", "tgyt", "-n", str(n), "-o", "json", "--no-pager",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (FileNotFoundError, asyncio.TimeoutError):
        return []
    out = []
    for line in stdout.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            journal_entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = journal_entry.get("MESSAGE", "")
        entry = {"raw": message, "level": "INFO", "event": message, "timestamp": journal_entry.get("__REALTIME_TIMESTAMP")}
        try:
            app_entry = json.loads(message)
            entry["level"] = app_entry.get("level", "INFO").upper()
            entry["event"] = app_entry.get("event", message)
            entry["timestamp"] = app_entry.get("timestamp", entry["timestamp"])
            entry["fields"] = {k: v for k, v in app_entry.items() if k not in ("level", "event", "timestamp")}
        except (json.JSONDecodeError, TypeError):
            entry["fields"] = {}
        out.append(entry)
    return out


@router.get("/logs")
async def logs_page(request: Request, n: int = Query(200, ge=1, le=2000), level: Optional[str] = None,
                     q: Optional[str] = None, tab: str = "app"):
    templates = request.app.state.templates
    store = request.app.state.store
    entries = await _journal_lines(n)
    if level:
        entries = [e for e in entries if e["level"] == level.upper()]
    if q:
        needle = q.lower()
        entries = [e for e in entries if needle in json.dumps(e).lower()]
    audit = await store.list_audit_log(limit=200)
    return templates.TemplateResponse(request, "logs.html", {
        "entries": entries, "audit": audit, "n": n, "level": level or "", "q": q or "",
        "tab": tab, "flashes": pop_flashes(request),
    })


@router.get("/logs/download")
async def logs_download(n: int = Query(500, ge=1, le=5000)):
    entries = await _journal_lines(n)
    text = "\n".join(f"[{e['level']}] {e['event']}" for e in entries)
    return PlainTextResponse(text, headers={"Content-Disposition": "attachment; filename=tgyt.log"})
