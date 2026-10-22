"""Google OAuth: PKCE authorization-code flow, both networking modes.

Mode A (``hostname``): a normal HTTPS route (``web/routes/channels.py``'s
``/oauth/callback``) receives the redirect directly -- no listener needed here.

Mode B (``loopback``): Google only allows ``http://127.0.0.1:<port>/...`` as a
redirect URI for Desktop-app clients, and that redirect lands in whatever
browser the operator used to open the consent screen -- which, over an SSH
``-L`` tunnel, forwards straight back to a listener on this VPS. ``run_loopback_flow``
owns spinning that listener up, waiting for the one callback, and tearing it down.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import aiohttp
import structlog

log = structlog.get_logger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"      # videos.insert (the upload itself)
SCOPE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"  # channels.list(mine), videos.list verification
# videos.delete accepts only youtube / youtube.force-ssl / youtubepartner (NOT upload or readonly), so
# deleting a video from the bot or the panel needs this narrower-than-`youtube` scope.
SCOPE_DELETE = "https://www.googleapis.com/auth/youtube.force-ssl"
SCOPE_FULL = "https://www.googleapis.com/auth/youtube"
REQUIRED_SCOPES = (SCOPE_UPLOAD, SCOPE_READONLY)
# openid + email are only used to record which Google account owns a channel (for grouping); they are
# non-sensitive scopes and grant no extra YouTube access.
SCOPES = f"{SCOPE_UPLOAD} {SCOPE_READONLY} {SCOPE_DELETE} openid email"

_HUMAN_ERRORS = {
    "redirect_uri_mismatch": "The redirect URI Google received doesn't match what's registered for this OAuth client.",
    "invalid_client": "The OAuth client ID/secret is wrong or was deleted in Google Cloud Console.",
    "access_denied": "Consent was denied or cancelled.",
    "invalid_grant": "That authorization code was already used or has expired -- start linking again.",
}


class OAuthError(Exception):
    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: Optional[str]
    expires_in: int
    scope: str
    token_type: str


def scope_gaps(granted: "str | list[str] | None") -> tuple[list[str], bool]:
    """(required scopes that are missing, whether deleting videos is possible) for a grant.
    Google's consent screen lets people untick individual permissions, so never assume."""
    have = set(granted.split() if isinstance(granted, str) else (granted or []))
    missing = [s for s in REQUIRED_SCOPES if s not in have]
    return missing, bool(have & {SCOPE_DELETE, SCOPE_FULL})


def can_delete(granted: "str | list[str] | None") -> bool:
    return scope_gaps(granted)[1]


def _mask(value: Optional[str], keep: int = 4) -> Optional[str]:
    if not value:
        return None
    return value[:keep] + "…"


def mask_email(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return f"{local[:2]}***@{domain}"


def describe_auth_url(url: str) -> dict:
    """The parts of an authorization URL that matter for diagnosing a link, with secrets
    (client id, state, PKCE challenge) redacted. This is what the link flow logs."""
    from urllib.parse import parse_qs, urlparse

    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    return {
        "scopes": q.get("scope", "").split(),
        "prompt": q.get("prompt"),
        "access_type": q.get("access_type"),
        "include_granted_scopes": q.get("include_granted_scopes"),  # None = not sent (Google's default: off)
        "login_hint": mask_email(q.get("login_hint")),
        "redirect_uri": q.get("redirect_uri"),
        "client_id": _mask(q.get("client_id"), 8),
        "response_type": q.get("response_type"),
        "pkce": q.get("code_challenge_method"),
        "state": "<redacted>" if q.get("state") else None,
    }


def generate_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorization_url(client_id: str, redirect_uri: str, state: str, code_challenge: str,
                            login_hint: Optional[str] = None) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "include_granted_scopes": "false",  # explicit: never fold in an earlier grant
        # select_account forces Google's account/channel chooser every time (consent alone lets it
        # silently reuse the last identity, which links the wrong Brand channel); consent keeps the
        # refresh token coming back.
        "prompt": "select_account consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        # Pre-selects that Google account so Google jumps straight to its channel chooser.
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(client_id: str, client_secret: str, code: str, code_verifier: str,
                         redirect_uri: str) -> TokenResponse:
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, data=data) as resp:
            body = await resp.json()
            if resp.status != 200:
                err = body.get("error", "unknown_error")
                raise OAuthError(_HUMAN_ERRORS.get(err, f"Google rejected the exchange: {err}"), code=err)
            return TokenResponse(
                access_token=body["access_token"],
                refresh_token=body.get("refresh_token"),
                expires_in=body.get("expires_in", 3600),
                scope=body.get("scope", SCOPES),
                token_type=body.get("token_type", "Bearer"),
            )


