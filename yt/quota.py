"""Dual-bucket quota ledger (see README / spec section 1.3 and 9).

``videos.insert`` and ``search.list`` each get their own 100-calls/day (1 unit
each) bucket; everything else shares the 10,000-units/day pool. All buckets
reset at midnight America/Los_Angeles -- ``db.store.pt_date()`` is the single
source of truth for "which day" a call belongs to.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from db.store import Store, pt_date

log = structlog.get_logger(__name__)

BUCKET_LIMITS = {"uploads": 100, "search": 100, "general": 10_000}

# Shared-pool costs for the calls this app makes outside the two dedicated buckets.
COSTS = {
    "thumbnails.set": 50,
    "playlistItems.insert": 50,
    "videos.delete": 50,
    "videos.update": 50,
    "videos.list": 1,
    "channels.list": 1,
    "videoCategories.list": 1,
}


class QuotaExceeded(Exception):
    def __init__(self, bucket: str, reset_in_seconds: int):
        hours = reset_in_seconds / 3600
        super().__init__(f"{bucket} quota exhausted; resets in {hours:.1f}h")
        self.bucket = bucket
        self.reset_in_seconds = reset_in_seconds


def seconds_until_pt_midnight() -> int:
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Los_Angeles")
    except Exception:  # pragma: no cover
        tz = None
    now = datetime.now(tz)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


async def remaining(store: Store, bucket: str) -> int:
    used = await store.quota_used(bucket)
    return max(0, BUCKET_LIMITS[bucket] - used)


async def check_and_spend(store: Store, bucket: str, units: int = 1) -> None:
    used = await store.quota_used(bucket)
    limit = BUCKET_LIMITS[bucket]
    if used + units > limit:
        raise QuotaExceeded(bucket, seconds_until_pt_midnight())
    await store.quota_spend(bucket, units)


async def spend_call(store: Store, call_name: str) -> None:
    """Convenience for the shared-pool 'general' bucket, keyed by API call name."""
    await check_and_spend(store, "general", COSTS.get(call_name, 1))
