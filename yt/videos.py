"""Cheap metadata calls against the YouTube Data API v3: status checks, delete, channel lookup.

Uses ``google-api-python-client`` (synchronous) wrapped in ``asyncio.to_thread`` --
per spec, the resumable upload itself is hand-rolled raw ``aiohttp`` in
``yt/upload.py``, but these calls are cheap and infrequent enough that the
synchronous client is simpler and safer to get right.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = structlog.get_logger(__name__)


def _client(access_token: str):
    creds = Credentials(token=access_token)
    return build("youtube", "v3", credentials=creds, static_discovery=True, cache_discovery=False)


async def get_video_status(access_token: str, video_id: str) -> dict:
    def _call():
        yt = _client(access_token)
        resp = yt.videos().list(part="status,snippet", id=video_id).execute()
        items = resp.get("items", [])
        if not items:
            return {"privacyStatus": "unknown", "uploadStatus": "unknown", "title": None, "found": False}
        item = items[0]
        return {
            "privacyStatus": item["status"].get("privacyStatus", "unknown"),
            "uploadStatus": item["status"].get("uploadStatus", "unknown"),
            "title": item["snippet"].get("title"),
            "found": True,
        }

    return await asyncio.to_thread(_call)


async def delete_video(access_token: str, video_id: str) -> None:
    def _call():
        yt = _client(access_token)
        try:
            yt.videos().delete(id=video_id).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                log.info("videos.delete_already_gone", video_id=video_id)
                return
            raise

    await asyncio.to_thread(_call)


async def list_my_channels(access_token: str) -> list[dict]:
    def _call():
        yt = _client(access_token)
        resp = yt.channels().list(part="snippet", mine=True).execute()
        out = []
        for item in resp.get("items", []):
            snippet = item["snippet"]
            thumbs = snippet.get("thumbnails", {})
            thumb = thumbs.get("default", {}).get("url") or thumbs.get("medium", {}).get("url")
            out.append({
                "channel_id": item["id"],
                "title": snippet.get("title", "Untitled channel"),
                "handle": snippet.get("customUrl"),
                "thumbnail_url": thumb,
            })
        return out

    return await asyncio.to_thread(_call)


async def set_thumbnail(access_token: str, video_id: str, image_path: str) -> None:
    def _call():
        yt = _client(access_token)
        from googleapiclient.http import MediaFileUpload

        yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(image_path)).execute()

    await asyncio.to_thread(_call)


async def add_to_playlist(access_token: str, playlist_id: str, video_id: str) -> None:
    def _call():
        yt = _client(access_token)
        yt.playlistItems().insert(
            part="snippet",
            body={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
        ).execute()

    await asyncio.to_thread(_call)
