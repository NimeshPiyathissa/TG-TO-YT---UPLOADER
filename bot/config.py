"""Central configuration. Validates on import and dies loudly on anything missing.

Import this module once, early, from every entrypoint (bot/main.py).

The env file path is resolved from the ``TGYT_ENV_FILE`` environment variable
(set by the systemd unit alongside ``EnvironmentFile=``) so that
``admin_password_blank_in_place`` (see ``db/store.py`` bootstrap in ``bot/main.py``)
knows exactly which file to rewrite. Falls back to ``.env`` for local dev.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import structlog
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = structlog.get_logger(__name__)

ENV_FILE = os.environ.get("TGYT_ENV_FILE", ".env")

# Fixed internal port uvicorn binds to (127.0.0.1 only) when OAUTH_MODE=hostname
# and nginx is terminating TLS in front of it. Not configurable -- nginx.conf
# hardcodes this same value. In loopback mode uvicorn binds WEB_PORT directly
# with a self-signed cert instead, and this constant is unused.
INTERNAL_WEB_PROXY_PORT = 8001


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # Telegram
    api_id: int
    api_hash: str
    bot_token: str
    user_session: Optional[str] = None
    relay_chat_id: Optional[int] = None   # MTProto relay channel, see README; needed for >20MB downloads
    owner_tg_id: int

    # OAuth / networking
    oauth_mode: str = "hostname"          # hostname | loopback
    public_ip: str = ""                   # auto-detected at install, informational
    public_host: str = ""                 # e.g. 20-1-2-3.sslip.io, derived in hostname mode
    loopback_port: int = 8721
    web_port: int = 443
    project_audited: bool = False
    yt_region_code: str = "US"

    # Web admin
    admin_username: str = "admin"
    admin_password: str = ""              # plaintext only until first-boot hash; blanked after
    require_totp: bool = False
    session_secret: str

    # Crypto & paths
    fernet_key_path: str = "/etc/tgyt/fernet.key"
    jobs_dir: str = "/var/lib/tgyt/jobs"
    db_path: str = "/var/lib/tgyt/tgyt.db"

    # Limits
    max_file_bytes: int = 2_147_483_648
    upload_chunk_bytes: int = 8_388_608
    max_concurrent_downloads: int = 2
    max_concurrent_uploads: int = 1
    default_privacy: str = "private"
    job_retention_hours: int = 24

    log_level: str = "INFO"

    @field_validator("relay_chat_id", mode="before")
    @classmethod
    def _empty_relay_chat_id_is_none(cls, v):
        if v == "" or v is None:
            return None
        return v

    @field_validator("oauth_mode")
    @classmethod
    def _validate_oauth_mode(cls, v: str) -> str:
        if v not in ("hostname", "loopback"):
            raise ValueError("OAUTH_MODE must be 'hostname' or 'loopback'")
        return v

    @field_validator("default_privacy")
    @classmethod
    def _validate_privacy(cls, v: str) -> str:
        if v not in ("private", "unlisted", "public"):
            raise ValueError("DEFAULT_PRIVACY must be private, unlisted, or public")
        return v

    @field_validator("upload_chunk_bytes")
    @classmethod
    def _validate_chunk(cls, v: int) -> int:
        if v % 262_144 != 0:
            raise ValueError("UPLOAD_CHUNK_BYTES must be a multiple of 256 KiB (262144)")
        return v

    @model_validator(mode="after")
    def _validate_oauth_host(self) -> "Settings":
        if self.oauth_mode == "hostname" and not self.public_host:
            log.warning(
                "config.no_public_host",
                msg="OAUTH_MODE=hostname but PUBLIC_HOST is empty; the installer "
                    "should have derived it from PUBLIC_IP. Channel linking will fail "
                    "until this is set.",
            )
        if not self.user_session:
            log.warning(
                "config.no_large_file_backend",
                msg="USER_SESSION is not set; files over 20 MB will be rejected with a clear error.",
            )
        if self.user_session and not self.relay_chat_id:
            log.warning(
                "config.no_relay_chat",
                msg="USER_SESSION is set but RELAY_CHAT_ID is not; the MTProto "
                    "relay path for >20MB downloads cannot run without it.",
            )
        return self

    @property
    def redirect_uri(self) -> str:
        """The exact Authorized redirect URI to register in Google Cloud Console.

        Must match what nginx (hostname mode) actually listens on -- omitting a
        non-default port here silently breaks the post-consent redirect (the
        browser tries the implicit port 443, nothing answers, and the callback
        that finalizes the channel link never runs).
        """
        if self.oauth_mode == "hostname":
            port = "" if self.web_port == 443 else f":{self.web_port}"
            return f"https://{self.public_host}{port}/oauth/callback"
        return f"http://127.0.0.1:{self.loopback_port}/oauth/callback"

    @property
    def ui_base_url(self) -> str:
        """The panel's own base URL, for building links to itself (e.g. the
        bot's /addyt message). Same port-omission rule as redirect_uri."""
        if self.oauth_mode == "hostname":
            host = self.public_host or self.public_ip
            port = "" if self.web_port == 443 else f":{self.web_port}"
            return f"https://{host}{port}"
        port = "" if self.web_port == 443 else f":{self.web_port}"
        return f"https://{self.public_ip}{port}"


def load_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # pydantic ValidationError or similar
        print(f"FATAL: invalid configuration: {exc}", file=sys.stderr)
        raise SystemExit(1)


settings = load_settings()
