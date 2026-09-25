"""Atomic, permission-preserving rewrites of the env file (mirrors the pattern
`bot/main.py` uses for blanking `ADMIN_PASSWORD` on first boot). Used by
`web/routes/settings.py` to toggle `PROJECT_AUDITED` in place."""
from __future__ import annotations

import os
import re
from pathlib import Path

from bot.config import ENV_FILE


def set_env_value(key: str, value: str) -> bool:
    """Replaces ``KEY=...`` with ``KEY=value`` in the env file. Returns True on
    success. Adds the key at the end if it isn't already present."""
    path = Path(ENV_FILE)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
    if pattern.search(text):
        new_text = pattern.sub(f"{key}={value}", text, count=1)
    else:
        sep = "" if text.endswith("\n") or not text else "\n"
        new_text = f"{text}{sep}{key}={value}\n"
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        st = path.stat()
        tmp.write_text(new_text, encoding="utf-8")
        os.chmod(tmp, st.st_mode)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def read_env_raw() -> str:
    try:
        return Path(ENV_FILE).read_text(encoding="utf-8")
    except OSError:
        return ""


_SENSITIVE_NAME = re.compile(r"(SECRET|TOKEN|PASSWORD|KEY|HASH)", re.IGNORECASE)


def redacted_env_lines() -> list[str]:
    lines = []
    for line in read_env_raw().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(line)
            continue
        key, _, value = line.partition("=")
        if value and _SENSITIVE_NAME.search(key):
            lines.append(f"{key}=***")
        else:
            lines.append(line)
    return lines
