"""Why a channel link can silently 'do nothing', and the evidence we log: the exact (redacted)
authorization request, the scopes Google actually granted, what channels.list returned, and
which channel ID was saved."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import structlog
from fastapi import Request
from structlog.testing import capture_logs

import web.app as webapp
from web.routes import channels as channels_routes
from yt import oauth

UPLOAD, READONLY, DELETE = oauth.SCOPE_UPLOAD, oauth.SCOPE_READONLY, oauth.SCOPE_DELETE
SECRET_STATE, SECRET_CHALLENGE = "STATE-SECRET-123", "CHALLENGE-SECRET-456"


# -- the authorization request ----------------------------------------------------

def test_scopes_cover_upload_channel_info_and_delete_not_just_identity():
    scopes = oauth.SCOPES.split()
    assert {UPLOAD, READONLY, DELETE} <= set(scopes)          # upload, channels.list/videos.list, videos.delete
    assert "openid" in scopes and "email" in scopes           # only for filing channels under an account
    assert oauth.SCOPE_UPLOAD in oauth.REQUIRED_SCOPES and oauth.SCOPE_READONLY in oauth.REQUIRED_SCOPES


def test_auth_request_forces_chooser_never_reuses_a_grant_and_redacts_secrets():
    url = oauth.build_authorization_url("1234567890-abcdef.apps.googleusercontent.com", "https://x.site/oauth/callback",
                                        SECRET_STATE, SECRET_CHALLENGE, login_hint="someone@gmail.com")
    q = parse_qs(urlparse(url).query)
    assert q["prompt"] == ["select_account consent"] and q["access_type"] == ["offline"]
    assert q["include_granted_scopes"] == ["false"]

    shown = oauth.describe_auth_url(url)
    assert shown["prompt"] == "select_account consent" and shown["include_granted_scopes"] == "false"
    assert shown["access_type"] == "offline" and shown["redirect_uri"] == "https://x.site/oauth/callback"
    assert {UPLOAD, READONLY, DELETE} <= set(shown["scopes"])
    assert shown["login_hint"] == "so***@gmail.com"
    text = str(shown)
    for secret in (SECRET_STATE, SECRET_CHALLENGE, "abcdef.apps.googleusercontent.com"):
        assert secret not in text


async def test_link_start_logs_the_redacted_request(store, monkeypatch):
    await store.set_oauth_client("1234567890-abcdef.apps.googleusercontent.com", b"secret", "web", "p", "t")
    monkeypatch.setattr(channels_routes, "crypto", SimpleNamespace(encrypt=lambda s: s.encode(), decrypt=lambda b: b.decode()))
    monkeypatch.setattr(channels_routes.settings, "oauth_mode", "hostname")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=store)), session={})
    with capture_logs() as logs:
        resp = await channels_routes.link_start(request, login_hint="me@gmail.com", csrf_ok=None)
    event = next(e for e in logs if e["event"] == "oauth.auth_request")
    assert event["mode"] == "hostname" and event["prompt"] == "select_account consent"
    assert event["include_granted_scopes"] == "false" and event["login_hint"] == "me***@gmail.com"
    assert DELETE in event["scopes"] and event["state"] == "<redacted>"
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    assert state not in str(event)


# -- the callback -----------------------------------------------------------------------

@pytest.fixture
def google(monkeypatch):
    g = {"scope": oauth.SCOPES, "channels": [("UC_A", "Channel A")], "email": "owner@gmail.com"}
    monkeypatch.setattr(channels_routes, "crypto", SimpleNamespace(encrypt=lambda s: s.encode(), decrypt=lambda b: b.decode()))

    async def exchange(*a, **k):
        return oauth.TokenResponse("at", "rt", 3600, g["scope"], "Bearer")

    async def listing(token):
        return [{"channel_id": c, "title": t, "handle": None, "thumbnail_url": None, "subscriber_count": None}
                for c, t in g["channels"]]

    async def email(token):
        return g["email"]

    monkeypatch.setattr(channels_routes, "exchange_code", exchange)
    monkeypatch.setattr(channels_routes, "list_my_channels", listing)
    monkeypatch.setattr(channels_routes, "fetch_account_email", email)
    return g


async def link(store):
    await store.set_oauth_client("cid", b"secret", "web", "p", "t")
    return await channels_routes.finalize_channel_from_code(
        store, "code", {"code_verifier_enc": b"v", "created_by": "t"}, "https://x/oauth/callback")


async def test_callback_logs_granted_scopes_channels_returned_and_saved_id(store, google):
    google["channels"] = [("UC_A", "Channel A")]
    with capture_logs() as logs:
        result = await link(store)
    by_event = {e["event"]: e for e in logs}
    assert set(by_event["oauth.callback_token"]["granted_scopes"]) >= {UPLOAD, READONLY, DELETE}
    assert by_event["oauth.callback_token"]["missing_required_scopes"] == []
    assert by_event["oauth.channels_returned"]["channels"] == [{"id": "UC_A", "title": "Channel A"}]
    saved = by_event["oauth.channel_saved"]
    assert saved["saved_channel_id"] == "UC_A" and saved["already_linked"] is False
    assert saved["google_email"] == "ow***@gmail.com" and result["can_delete"] is True


async def test_same_channel_returned_again_is_reported_as_nothing_new_and_saves_no_duplicate(store, google):
    first = await link(store)
    with capture_logs() as logs:
        again = await link(store)   # Google offered no chooser: it hands back the channel we already have
    assert again["already_linked"] is True and again["channel_pk"] == first["channel_pk"]
    assert len(await store.list_yt_channels()) == 1
    assert next(e for e in logs if e["event"] == "oauth.channel_saved")["already_linked"] is True
    message = channels_routes.link_result_message(again)
    assert message.startswith("No new channel was added") and "owns" in message


async def test_a_different_channel_from_the_same_account_is_a_second_row(store, google):
    await link(store)
    google["channels"] = [("UC_B", "Channel B")]
    second = await link(store)
    assert second["already_linked"] is False
    assert [r["channel_id"] for r in await store.list_yt_channels()] == ["UC_A", "UC_B"]
    assert channels_routes.link_result_message(second).startswith("Linked new channel: Channel B")


async def test_missing_required_youtube_scope_refuses_the_link(store, google):
    google["scope"] = f"{READONLY} openid email"   # upload permission unticked on Google's consent screen
    with capture_logs() as logs:
        with pytest.raises(RuntimeError, match="youtube.upload"):
            await link(store)
    assert await store.list_yt_channels() == []
    assert next(e for e in logs if e["event"] == "oauth.callback_token")["missing_required_scopes"] == [UPLOAD]


async def test_identity_only_grant_is_refused(store, google):
    google["scope"] = "openid email https://www.googleapis.com/auth/userinfo.email"
    with pytest.raises(RuntimeError, match="didn't grant"):
        await link(store)


async def test_link_without_delete_scope_still_works_but_says_deleting_wont(store, google):
    google["scope"] = f"{UPLOAD} {READONLY} openid email"
    result = await link(store)
    assert result["can_delete"] is False
    assert "Deleting videos from YouTube won't work" in channels_routes.link_result_message(result)
    google["scope"] = f"{UPLOAD} {READONLY} {oauth.SCOPE_FULL}"   # the broader `youtube` scope also allows delete
    assert (await link(store))["can_delete"] is True


def test_scope_gap_helpers():
    assert oauth.scope_gaps(f"{UPLOAD} {READONLY}") == ([], False)
    assert oauth.scope_gaps(f"{UPLOAD} {READONLY} {DELETE}") == ([], True)
    assert oauth.scope_gaps([UPLOAD]) == ([READONLY], False)
    assert oauth.scope_gaps(None) == ([UPLOAD, READONLY], False)
    assert oauth.can_delete(DELETE) and not oauth.can_delete(UPLOAD)


async def test_newly_linked_channels_appear_everywhere_without_a_restart(store, google):
    from bot import flow
    from bot.handlers.channels import _render_channel_list

    await link(store)
    assert await store.count_channels() == 1                        # dashboard "Linked channels"
    google["channels"], google["email"] = [("UC_B", "Channel B")], "other@gmail.com"
    await link(store)

    assert await store.count_channels() == 2
    listing = await _render_channel_list(store)                      # /channels and the MY CHANNELS button
    assert "Channel A" in listing and "Channel B" in listing and "other@gmail.com" in listing
    picker = [row[0].text for row in flow.channel_picker_keyboard(await store.list_yt_channels(), 1).inline_keyboard]
    assert "📺 Channel B" in picker                                  # the bot's picker, same process, no restart


# -- the panel ---------------------------------------------------------------------------

async def test_channels_page_flags_a_channel_that_cannot_delete_yet(store, monkeypatch):
    old = await store.upsert_yt_channel("UC_OLD", "Old channel", None, None, b"r", b"a",
                                        "2099-01-01T00:00:00+00:00", f"{UPLOAD} {READONLY}", "t")
    await store.upsert_yt_channel("UC_NEW", "New channel", None, None, b"r", b"a",
                                  "2099-01-01T00:00:00+00:00", oauth.SCOPES, "t")
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
        guide = (await client.get("/help/linking-channels")).text
    assert html.count("Deleting videos from YouTube needs a newer permission") == 1
    assert "can't manage via YouTube APIs" in guide and "Which Google account can link which channel" in guide
    assert old  # silence unused warning; the one flagged card is the old channel
