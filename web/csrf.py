"""Session-bound CSRF token: one per session, embedded as a hidden field in
every mutating form, checked by `require_csrf` on every POST/PUT/DELETE route."""
from __future__ import annotations

import secrets

from fastapi import Form, HTTPException, Request


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


async def require_csrf(request: Request, csrf_token: str = Form(...)) -> None:
    expected = request.session.get("csrf_token")
    if not expected or not secrets.compare_digest(expected, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token mismatch. Reload the page and try again.")
