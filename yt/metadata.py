"""Video category cache and ffprobe-based file inspection."""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import structlog

from db.store import Store

log = structlog.get_logger(__name__)

DEFAULT_CATEGORIES = [("22", "People & Blogs"), ("24", "Entertainment"), ("27", "Education")]


async def get_categories(store: Store, access_token: str, region_code: str) -> list[dict]:
    cached = await store.get_cached_categories(region_code)
    if cached:
        return [{"id": r["category_id"], "title": r["title"]} for r in cached]

    def _call():
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        yt = build("youtube", "v3", credentials=Credentials(token=access_token), static_discovery=True, cache_discovery=False)
        resp = yt.videoCategories().list(part="snippet", regionCode=region_code).execute()
        return [
            (item["id"], item["snippet"]["title"])
            for item in resp.get("items", [])
            if item["snippet"].get("assignable", True)
        ]

    try:
        categories = await asyncio.to_thread(_call)
        if not categories:
            categories = DEFAULT_CATEGORIES
    except Exception as exc:
        log.warning("metadata.categories_fetch_failed", error=str(exc))
        categories = DEFAULT_CATEGORIES

    await store.replace_category_cache(region_code, categories)
    return [{"id": cid, "title": title} for cid, title in categories]


async def probe_video(local_path: str) -> dict:
    """Runs ffprobe and returns duration/width/height/mime. Never raises --
    a probe failure just means the job proceeds with unknown values."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", local_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            log.warning("metadata.ffprobe_failed", stderr=stderr.decode(errors="replace")[:500])
            return {"duration_s": None, "width": None, "height": None, "mime": None, "is_video": False}
        data = json.loads(stdout)
        fmt = data.get("format", {})
        video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        duration = float(fmt.get("duration")) if fmt.get("duration") else None
        return {
            "duration_s": duration,
            "width": video_stream.get("width") if video_stream else None,
            "height": video_stream.get("height") if video_stream else None,
            "mime": "video/mp4",
            "is_video": video_stream is not None,
        }
    except Exception as exc:
        log.warning("metadata.probe_error", error=str(exc))
        return {"duration_s": None, "width": None, "height": None, "mime": None, "is_video": False}
