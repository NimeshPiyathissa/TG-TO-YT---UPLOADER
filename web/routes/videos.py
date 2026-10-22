"""GET /videos (filter/search) + delete (spec section 6)."""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from web import auth
from web.csrf import require_csrf
from web.flash import flash, pop_flashes
from yt.tokens import NeedsReauth, get_valid_access_token
from yt.oauth import can_delete
from yt.videos import delete_video

log = structlog.get_logger(__name__)
router = APIRouter()


class MissingDeleteScope(Exception):
    """The channel was linked before the delete permission was requested."""


@router.get("/videos")
async def videos_list(request: Request, channel_pk: Optional[int] = None, q: Optional[str] = None):
    templates = request.app.state.templates
    store = request.app.state.store
    videos = await store.list_videos(channel_pk=channel_pk, search=q, limit=200)
    channels = await store.list_yt_channels(include_revoked=True)
    channel_titles = {c["id"]: c["channel_title"] for c in channels}
    return templates.TemplateResponse(request, "videos.html", {
        "videos": videos, "channels": channels, "channel_titles": channel_titles,
        "filter_channel_pk": channel_pk, "q": q or "", "flashes": pop_flashes(request),
    })


@router.post("/videos/{video_id}/delete")
async def delete_video_route(video_id: str, request: Request, confirm: str = Form(""),
                              csrf_ok: None = Depends(require_csrf)):
    store = request.app.state.store
    video = await store.get_video(video_id)
    if video is None:
        flash(request, "Video record not found.", "error")
        return RedirectResponse("/videos", status_code=303)
    if video["deleted_at"] is not None:
        flash(request, "Already deleted.", "info")
        return RedirectResponse("/videos", status_code=303)
    if confirm.strip().lower() != "delete":
        flash(request, "Type DELETE to confirm.", "error")
        return RedirectResponse("/videos", status_code=303)

    user = auth.current_user(request)
    channel = await store.get_yt_channel(video["channel_pk"]) if video["channel_pk"] else None
    try:
        if channel is not None and not can_delete(channel["scopes"]):
            raise MissingDeleteScope()
        access_token = await get_valid_access_token(store, video["channel_pk"])
        await delete_video(access_token, video_id)
    except MissingDeleteScope:
        flash(request, "This channel was linked before deleting was permitted, so YouTube would refuse it. Re-authorize "
                        "the channel on the Channels page (leave every box ticked) and delete again. "
                        "The record is still marked deleted here.", "error")
    except NeedsReauth:
        flash(request, "That channel needs to be re-authorised before this video can be deleted from YouTube. "
                        "The record is still marked deleted here.", "error")
    except Exception:
        log.warning("videos.delete_failed", video_id=video_id, exc_info=True)
        flash(request, "Couldn't reach YouTube to delete it there, but the record is marked deleted here.", "error")

    await store.soft_delete_video(video_id, user["username"] if user else "unknown")
    await store.write_audit("app_user", user["username"] if user else None, "video_deleted", target=video_id)
    flash(request, "Video deleted.", "success")
    return RedirectResponse("/videos", status_code=303)
