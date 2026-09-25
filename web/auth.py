"""Argon2 + optional TOTP login for the admin web UI (spec section 6, section 8).

Lockout is both per-account (``app_users.failed_attempts``/``locked_until``)
and per-IP (``login_throttle``), independently of each other -- 5 attempts,
15-minute lockout either way.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import Request
from starlette.datastructures import Address

from bot import crypto, security
from db.store import Store

MAX_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

SESSION_USER_ID = "app_user_id"
SESSION_USERNAME = "app_username"
SESSION_ROLE = "app_role"


def client_ip(request: Request) -> str:
    # nginx (Mode A) sets X-Forwarded-For; trust it since only nginx can reach
    # the internal uvicorn port (127.0.0.1:8001) in that mode. In loopback mode
    # uvicorn is reached directly, so request.client.host is already correct.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    client: Optional[Address] = request.client
    return client.host if client else "unknown"


def _is_locked(locked_until: Optional[str]) -> bool:
    if not locked_until:
        return False
    return datetime.fromisoformat(locked_until) > datetime.now(timezone.utc)


class LoginResult:
    def __init__(self, ok: bool, reason: str = "", needs_totp: bool = False, user_row=None):
        self.ok = ok
        self.reason = reason
        self.needs_totp = needs_totp
        self.user_row = user_row


async def attempt_login(store: Store, ip: str, username: str, password: str,
                         totp_code: Optional[str], require_totp_default: bool) -> LoginResult:
    ip_row = await store.get_ip_throttle(ip)
    if ip_row and _is_locked(ip_row["locked_until"]):
        return LoginResult(False, "Too many attempts from this network. Try again in a few minutes.")

    user = await store.get_app_user_by_username(username)
    if user is None:
        await store.record_ip_failure(ip, MAX_ATTEMPTS, LOCKOUT_MINUTES)
        return LoginResult(False, "Invalid username or password.")

    if _is_locked(user["locked_until"]):
        return LoginResult(False, "This account is temporarily locked. Try again in a few minutes.")

    if not security.verify_password(user["password_hash"], password):
        await store.record_login_failure(username, MAX_ATTEMPTS, LOCKOUT_MINUTES)
        await store.record_ip_failure(ip, MAX_ATTEMPTS, LOCKOUT_MINUTES)
        return LoginResult(False, "Invalid username or password.")

    needs_totp = require_totp_default or user["totp_secret_enc"] is not None
    if needs_totp:
        if not user["totp_secret_enc"]:
            return LoginResult(False, "TOTP is required but not set up for this account yet. Contact the owner.")
        if not totp_code:
            return LoginResult(False, "", needs_totp=True, user_row=user)
        secret = crypto.decrypt(user["totp_secret_enc"])
        if not security.verify_totp(secret, totp_code):
            await store.record_login_failure(username, MAX_ATTEMPTS, LOCKOUT_MINUTES)
            await store.record_ip_failure(ip, MAX_ATTEMPTS, LOCKOUT_MINUTES)
            return LoginResult(False, "Invalid authentication code.", needs_totp=True, user_row=user)

    if security.needs_rehash(user["password_hash"]):
        await store.set_app_user_password(user["id"], security.hash_password(password))

    await store.record_login_success(username)
    await store.clear_ip_throttle(ip)
    return LoginResult(True, user_row=user)


def current_user(request: Request) -> Optional[dict]:
    uid = request.session.get(SESSION_USER_ID)
    if uid is None:
        return None
    return {
        "id": uid,
        "username": request.session.get(SESSION_USERNAME),
        "role": request.session.get(SESSION_ROLE),
    }


def log_in_session(request: Request, user_row) -> None:
    request.session[SESSION_USER_ID] = user_row["id"]
    request.session[SESSION_USERNAME] = user_row["username"]
    request.session[SESSION_ROLE] = user_row["role"]


def log_out_session(request: Request) -> None:
    request.session.clear()
