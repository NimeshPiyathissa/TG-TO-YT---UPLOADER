"""Admin Panel -> Telegram notification on admin promotion (bot/notify.py)."""
import asyncio

import pytest
from pyrogram.errors import PeerIdInvalid

from bot import access, notify


class FakeBot:
    def __init__(self, exc=None):
        self.exc, self.sent = exc, []

    async def send_message(self, chat_id, text, **kwargs):
        if self.exc:
            raise self.exc
        self.sent.append((chat_id, text, kwargs))


@pytest.mark.asyncio
async def test_promotion_sends_message_with_html_and_no_markdown():
    bot = FakeBot()
    assert await notify.notify_role_change(bot, 42, "blocked", "admin", first_name="A<b>&") is True
    chat_id, text, kwargs = bot.sent[0]
    assert chat_id == 42 and kwargs["parse_mode"].name == "HTML"
    assert "Admin access granted" in text and "A&lt;b&gt;&amp;" in text and "*" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("previous,new", [
    ("admin", "admin"), ("owner", "admin"),          # no-op / demotion
    ("blocked", "uploader"), ("admin", "blocked"),   # not an admin promotion
])
async def test_no_message_unless_promoted_to_admin(previous, new):
    bot = FakeBot()
    assert await notify.notify_role_change(bot, 42, previous, new) is None
    assert bot.sent == []


@pytest.mark.asyncio
async def test_new_user_without_row_is_notified_and_missing_bot_is_skipped():
    assert await notify.notify_role_change(FakeBot(), 7, None, "admin") is True
    assert await notify.notify_role_change(None, 7, "uploader", "admin") is None


@pytest.mark.asyncio
async def test_undeliverable_dm_returns_false_and_never_raises():
    # Telegram refuses to DM someone who never started the bot.
    assert await notify.notify_role_change(FakeBot(PeerIdInvalid()), 42, "blocked", "admin") is False
    assert await notify.notify_role_change(FakeBot(KeyError("peer")), 42, "blocked", "admin") is False
    assert await notify.notify_role_change(FakeBot(RuntimeError("boom")), 42, "blocked", "admin") is False


@pytest.mark.asyncio
async def test_role_change_applies_to_next_check_without_restart(store):
    """The bot re-reads tg_users.role on every update, so the panel's write is live at once."""
    assert await access.check_access(store, 99, "u", "U") == "blocked"
    assert not access.is_admin("blocked")
    await store.set_tg_user_role(99, "admin", added_by="web")
    role = await access.check_access(store, 99, "u", "U")
    assert role == "admin" and access.is_admin(role) and access.is_authorized(role)
    await store.set_tg_user_role(99, "blocked", added_by="web")
    assert not access.is_authorized(await access.check_access(store, 99, "u", "U"))
