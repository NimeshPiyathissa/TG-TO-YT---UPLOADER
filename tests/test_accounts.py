"""Channels grouped by owning Google account: recording the email at link time, the
login_hint "add another from this account" flow, and the grouped panel / bot views."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import Request

import web.app as webapp
from bot import flow
from bot.handlers.channels import _render_channel_list
from bot.queue import JobQueue
from web.routes import channels as channels_routes
from yt import oauth

EXP = "2099-01-01T00:00:00+00:00"


async def add(store, channel_id, title, email=None, status="active", subs=None):
    pk = await store.upsert_yt_channel(channel_id, title, f"@{title}", None, b"r", b"a", EXP, "s", "tester",
                                       google_email=email, subscriber_count=subs)
    if status != "active":
        await store.set_channel_status(pk, status)
    return pk


# -- recording the account at link time -------------------------------------------

@pytest.fixture
def fake_google(monkeypatch):
    """Google replaced: each consent grants the chosen channel, owned by the chosen account."""
    chosen = {"channel": ("UC_A", "Channel A", 1500), "email": "owner@gmail.com"}
    monkeypatch.setattr(channels_routes, "crypto",
                        SimpleNamespace(encrypt=lambda s: s.encode(), decrypt=lambda b: b.decode()))

    async def fake_exchange(*a, **k):
        return oauth.TokenResponse(access_token="at", refresh_token="rt", expires_in=3600, scope=oauth.SCOPES,
                                   token_type="Bearer")

    async def fake_list(access_token):
        cid, title, subs = chosen["channel"]
        return [{"channel_id": cid, "title": title, "handle": None, "thumbnail_url": None, "subscriber_count": subs}]

    async def fake_email(access_token):
        return chosen["email"]

    monkeypatch.setattr(channels_routes, "exchange_code", fake_exchange)
    monkeypatch.setattr(channels_routes, "list_my_channels", fake_list)
    monkeypatch.setattr(channels_routes, "fetch_account_email", fake_email)
    return chosen


async def link(store):
    await store.set_oauth_client("client-id", b"secret", "web", "proj", "tester")
    return await channels_routes.finalize_channel_from_code(
        store, "code", {"code_verifier_enc": b"v", "created_by": "tester"}, "https://x/oauth/callback")


async def test_link_records_account_email_and_subscribers(store, fake_google):
    result = await link(store)
    row = await store.get_yt_channel(result["channel_pk"])
    assert row["google_email"] == "owner@gmail.com" and row["subscriber_count"] == 1500
    assert "owner@gmail.com" in channels_routes.link_result_message(result)


async def test_brand_channels_of_one_account_share_an_email_other_account_differs(store, fake_google):
    await link(store)
    fake_google["channel"] = ("UC_B", "Channel B", None)  # hidden subscriber count
    await link(store)
    fake_google["channel"], fake_google["email"] = ("UC_C", "Channel C", 7), "other@gmail.com"
    await link(store)
    rows = {r["channel_id"]: r for r in await store.list_yt_channels()}
    assert [rows[k]["google_email"] for k in ("UC_A", "UC_B", "UC_C")] == ["owner@gmail.com"] * 2 + ["other@gmail.com"]
    assert rows["UC_B"]["subscriber_count"] is None


async def test_relink_without_email_keeps_known_email_but_new_email_replaces_it(store, fake_google):
    first = await link(store)
    fake_google["email"] = None  # e.g. the email permission was unticked
    result = await link(store)
    assert result["email"] is None and "Account not recorded" in channels_routes.link_result_message(result)
    assert (await store.get_yt_channel(first["channel_pk"]))["google_email"] == "owner@gmail.com"
    fake_google["email"] = "moved@gmail.com"
    await link(store)
    assert (await store.get_yt_channel(first["channel_pk"]))["google_email"] == "moved@gmail.com"


async def test_channel_linked_before_emails_existed_has_no_account(store):
    pk = await add(store, "UC_OLD", "Old")
    assert (await store.get_yt_channel(pk))["google_email"] is None


class FakeSession:
    def __init__(self, status=200, payload=None, exc=None):
        self.status, self.payload, self.exc = status, payload or {}, exc

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, headers=None):
        if self.exc:
            raise self.exc
        return self

    async def json(self):
        return self.payload


async def test_fetch_account_email_is_forgiving(monkeypatch):
    monkeypatch.setattr(oauth.aiohttp, "ClientSession", FakeSession(payload={"email": "  Me@Gmail.COM "}))
    assert await oauth.fetch_account_email("tok") == "me@gmail.com"
    monkeypatch.setattr(oauth.aiohttp, "ClientSession", FakeSession(status=401))
    assert await oauth.fetch_account_email("tok") is None
    monkeypatch.setattr(oauth.aiohttp, "ClientSession", FakeSession(payload={}))
    assert await oauth.fetch_account_email("tok") is None
    monkeypatch.setattr(oauth.aiohttp, "ClientSession", FakeSession(exc=RuntimeError("down")))
    assert await oauth.fetch_account_email("tok") is None


def test_link_flow_requests_email_scopes_and_login_hint():
    assert "openid" in oauth.SCOPES and "email" in oauth.SCOPES.split()
    q = parse_qs(urlparse(oauth.build_authorization_url("cid", "https://x/cb", "s", "c", login_hint="me@gmail.com")).query)
    assert q["login_hint"] == ["me@gmail.com"] and q["prompt"] == ["select_account consent"]
    assert "login_hint" not in parse_qs(urlparse(oauth.build_authorization_url("cid", "https://x/cb", "s", "c")).query)


def test_login_hint_must_be_a_plain_email():
    assert channels_routes._clean_hint("  Me@Gmail.com ") == "me@gmail.com"
    for bad in ("", None, "not an email", "a@b", "a b@c.com", "a@b@c.com"):
        assert channels_routes._clean_hint(bad) is None


async def test_reauthorize_and_add_from_account_send_that_accounts_hint(store, fake_google, monkeypatch):
    monkeypatch.setattr(channels_routes.settings, "oauth_mode", "hostname")
    result = await link(store)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=store)), session={})

    resp = await channels_routes.reauthorize(result["channel_pk"], request, csrf_ok=None)
    assert parse_qs(urlparse(resp.headers["location"]).query)["login_hint"] == ["owner@gmail.com"]

    resp = await channels_routes.link_start(request, login_hint="Other@Gmail.com", csrf_ok=None)
    assert parse_qs(urlparse(resp.headers["location"]).query)["login_hint"] == ["other@gmail.com"]

    resp = await channels_routes.link_start(request, login_hint="", csrf_ok=None)  # brand-new account: no hint
    assert "login_hint" not in parse_qs(urlparse(resp.headers["location"]).query)


# -- grouping -----------------------------------------------------------------------

async def test_group_by_account_default_leads_and_unrecorded_is_last(store):
    await add(store, "UC1", "Old channel")                          # no email
    await add(store, "UC2", "B1", email="b@gmail.com")
    a1 = await add(store, "UC3", "A1", email="a@gmail.com")
    await add(store, "UC4", "B2", email="b@gmail.com")
    channels = await store.list_yt_channels()

    groups = flow.group_by_account(channels, default_group_first=False)
    assert [(e, [c["channel_title"] for c in m]) for e, m in groups] == [
        ("b@gmail.com", ["B1", "B2"]), ("a@gmail.com", ["A1"]), (None, ["Old channel"])]  # oldest account first

    await store.set_default_channel(a1)
    groups = flow.group_by_account(await store.list_yt_channels())
    assert [e for e, _ in groups] == ["a@gmail.com", "b@gmail.com", None]  # default's account leads
    b2 = [c for c in await store.list_yt_channels() if c["channel_title"] == "B2"][0]
    await store.set_default_channel(b2["id"])
    (email, members), *_ = flow.group_by_account(await store.list_yt_channels())
    assert email == "b@gmail.com" and [c["channel_title"] for c in members] == ["B2", "B1"]  # default first in group


def buttons(markup):
    return [(row[0].text, row[0].callback_data) for row in markup.inline_keyboard]


async def test_picker_groups_by_account_with_inert_headers(store):
    await add(store, "UC1", "A1", email="a@gmail.com")
    await add(store, "UC2", "B1", email="b@gmail.com")
    await add(store, "UC3", "A2", email="a@gmail.com")
    await add(store, "UC4", "Legacy")
    rows = buttons(flow.channel_picker_keyboard(await store.list_yt_channels(), 9))
    texts = [t for t, _ in rows]
    assert texts == ["👤 a@gmail.com", "📺 ⭐ A1", "📺 A2", "👤 b@gmail.com", "📺 B1",
                     f"👤 {flow.UNRECORDED_ACCOUNT}", "📺 Legacy", "❌ Cancel"]
    assert [d for t, d in rows if t.startswith("👤")] == ["acct"] * 3            # headers do nothing
    assert all(d.startswith("pickch:9:") for t, d in rows if t.startswith("📺"))  # channels still pick


async def test_picker_has_no_headers_for_a_single_account(store):
    await add(store, "UC1", "A1", email="a@gmail.com")
    await add(store, "UC2", "A2", email="a@gmail.com")
    assert [t for t, _ in buttons(flow.channel_picker_keyboard(await store.list_yt_channels(), 1))] == \
        ["📺 ⭐ A1", "📺 A2", "❌ Cancel"]


async def test_bot_picker_skips_broken_channels_inside_groups(store):
    await add(store, "UC1", "A1", email="a@gmail.com")
    await add(store, "UC2", "B1", email="b@gmail.com")
    await add(store, "UC3", "B2", email="b@gmail.com", status="needs_reauth")
    queue = JobQueue(AsyncMock(), None, store)
    job_id = await store.create_job(1, 1, source_chat_id=1, source_message_id=1, file_name="v.mp4", size_bytes=1,
                                    mime="video/mp4")
    await store.update_job_fields(job_id, state="AWAITING_CHANNEL", status_message_id=5)
    await queue._post_channel_or_caption_prompt(await store.get_job(job_id))
    text = queue.bot.edit_message_text.call_args.args[2]
    markup = queue.bot.edit_message_text.call_args.kwargs["reply_markup"]
    assert [t for t, _ in buttons(markup)] == ["👤 a@gmail.com", "📺 ⭐ A1", "👤 b@gmail.com", "📺 B1", "❌ Cancel"]
    assert "Skipped" in text and "B2" in text


async def test_channels_command_groups_numbers_continuously_and_escapes(store):
    await add(store, "UC1", "A1", email="a@gmail.com")
    await add(store, "UC2", "B1", email="<b>@evil.com")
    await add(store, "UC3", "Legacy")
    text = await _render_channel_list(store)
    lines = text.split("\n")
    assert "👤 <b>a@gmail.com</b>" in lines and "👤 <b>&lt;b&gt;@evil.com</b>" in lines
    assert f"👤 <b>{flow.UNRECORDED_ACCOUNT}</b>" in lines
    assert [l[:2] for l in lines if l[:1].isdigit()] == ["1.", "2.", "3."]

    single = await _render_channel_list(await _single(store))
    assert "👤" not in single


async def _single(store):
    await store.conn.execute("DELETE FROM yt_channels WHERE channel_id IN ('UC2', 'UC3')")
    await store.conn.commit()
    return store


# -- the Channels page ----------------------------------------------------------------

async def test_channels_page_groups_cards_under_accounts(store, monkeypatch):
    await add(store, "UC1", "Alpha", email="a@gmail.com", subs=1234)
    await add(store, "UC2", "Beta", email="a@gmail.com")
    await add(store, "UC3", "Gamma", email="b@gmail.com", subs=5)
    await add(store, "UC4", "Legacy <i>")
    monkeypatch.setattr(webapp, "PUBLIC_PATHS", webapp.PUBLIC_PATHS | {"/_login"})
    app = webapp.create_app(store)

    @app.get("/_login")
    async def _login(request: Request):
        request.session.update({webapp.auth.SESSION_USER_ID: 1, webapp.auth.SESSION_USERNAME: "admin",
                                webapp.auth.SESSION_ROLE: "owner"})
        return {}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        await client.get("/_login")
        html = (await client.get("/channels")).text

    assert html.index("a@gmail.com") < html.index("Alpha") < html.index("Beta") < html.index("b@gmail.com") \
        < html.index("Gamma") < html.index(flow.UNRECORDED_ACCOUNT) < html.index("Legacy")
    assert html.count("Add another channel from this account") == 2          # per recorded account, none for legacy
    assert html.count('name="login_hint" value="a@gmail.com"') == 1 and 'value="b@gmail.com"' in html
    assert "1,234" in html and "Subscribers" in html
    assert "Legacy &lt;i&gt;" in html and "Legacy <i>" not in html
    assert "one pool shared by every channel" in html