async def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> TokenResponse:
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, data=data) as resp:
            body = await resp.json()
            if resp.status != 200:
                err = body.get("error", "unknown_error")
                raise OAuthError(_HUMAN_ERRORS.get(err, f"Google rejected the refresh: {err}"), code=err)
            return TokenResponse(
                access_token=body["access_token"],
                refresh_token=body.get("refresh_token"),  # usually absent on refresh
                expires_in=body.get("expires_in", 3600),
                scope=body.get("scope", SCOPES),
                token_type=body.get("token_type", "Bearer"),
            )


async def fetch_account_email(access_token: str) -> Optional[str]:
    """Google account email for this authorization, or None if the email scope wasn't granted
    (or the call fails). Never raises: a missing email must not block linking a channel."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}) as resp:
                if resp.status != 200:
                    return None
                email = (await resp.json()).get("email")
    except Exception as exc:
        log.warning("oauth.userinfo_failed", error=str(exc))
        return None
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


async def revoke_token(token: str) -> None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(REVOKE_URL, data={"token": token}) as resp:
                if resp.status not in (200, 400):  # 400 = already invalid, treat as done
                    log.warning("oauth.revoke_unexpected_status", status=resp.status)
    except Exception as exc:  # best-effort -- the DB row is removed either way
        log.warning("oauth.revoke_failed", error=str(exc))


_CALLBACK_HTML = """<!doctype html><html><head><title>TG-YT Uploader</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{text-align:center}}</style></head>
<body><div class="card"><h2>{heading}</h2><p>{body}</p></div></body></html>"""


class LoopbackCallbackServer:
    """One-shot HTTP listener on 127.0.0.1:<port> for Mode B's Desktop-app redirect."""

    def __init__(self, port: int):
        self.port = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._future: "asyncio.Future[tuple[str, str]]" = asyncio.get_event_loop().create_future()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            # Drain headers.
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if line in (b"\r\n", b"\n", b""):
                    break
            try:
                path = request_line.decode("latin1").split(" ", 2)[1]
            except (IndexError, UnicodeDecodeError):
                path = ""
            from urllib.parse import urlparse, parse_qs

            qs = parse_qs(urlparse(path).query)
            code = qs.get("code", [None])[0]
            state = qs.get("state", [None])[0]
            error = qs.get("error", [None])[0]
            if error:
                html = _CALLBACK_HTML.format(heading="Linking cancelled", body=f"Google reported: {error}. You can close this tab.")
            elif code and state:
                html = _CALLBACK_HTML.format(heading="Channel linked", body="You can close this tab and return to the panel.")
            else:
                html = _CALLBACK_HTML.format(heading="Something went wrong", body="No authorization code received. You can close this tab.")
            body = html.encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
            if not self._future.done():
                if error:
                    self._future.set_exception(OAuthError(_HUMAN_ERRORS.get(error, error), code=error))
                elif code and state:
                    self._future.set_result((code, state))
        except Exception as exc:  # pragma: no cover - defensive, single best-effort request
            if not self._future.done():
                self._future.set_exception(exc)
        finally:
            writer.close()

    async def wait_for_callback(self, timeout: int = 300) -> tuple[str, str]:
        try:
            return await asyncio.wait_for(self._future, timeout=timeout)
        except asyncio.TimeoutError:
            raise OAuthError("Timed out waiting for the Google redirect. Try linking again.")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
