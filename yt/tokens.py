"""Access-token lifecycle for linked channels: lazy refresh, invalid_grant handling.

Every function here takes the shared ``Store`` instance so it can persist the
refreshed (encrypted) token and flip a channel to ``needs_reauth`` in one place --
this is the only module allowed to write ``yt_channels.access_token_enc`` /
``status`` outside of the initial link.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from bot import crypto
from db.store import Store
from yt.oauth import OAuthError, refresh_access_token

log = structlog.get_logger(__name__)

_EXPIRY_SKEW = timedelta(minutes=2)


class NeedsReauth(Exception):
    def __init__(self, channel_pk: int, channel_title: str = ""):
        super().__init__(f"channel {channel_pk} needs re-authorization")
        self.channel_pk = channel_pk
        self.channel_title = channel_title


async def _client_secret(store: Store) -> tuple[str, str]:
    row = await store.get_oauth_client()
    if row is None:
        raise RuntimeError("No OAuth client configured yet -- upload client_secret.json in Settings first.")
    return row["client_id"], crypto.decrypt(row["client_secret_enc"])


async def _do_refresh(store: Store, channel_row) -> str:
    client_id, client_secret = await _client_secret(store)
    refresh_token = crypto.decrypt(channel_row["refresh_token_enc"])
    try:
        tok = await refresh_access_token(client_id, client_secret, refresh_token)
    except OAuthError as exc:
        if exc.code == "invalid_grant":
            await store.set_channel_status(channel_row["id"], "needs_reauth")
            log.warning("tokens.needs_reauth", channel_pk=channel_row["id"])
            raise NeedsReauth(channel_row["id"], channel_row["channel_title"]) from exc
        raise
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=tok.expires_in)).isoformat()
    new_refresh_enc = crypto.encrypt(tok.refresh_token) if tok.refresh_token else None
    await store.update_channel_tokens(
        channel_row["id"], crypto.encrypt(tok.access_token), expires_at, refresh_token_enc=new_refresh_enc
    )
    return tok.access_token


async def get_valid_access_token(store: Store, channel_pk: int) -> str:
    row = await store.get_yt_channel(channel_pk)
    if row is None:
        raise ValueError(f"channel {channel_pk} not found")
    if row["status"] == "needs_reauth":
        raise NeedsReauth(channel_pk, row["channel_title"])
    if row["access_token_enc"] and row["access_expires_at"]:
        expires_at = datetime.fromisoformat(row["access_expires_at"])
        if expires_at - _EXPIRY_SKEW > datetime.now(timezone.utc):
            return crypto.decrypt(row["access_token_enc"])
    return await _do_refresh(store, row)


async def refresh_now(store: Store, channel_pk: int) -> str:
    row = await store.get_yt_channel(channel_pk)
    if row is None:
        raise ValueError(f"channel {channel_pk} not found")
    return await _do_refresh(store, row)
