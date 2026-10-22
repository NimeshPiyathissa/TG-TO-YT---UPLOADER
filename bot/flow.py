"""Pure helpers for the video -> channel -> caption conversation (spec section 5.4).

Conversation state itself lives in ``db.store``'s ``flow_sessions`` table (survives
a restart); this module only builds the messages/keyboards and validates input.
Steps: ``awaiting_channel`` -> ``awaiting_caption``. Skipped entirely to
``awaiting_caption`` when only one channel is linked (spec 5.4 step 2).
"""
from __future__ import annotations

from dataclasses import dataclass

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.fmt import b, esc

MAX_TITLE = 100
MAX_DESC = 5000


class ValidationError(Exception):
    pass


def validate_caption(raw_text: str) -> tuple[str, str]:
    lines = raw_text.split("\n", 1)
    title = lines[0].strip().replace("<", "").replace(">", "")
    description = (lines[1].strip() if len(lines) > 1 else "")[:MAX_DESC + 1]
    if not title:
        raise ValidationError("The title can't be empty. Send at least one line of text.")
    if len(title) > MAX_TITLE:
        raise ValidationError(f"That title is {len(title)} characters -- max is {MAX_TITLE}. Trim it and resend.")
    if len(description) > MAX_DESC:
        raise ValidationError(f"That description is {len(description)} characters -- max is {MAX_DESC}. Trim it and resend.")
    return title, description


def split_channels(channels: list) -> tuple[list, list]:
    """(usable, needs_reauth): only active channels can receive an upload."""
    return ([c for c in channels if c["status"] == "active"],
            [c for c in channels if c["status"] == "needs_reauth"])


def reauth_note(needs_reauth: list) -> str:
    """Tell the user which channels were left out of the picker and why ('' if none)."""
    if not needs_reauth:
        return ""
    names = ", ".join(esc(c["channel_title"]) for c in needs_reauth)
    return ("\n\n⚠️ <b>Skipped (needs re-authorization):</b> " + names + "\n"
            "An admin can relink them in the web panel under Channels.")


def channel_picker_text(file_name: str, size_bytes: int, duration_s: float | None,
                         width: int | None, height: int | None, note: str = "") -> str:
    from bot.ui import human_bytes

    dims = f"{width}×{height}" if width and height else "unknown"
    dur = _fmt_duration(duration_s) if duration_s else "?"
    return (
        f"✅ <b>Video received</b> — <code>{esc(file_name)}</code>\n"
        f"📦 {human_bytes(size_bytes)} · ⏱ {dur} · {dims}\n\n"
        f"📺 <b>Where should this go?</b>"
        f"{note}"
    )


def channel_picker_keyboard(channels: list, job_id: int) -> InlineKeyboardMarkup:
    rows = []
    for ch in sorted(channels, key=lambda c: not c["is_default"]):  # default channel first, rest keep order
        star = "⭐ " if ch["is_default"] else ""
        rows.append([InlineKeyboardButton(f"📺 {star}{ch['channel_title']}", callback_data=f"pickch:{job_id}:{ch['id']}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"canceljob:{job_id}")])
    return InlineKeyboardMarkup(rows)


def caption_prompt_text(channel_title: str) -> str:
    return (
        f"📺 Channel: {b(channel_title)}\n\n"
        f"✍️ <b>Send me a caption for this video.</b>\n"
        f"The first line becomes the <b>title</b> (max 100 chars), everything after it "
        f"becomes the <b>description</b>.\n\n"
        f"Or tap below to use the filename as the title."
    )


def caption_prompt_keyboard(job_id: int, single_channel: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📄 Use filename", callback_data=f"usefilename:{job_id}")]]
    if not single_channel:
        rows[0].append(InlineKeyboardButton("⬅️ Change channel", callback_data=f"changechannel:{job_id}"))
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"canceljob:{job_id}")])
    return InlineKeyboardMarkup(rows)


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
