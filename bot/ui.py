"""Progress bar rendering and throttled status-message editing (spec section 5.5).

One status message per job, edited in place. A single ticker task per job
coalesces rapid progress callbacks into edits no more than once every 5s, and
only when the bar has moved by at least 2%. Never edit directly from inside a
download/upload chunk loop -- call `update()` (cheap, in-memory) and let the
ticker decide when to actually hit the Telegram API.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import pyrogram
import structlog
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified

log = structlog.get_logger(__name__)

BAR_WIDTH = 16
MIN_EDIT_INTERVAL_S = 5.0
MIN_PCT_DELTA = 2.0


def render_bar(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(BAR_WIDTH * pct / 100)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_rate(bytes_per_s: float) -> str:
    return f"{human_bytes(bytes_per_s)}/s"


def human_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # NaN guard
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def human_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {seconds}s"


@dataclass
class _State:
    phase: str
    current: int
    total: int
    rate_bps: float = 0.0
    header_lines: tuple[str, ...] = ()
    footer_lines: tuple[str, ...] = ()


class ProgressReporter:
    """One instance per job. Call update() as often as you like; a ticker
    task decides when to actually hit the Telegram API."""

    PHASE_HEADER = {
        "download": "📥 <b>Downloading from Telegram</b>",
        "upload": "⬆️ <b>Uploading to YouTube</b>",
    }

    def __init__(self, client: pyrogram.Client, chat_id: int, message_id: int):
        self.client = client
        self.chat_id = chat_id
        self.message_id = message_id
        self._state: Optional[_State] = None
        self._last_sent_pct: float = -100.0
        self._last_sent_at: float = 0.0
        self._interval = MIN_EDIT_INTERVAL_S
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._prev_current = 0
        self._prev_ts = time.monotonic()

    def start(self) -> None:
        self._task = asyncio.create_task(self._ticker())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            await self._task

    def update(self, phase: str, current: int, total: int,
               header_lines: tuple[str, ...] = (), footer_lines: tuple[str, ...] = ()) -> None:
        now = time.monotonic()
        dt = max(now - self._prev_ts, 1e-6)
        rate = (current - self._prev_current) / dt
        self._prev_current, self._prev_ts = current, now
        self._state = _State(
            phase=phase, current=current, total=total, rate_bps=max(rate, 0.0),
            header_lines=header_lines, footer_lines=footer_lines,
        )

    def _render(self, state: _State) -> str:
        pct = (state.current / state.total * 100) if state.total else 0.0
        bar = render_bar(pct)
        eta = human_eta((state.total - state.current) / state.rate_bps) if state.rate_bps > 0 else "?"
        header = self.PHASE_HEADER.get(state.phase, state.phase)
        lines = [header, *state.header_lines, "",
                 f"<code>{bar}</code> {pct:.0f}%",
                 f"{human_bytes(state.current)} / {human_bytes(state.total)} · {human_rate(state.rate_bps)} · ~{eta} left"]
        lines.extend(state.footer_lines)
        return "\n".join(lines)

    async def _ticker(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
                break  # stopped
            except asyncio.TimeoutError:
                pass
            await self._maybe_flush()
        await self._maybe_flush(force=True)

    async def _maybe_flush(self, force: bool = False) -> None:
        state = self._state
        if state is None:
            return
        pct = (state.current / state.total * 100) if state.total else 0.0
        now = time.monotonic()
        moved_enough = abs(pct - self._last_sent_pct) >= MIN_PCT_DELTA
        time_ok = (now - self._last_sent_at) >= MIN_EDIT_INTERVAL_S
        if not force and not (moved_enough and time_ok):
            return
        text = self._render(state)
        try:
            await self.client.edit_message_text(self.chat_id, self.message_id, text, parse_mode=ParseMode.HTML)
            self._last_sent_pct = pct
            self._last_sent_at = now
            self._interval = MIN_EDIT_INTERVAL_S
        except FloodWait as e:
            log.warning("ui.flood_wait", seconds=e.value)
            self._interval = MIN_EDIT_INTERVAL_S + e.value + 1
            await asyncio.sleep(e.value + 1)
        except MessageNotModified:
            pass
        except Exception:
            log.warning("ui.edit_failed", exc_info=True)

    async def final_message(self, text: str, reply_markup=None) -> None:
        try:
            await self.client.edit_message_text(
                self.chat_id, self.message_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
        except MessageNotModified:
            pass
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            try:
                await self.client.edit_message_text(
                    self.chat_id, self.message_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
                )
            except Exception:
                log.warning("ui.final_message_failed", exc_info=True)
        except Exception:
            log.warning("ui.final_message_failed", exc_info=True)
