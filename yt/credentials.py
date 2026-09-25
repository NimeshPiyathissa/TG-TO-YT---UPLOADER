"""Ingest and validate the two JSON files an operator can upload (see README section 1.1).

``client_secret_*.json`` identifies the API project (uploaded once, in Settings).
``token.json`` is a per-channel refresh token (uploaded once per channel, via the
offline-helper path in Mode B, or produced internally after a completed consent).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


class InvalidCredentialFile(Exception):
    pass


class ServiceAccountRejected(Exception):
    MESSAGE = "Service accounts can't upload to YouTube. Download an OAuth Client ID JSON (Desktop app) instead."

    def __init__(self):
        super().__init__(self.MESSAGE)


@dataclass
class ClientSecret:
    client_id: str
    client_secret: str
    client_type: str  # "web" | "installed"
    project_id: Optional[str]
    redirect_uris: list[str]


def parse_client_secret(raw: bytes) -> ClientSecret:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidCredentialFile(f"Not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise InvalidCredentialFile("Expected a JSON object.")

    if data.get("type") == "service_account":
        raise ServiceAccountRejected()

    key = "web" if "web" in data else ("installed" if "installed" in data else None)
    if key is None:
        raise InvalidCredentialFile(
            "This doesn't look like an OAuth Client ID JSON -- expected a top-level "
            "'web' or 'installed' key. Download it from Google Cloud Console -> "
            "Credentials -> your OAuth client -> Download JSON."
        )

    block = data[key]
    client_id = block.get("client_id")
    client_secret = block.get("client_secret")
    if not client_id or not client_secret:
        raise InvalidCredentialFile("Missing client_id or client_secret in the uploaded JSON.")

    return ClientSecret(
        client_id=client_id,
        client_secret=client_secret,
        client_type=key,
        project_id=block.get("project_id"),
        redirect_uris=block.get("redirect_uris", []),
    )


@dataclass
class OfflineToken:
    refresh_token: str
    client_id: Optional[str]
    client_secret: Optional[str]
    scopes: list[str]


def parse_token_json(raw: bytes) -> OfflineToken:
    """Parses the ``token.json`` written by ``tools/link_channel.py``
    (``google.oauth2.credentials.Credentials.to_json()`` format)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidCredentialFile(f"Not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise InvalidCredentialFile("Expected a JSON object.")

    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise InvalidCredentialFile(
            "This token.json has no refresh_token -- it may have been generated "
            "without offline access. Re-run link_channel.py."
        )

    scopes = data.get("scopes") or []
    return OfflineToken(
        refresh_token=refresh_token,
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=scopes,
    )
