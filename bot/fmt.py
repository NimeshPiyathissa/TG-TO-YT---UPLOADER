"""HTML message-formatting helpers.

Pyrogram's own HTML parser (Kurigram 2.1.x) is used for every bot message, so
custom emoji are written as ``<emoji id="...">`` (its spelling of the Bot API's
``<tg-emoji emoji-id="...">``). Entity offsets are computed by the parser in
UTF-16 units. Anything user- or Google-supplied must go through ``esc()``.
"""
from __future__ import annotations

import html

# Custom emoji ids -> fallback unicode glyph shown when a client can't render them.
EMOJI_PLAY = (6141129078033358884, "▶️")      # TG -> YouTube Uploader
EMOJI_CHANNELS = (6140885579157479105, "📱")
EMOJI_UPLOADS = (5456140674028019486, "⚡️")
EMOJI_OTHER = (5341715473882955310, "⚙️")
EMOJI_LINKED = (6212828884115660309, "🔗")
EMOJI_QUOTA = (5422360266618707867, "📊")
EMOJI_WARN = (5420323339723881652, "⚠️")
EMOJI_OK = (6138698461551273388, "✅")
EMOJI_STATUS = (5899885107228971015, "⚡️")

# Reply-keyboard / inline button icons (kept for when the library supports them).
BTN_MY_CHANNELS = (5429277560557019280, "▶️")
BTN_STATUS = (5899885107228971015, "⚡️")
BTN_HELP = (5436113877181941026, "❓")


def esc(value: object) -> str:
    """Escape dynamic text for use inside an HTML-parsed message."""
    return html.escape(str(value), quote=False)


def b(value: object) -> str:
    return f"<b>{esc(value)}</b>"


def emoji(spec: tuple[int, str]) -> str:
    emoji_id, glyph = spec
    return f'<emoji id="{emoji_id}">{glyph}</emoji>'


def user_link(user_id: int, first_name: str | None) -> str:
    """Bold + clickable name linking to the user's Telegram profile."""
    return f'<b><a href="tg://user?id={int(user_id)}">{esc(first_name or "there")}</a></b>'
