"""Several YouTube channels under one Google account / one OAuth client: linking,
duplicate prevention, default + label management, and the bot's channel picker."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import aiosqlite
import pytest

from bot import flow
from bot.queue import JobQueue
from db.store import Store
from web.routes import channels as channels_routes
from yt import oauth

ACCOUNT_REFRESH = b"same-google-account-refresh-token"


async def add(store, channel_id, title, status="active", refresh=ACCOUNT_REFRESH):
    pk = await store.upsert_yt_channel(channel_id, title, f"@{title}", None, refresh, b"at", "2099-01-01T00:00:00+00:00",
                                       "scope", "tester")
    if status != "active":
        await store.set_channel_status(pk, status)
    return pk


# -- linking several channels from one Google account ----------------------------

@pytest.fixture
def fake_google(monkeypatch, store):
    """Run the real finalize_channel_from_code with Google replaced: the same account/client,
    but the channel chooser decides which Brand channel each consent grants."""
    chosen = {"channel": ("UC_A", "Channel A")}
    monkeypatch.setattr(channels_routes, "crypto",
                        SimpleNamespace(encrypt=lambda s: s.encode(), decrypt=lambda b: b.decode()))

    async def fake_exchange(client_id, client_secret, code, verifier, redirect_uri):
        return oauth.TokenResponse(access_token=f"at-{code}", refresh_token=f"rt-{code}", expires_in=3600,
                                   scope=oauth.SCOPES, token_type="Bearer")

    async def fake_list(access_token):
        cid, title = chosen["channel"]
        return [{"channel_id": cid, "title": title, "handle": f"@{title}", "thumbnail_url": None}]

    async def fake_email(access_token):
        return None

    monkeypatch.setattr(channels_routes, "fetch_account_email", fake_email)  # no network in tests
    monkeypatch.setattr(channels_routes, "exchange_code", fake_exchange)
    monkeypatch.setattr(channels_routes, "list_my_channels", fake_list)
    return chosen


async def link(store, code):
    await store.set_oauth_client("client-id", b"secret", "web", "proj", "tester")
    state_row = {"code_verifier_enc": b"verifier", "created_by": "tester"}
    return await channels_routes.finalize_channel_from_code(store, code, state_row, "https://x/oauth/callback")


async def test_two_brand_channels_from_one_account_are_two_rows_with_own_tokens(store, fake_google):
    first = await link(store, "one")
    fake_google["channel"] = ("UC_B", "Channel B")
    second = await link(store, "two")

    assert first["channel_pk"] != second["channel_pk"]
    assert not first["already_linked"] and not second["already_linked"]
    rows = await store.list_yt_channels()
    assert [r["channel_id"] for r in rows] == ["UC_A", "UC_B"]
    assert [bytes(r["refresh_token_enc"]) for r in rows] == [b"rt-one", b"rt-two"]  # second didn't overwrite first
    assert [r["is_default"] for r in rows] == [1, 0]  # only the first link became the default


async def test_relinking_same_channel_refreshes_instead_of_duplicating(store, fake_google):
    first = await link(store, "one")
    await store.set_default_channel(first["channel_pk"])
    await store.conn.execute("UPDATE yt_channels SET uploads_count = 7")
    await store.conn.commit()

    again = await link(store, "again")
    assert again["already_linked"] is True and again["channel_pk"] == first["channel_pk"]
    rows = await store.list_yt_channels()
    assert len(rows) == 1
    assert bytes(rows[0]["refresh_token_enc"]) == b"rt-again"          # token refreshed
    assert rows[0]["uploads_count"] == 7 and rows[0]["is_default"] == 1  # stats and default kept
    assert "already linked" in channels_routes.link_result_message(again)
    assert "Linked new channel" in channels_routes.link_result_message(first)


async def test_relink_restores_a_channel_that_needed_reauth(store, fake_google):
    first = await link(store, "one")
    await store.set_channel_status(first["channel_pk"], "needs_reauth")
    await link(store, "fixed")
    assert (await store.get_yt_channel(first["channel_pk"]))["status"] == "active"


def test_auth_url_always_requests_the_account_chooser():
    url = oauth.build_authorization_url("cid", "https://x/oauth/callback", "state", "challenge")
    query = parse_qs(urlparse(url).query)
    assert query["prompt"] == ["select_account consent"] and query["access_type"] == ["offline"]


# -- default channel and display label -------------------------------------------

async def test_default_channel_is_single_and_must_be_active(store):
    a, b = await add(store, "UC_A", "A"), await add(store, "UC_B", "B")
    broken = await add(store, "UC_C", "C", status="needs_reauth")
    assert [r["is_default"] for r in await store.list_yt_channels()] == [1, 0, 0]

    assert await store.set_default_channel(b) is True
    assert [r["is_default"] for r in await store.list_yt_channels()] == [0, 1, 0]
    assert (await store.get_default_channel())["id"] == b

    assert await store.set_default_channel(broken) is False   # broken channel refused
    assert await store.set_default_channel(9999) is False     # unknown channel refused
    assert (await store.get_default_channel())["id"] == b      # unchanged


async def test_removing_the_default_promotes_another_active_channel(store):
    a, b = await add(store, "UC_A", "A"), await add(store, "UC_B", "B")
    await store.delete_channel(a)
    assert (await store.get_yt_channel(b))["is_default"] == 1


async def test_display_label_overrides_title_everywhere_and_can_be_cleared(store):
    pk = await add(store, "UC_A", "Google Title")
    assert await store.set_channel_label(pk, "  Main show  ") is True

    for row in (await store.get_yt_channel(pk), (await store.list_yt_channels())[0],
                await store.get_default_channel(), await store.get_yt_channel_by_channel_id("UC_A")):
        assert row["channel_title"] == "Main show" and row["youtube_title"] == "Google Title"

    # Re-linking refreshes Google's title but must not wipe the operator's label.
    await store.upsert_yt_channel("UC_A", "Renamed On YouTube", None, None, b"r", b"a", "x", "s", "t")
    row = await store.get_yt_channel(pk)
    assert row["channel_title"] == "Main show" and row["youtube_title"] == "Renamed On YouTube"

    await store.set_channel_label(pk, "")
    assert (await store.get_yt_channel(pk))["channel_title"] == "Renamed On YouTube"
    assert await store.set_channel_label(9999, "x") is False


async def test_existing_database_without_label_column_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as db:
        await db.execute("""CREATE TABLE yt_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL UNIQUE, channel_title TEXT NOT NULL,
            handle TEXT, thumbnail_url TEXT, refresh_token_enc BLOB NOT NULL, access_token_enc BLOB,
            access_expires_at TEXT, scopes TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            is_default INTEGER NOT NULL DEFAULT 0, added_at TEXT NOT NULL, added_by TEXT, last_used_at TEXT,
            uploads_count INTEGER NOT NULL DEFAULT 0)""")
        await db.execute("INSERT INTO yt_channels (channel_id, channel_title, refresh_token_enc, scopes, added_at) "
                         "VALUES ('UC_OLD', 'Old', x'00', 's', 'now')")
        await db.commit()
    store = Store(str(path))
    await store.connect()
    try:
        row = (await store.list_yt_channels())[0]
        assert row["channel_title"] == "Old" and row["display_label"] is None
        assert await store.set_channel_label(row["id"], "Renamed")
    finally:
        await store.close()


# -- the bot's channel picker -----------------------------------------------------

def button_texts(markup):
    return [row[0].text for row in markup.inline_keyboard]


async def prompt_for_new_job(store):
    queue = JobQueue(AsyncMock(), None, store)
    job_id = await store.create_job(1, 1, source_chat_id=1, source_message_id=1, file_name="v.mp4",
                                    size_bytes=10, mime="video/mp4")
    await store.update_job_fields(job_id, state="AWAITING_CHANNEL", status_message_id=5)
    await queue._post_channel_or_caption_prompt(await store.get_job(job_id))
    _, _, text = queue.bot.edit_message_text.call_args.args
    return text, queue.bot.edit_message_text.call_args.kwargs["reply_markup"], job_id


async def test_picker_lists_every_active_channel_default_first(store):
    await add(store, "UC_A", "Alpha")
    b = await add(store, "UC_B", "Beta")
    await add(store, "UC_C", "Gamma")
    await store.set_default_channel(b)

    text, markup, _ = await prompt_for_new_job(store)
    assert "Where should this go?" in text and "Skipped" not in text
    assert button_texts(markup) == ["📺 ⭐ Beta", "📺 Alpha", "📺 Gamma", "❌ Cancel"]


async def test_picker_skips_channels_needing_reauth_and_says_so(store):
    await add(store, "UC_A", "Alpha")
    await add(store, "UC_B", "Beta")
    await add(store, "UC_C", "Needs <Fix>", status="needs_reauth")

    text, markup, _ = await prompt_for_new_job(store)
    assert button_texts(markup) == ["📺 ⭐ Alpha", "📺 Beta", "❌ Cancel"]
    assert "Skipped (needs re-authorization)" in text and "Needs &lt;Fix&gt;" in text  # escaped for HTML


async def test_only_one_active_channel_is_auto_selected_with_note(store):
    good = await add(store, "UC_A", "Alpha")
    await add(store, "UC_B", "Beta", status="needs_reauth")

    text, _, job_id = await prompt_for_new_job(store)
    assert "only active channel" in text and "Skipped" in text
    assert (await store.get_job(job_id)).channel_pk == good


async def test_no_usable_channel_offers_only_cancel(store):
    await add(store, "UC_A", "Alpha", status="needs_reauth")
    text, markup, job_id = await prompt_for_new_job(store)
    assert button_texts(markup) == ["❌ Cancel"] and "No channel can take this upload" in text
    assert (await store.get_job(job_id)).state == "AWAITING_CHANNEL"


async def test_channel_linked_after_startup_shows_in_next_picker_without_restart(store):
    await add(store, "UC_A", "Alpha")
    await add(store, "UC_B", "Beta")
    _, first, _ = await prompt_for_new_job(store)
    assert len(first.inline_keyboard) == 3

    await add(store, "UC_NEW", "Brand New")  # linked from the panel while the bot keeps running
    _, second, _ = await prompt_for_new_job(store)
    assert button_texts(second)[-2] == "📺 Brand New"


def test_split_channels_and_label_used_by_picker_keyboard():
    rows = [{"id": 1, "status": "active", "channel_title": "A", "is_default": 0},
            {"id": 2, "status": "needs_reauth", "channel_title": "B", "is_default": 0},
            {"id": 3, "status": "revoked", "channel_title": "C", "is_default": 0}]
    active, broken = flow.split_channels(rows)
    assert [c["id"] for c in active] == [1] and [c["id"] for c in broken] == [2]
    assert flow.reauth_note([]) == ""
