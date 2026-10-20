"""Test bootstrap: bot.config validates env vars on import, so give it dummies
before anything imports it. Nothing here touches a real Telegram or Google account."""
import os
import sys
from pathlib import Path

import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for key, value in {
    "API_ID": "1", "API_HASH": "0" * 32, "BOT_TOKEN": "1:test", "OWNER_TG_ID": "1",
    "SESSION_SECRET": "test-secret", "TGYT_ENV_FILE": str(ROOT / "tests" / "nonexistent.env"),
}.items():
    os.environ.setdefault(key, value)

from db.store import Store  # noqa: E402  (after the env is set up)


@pytest_asyncio.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    await s.connect()
    yield s
    await s.close()
