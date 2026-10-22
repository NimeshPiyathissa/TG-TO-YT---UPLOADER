"""Async data-access layer over SQLite (WAL). One connection per process.

All timestamps are stored as ISO-8601 UTC strings (``datetime.now(timezone.utc).isoformat()``).
The job state machine is enforced in exactly one place: :func:`Store.transition_job`.
"""
from __future__ import annotations

import json
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Job state machine (spec section 4)
# ---------------------------------------------------------------------------

JOB_TERMINAL_STATES = {"DONE", "FAILED", "CANCELLED"}

JOB_TRANSITIONS: dict[str, set[str]] = {
    "QUEUED": {"DOWNLOADING", "FAILED", "CANCELLED"},
    "DOWNLOADING": {"AWAITING_CHANNEL", "FAILED", "CANCELLED"},
    "AWAITING_CHANNEL": {"AWAITING_CAPTION", "FAILED", "CANCELLED"},
    "AWAITING_CAPTION": {"READY", "FAILED", "CANCELLED"},
    "READY": {"UPLOADING", "FAILED", "CANCELLED"},
    "UPLOADING": {"VERIFYING", "FAILED", "CANCELLED"},
    "VERIFYING": {"DONE", "FAILED", "CANCELLED"},
    "DONE": set(),
    "FAILED": {"READY", "QUEUED"},   # retry re-enters the pipeline
    "CANCELLED": set(),
}


class IllegalTransition(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pt_date() -> str:
    """Current date in America/Los_Angeles, used for quota bucket keys."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:  # pragma: no cover - tzdata always present on target host
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def new_token(n: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


@dataclass
class Job:
    id: int
    tg_user_id: int
    channel_pk: Optional[int]
    chat_id: int
    status_message_id: Optional[int]
    tg_file_id: Optional[str]
    tg_file_unique_id: Optional[str]
    source_chat_id: Optional[int]
    source_message_id: Optional[int]
    file_name: Optional[str]
    size_bytes: Optional[int]
    duration_s: Optional[float]
    width: Optional[int]
    height: Optional[int]
    mime: Optional[str]
    local_path: Optional[str]
    caption: Optional[str]
    title: Optional[str]
    description: Optional[str]
    privacy: str
    category_id: Optional[str]
    made_for_kids: int
    state: str
    upload_session_uri: Optional[str]
    bytes_sent: int
    video_id: Optional[str]
    attempts: int
    last_error: Optional[str]
    lease_owner: Optional[str]
    lease_expires_at: Optional[str]
    queued_at: str
    download_started_at: Optional[str]
    download_finished_at: Optional[str]
    upload_started_at: Optional[str]
    finished_at: Optional[str]

    @staticmethod
    def from_row(row: aiosqlite.Row) -> "Job":
        return Job(**dict(row))


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        schema_path = Path(__file__).parent / "schema.sql"
        await self._conn.executescript(schema_path.read_text())
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Additive column changes for databases created before the column existed
        (schema.sql only has CREATE TABLE IF NOT EXISTS, which never alters a table)."""
        cur = await self._conn.execute("PRAGMA table_info(yt_channels)")
        columns = {row["name"] for row in await cur.fetchall()}
        for column, ddl in (("display_label", "TEXT"), ("google_email", "TEXT"), ("subscriber_count", "INTEGER")):
            if column not in columns:
                await self._conn.execute(f"ALTER TABLE yt_channels ADD COLUMN {column} {ddl}")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Store.connect() not called"
        return self._conn

    # -- app_users (web admin logins) --------------------------------------

    async def count_app_users(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS n FROM app_users")
        return (await cur.fetchone())["n"]

    async def create_app_user(self, username: str, password_hash: str, role: str = "owner") -> int:
        cur = await self.conn.execute(
            """INSERT INTO app_users (username, password_hash, role, created_at)
               VALUES (?, ?, ?, ?)""",
            (username, password_hash, role, now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_app_user_by_username(self, username: str) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM app_users WHERE username = ?", (username,))
        return await cur.fetchone()

    async def get_app_user(self, user_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM app_users WHERE id = ?", (user_id,))
        return await cur.fetchone()

    async def list_app_users(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM app_users ORDER BY id")
        return list(await cur.fetchall())

    async def record_login_success(self, username: str) -> None:
        await self.conn.execute(
            "UPDATE app_users SET last_login_at = ?, failed_attempts = 0, locked_until = NULL WHERE username = ?",
            (now(), username),
        )
        await self.conn.commit()

    async def record_login_failure(self, username: str, max_attempts: int, lockout_minutes: int) -> None:
        row = await self.get_app_user_by_username(username)
        if row is None:
            return
        attempts = row["failed_attempts"] + 1
        locked_until = None
        if attempts >= max_attempts:
            locked_until = (datetime.now(timezone.utc) + timedelta(minutes=lockout_minutes)).isoformat()
        await self.conn.execute(
            "UPDATE app_users SET failed_attempts = ?, locked_until = ? WHERE username = ?",
            (attempts, locked_until, username),
        )
        await self.conn.commit()

    async def set_totp_secret(self, user_id: int, secret_enc: Optional[bytes]) -> None:
        await self.conn.execute("UPDATE app_users SET totp_secret_enc = ? WHERE id = ?", (secret_enc, user_id))
        await self.conn.commit()

    async def set_app_user_password(self, user_id: int, password_hash: str) -> None:
        await self.conn.execute("UPDATE app_users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        await self.conn.commit()

    # -- login_throttle (per-IP) --------------------------------------------

    async def get_ip_throttle(self, ip: str) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM login_throttle WHERE ip = ?", (ip,))
        return await cur.fetchone()

    async def record_ip_failure(self, ip: str, max_attempts: int, lockout_minutes: int) -> None:
        row = await self.get_ip_throttle(ip)
        attempts = (row["failed_attempts"] if row else 0) + 1
        locked_until = None
        if attempts >= max_attempts:
            locked_until = (datetime.now(timezone.utc) + timedelta(minutes=lockout_minutes)).isoformat()
        await self.conn.execute(
            """INSERT INTO login_throttle (ip, failed_attempts, locked_until, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(ip) DO UPDATE SET
                   failed_attempts = excluded.failed_attempts,
                   locked_until = excluded.locked_until,
                   updated_at = excluded.updated_at""",
            (ip, attempts, locked_until, now()),
        )
        await self.conn.commit()

    async def clear_ip_throttle(self, ip: str) -> None:
        await self.conn.execute(
            """INSERT INTO login_throttle (ip, failed_attempts, locked_until, updated_at)
               VALUES (?, 0, NULL, ?)
               ON CONFLICT(ip) DO UPDATE SET failed_attempts = 0, locked_until = NULL, updated_at = excluded.updated_at""",
            (ip, now()),
        )
        await self.conn.commit()

    # -- tg_users (Telegram access control) ---------------------------------

    async def get_tg_user(self, tg_user_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM tg_users WHERE tg_user_id = ?", (tg_user_id,))
        return await cur.fetchone()

    async def ensure_tg_user(self, tg_user_id: int, username: Optional[str], first_name: Optional[str],
                              default_role: str = "blocked") -> aiosqlite.Row:
        existing = await self.get_tg_user(tg_user_id)
        if existing is not None:
            await self.conn.execute(
                "UPDATE tg_users SET username = ?, first_name = ?, last_seen_at = ? WHERE tg_user_id = ?",
                (username, first_name, now(), tg_user_id),
            )
            await self.conn.commit()
            return await self.get_tg_user(tg_user_id)  # type: ignore[return-value]
        await self.conn.execute(
            """INSERT INTO tg_users (tg_user_id, username, first_name, role, added_by, added_at, last_seen_at)
               VALUES (?, ?, ?, ?, 'system', ?, ?)""",
            (tg_user_id, username, first_name, default_role, now(), now()),
        )
        await self.conn.commit()
        return await self.get_tg_user(tg_user_id)  # type: ignore[return-value]

    async def set_tg_user_role(self, tg_user_id: int, role: str, added_by: Optional[str] = None) -> None:
        await self.conn.execute(
            "UPDATE tg_users SET role = ?, added_by = COALESCE(?, added_by) WHERE tg_user_id = ?",
            (role, added_by, tg_user_id),
        )
        await self.conn.commit()

    async def list_tg_users(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM tg_users ORDER BY added_at DESC")
        return list(await cur.fetchall())

    async def increment_tg_user_uploads(self, tg_user_id: int) -> None:
        await self.conn.execute(
            "UPDATE tg_users SET uploads_count = uploads_count + 1 WHERE tg_user_id = ?", (tg_user_id,)
        )
        await self.conn.commit()

    # -- oauth_client (single row, id=1) -------------------------------------

    async def get_oauth_client(self) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM oauth_client WHERE id = 1")
        return await cur.fetchone()

    async def set_oauth_client(self, client_id: str, client_secret_enc: bytes, client_type: str,
                                project_id: Optional[str], uploaded_by: Optional[str]) -> None:
        await self.conn.execute(
            """INSERT INTO oauth_client (id, client_id, client_secret_enc, client_type, project_id, uploaded_at, uploaded_by)
               VALUES (1, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   client_id = excluded.client_id, client_secret_enc = excluded.client_secret_enc,
                   client_type = excluded.client_type, project_id = excluded.project_id,
                   uploaded_at = excluded.uploaded_at, uploaded_by = excluded.uploaded_by""",
            (client_id, client_secret_enc, client_type, project_id, now(), uploaded_by),
        )
        await self.conn.commit()

    # -- oauth_states (PKCE state, TTL) --------------------------------------

    async def create_oauth_state(self, created_by: str, mode: str, code_verifier_enc: bytes,
                                  ttl_minutes: int = 10) -> str:
        state = new_token(32)
        start = datetime.now(timezone.utc)
        await self.conn.execute(
            """INSERT INTO oauth_states (state, created_by, mode, code_verifier_enc, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (state, created_by, mode, code_verifier_enc, start.isoformat(),
             (start + timedelta(minutes=ttl_minutes)).isoformat()),
        )
        await self.conn.commit()
        return state

    async def consume_oauth_state(self, state: str) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM oauth_states WHERE state = ?", (state,))
        row = await cur.fetchone()
        if row is None or row["consumed_at"] is not None:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            return None
        await self.conn.execute("UPDATE oauth_states SET consumed_at = ? WHERE state = ?", (now(), state))
        await self.conn.commit()
        return row

    async def purge_expired_oauth_states(self) -> None:
        await self.conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now(),))
        await self.conn.commit()

    # -- yt_channels ----------------------------------------------------------

    async def upsert_yt_channel(
        self, channel_id: str, channel_title: str, handle: Optional[str], thumbnail_url: Optional[str],
        refresh_token_enc: bytes, access_token_enc: Optional[bytes], access_expires_at: Optional[str],
        scopes: str, added_by: Optional[str], google_email: Optional[str] = None,
        subscriber_count: Optional[int] = None,
    ) -> int:
        cur = await self.conn.execute("SELECT id FROM yt_channels WHERE channel_id = ?", (channel_id,))
        existing = await cur.fetchone()
        if existing:
            await self.conn.execute(
                """UPDATE yt_channels SET channel_title = ?, handle = ?, thumbnail_url = ?,
                       refresh_token_enc = ?, access_token_enc = ?, access_expires_at = ?,
                       scopes = ?, status = 'active', last_used_at = ?,
                       google_email = COALESCE(?, google_email), subscriber_count = COALESCE(?, subscriber_count)
                   WHERE id = ?""",
                (channel_title, handle, thumbnail_url, refresh_token_enc, access_token_enc,
                 access_expires_at, scopes, now(), google_email, subscriber_count, existing["id"]),
            )
            await self.conn.commit()
            return existing["id"]
        cur = await self.conn.execute("SELECT COUNT(*) AS n FROM yt_channels WHERE status != 'revoked'")
        is_first = (await cur.fetchone())["n"] == 0
        cur = await self.conn.execute(
            """INSERT INTO yt_channels
               (channel_id, channel_title, handle, thumbnail_url, refresh_token_enc, access_token_enc,
                access_expires_at, scopes, status, is_default, added_at, added_by, google_email, subscriber_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)""",
            (channel_id, channel_title, handle, thumbnail_url, refresh_token_enc, access_token_enc,
             access_expires_at, scopes, int(is_first), now(), added_by, google_email, subscriber_count),
        )
        await self.conn.commit()
        return cur.lastrowid

    # channel_title = the operator's label when set, else Google's title; youtube_title is always Google's.
    _CHANNEL_SELECT = (
        "SELECT id, channel_id, COALESCE(NULLIF(display_label, ''), channel_title) AS channel_title, "
        "channel_title AS youtube_title, display_label, handle, thumbnail_url, refresh_token_enc, "
        "access_token_enc, access_expires_at, scopes, status, is_default, added_at, added_by, "
        "last_used_at, uploads_count, google_email, subscriber_count FROM yt_channels"
    )

    async def list_yt_channels(self, include_revoked: bool = False) -> list[aiosqlite.Row]:
        where = "" if include_revoked else " WHERE status != 'revoked'"
        cur = await self.conn.execute(f"{self._CHANNEL_SELECT}{where} ORDER BY id")
        return list(await cur.fetchall())

    async def get_yt_channel(self, channel_pk: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute(f"{self._CHANNEL_SELECT} WHERE id = ?", (channel_pk,))
        return await cur.fetchone()

    async def get_yt_channel_by_channel_id(self, channel_id: str) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute(f"{self._CHANNEL_SELECT} WHERE channel_id = ?", (channel_id,))
        return await cur.fetchone()

    async def get_default_channel(self) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute(f"{self._CHANNEL_SELECT} WHERE status = 'active' AND is_default = 1 LIMIT 1")
        row = await cur.fetchone()
        if row:
            return row
        cur = await self.conn.execute(f"{self._CHANNEL_SELECT} WHERE status = 'active' ORDER BY id LIMIT 1")
        return await cur.fetchone()

    async def set_default_channel(self, channel_pk: int) -> bool:
        """Make an active channel the single default. Returns False (changing nothing) if it
        doesn't exist or isn't active, so a broken channel can't become the default."""
        cur = await self.conn.execute("SELECT 1 FROM yt_channels WHERE id = ? AND status = 'active'", (channel_pk,))
        if await cur.fetchone() is None:
            return False
        await self.conn.execute("UPDATE yt_channels SET is_default = 0")
        await self.conn.execute("UPDATE yt_channels SET is_default = 1 WHERE id = ?", (channel_pk,))
        await self.conn.commit()
        return True

    async def set_channel_label(self, channel_pk: int, label: Optional[str]) -> bool:
        """Set (or clear, with an empty label) the display name used in the bot and panel."""
        label = (label or "").strip()[:60] or None
        cur = await self.conn.execute("UPDATE yt_channels SET display_label = ? WHERE id = ?", (label, channel_pk))
        await self.conn.commit()
        return cur.rowcount > 0

    async def update_channel_tokens(self, channel_pk: int, access_token_enc: bytes, access_expires_at: str,
                                     refresh_token_enc: Optional[bytes] = None) -> None:
        if refresh_token_enc is not None:
            await self.conn.execute(
                """UPDATE yt_channels SET access_token_enc = ?, access_expires_at = ?,
                       refresh_token_enc = ?, status = 'active' WHERE id = ?""",
                (access_token_enc, access_expires_at, refresh_token_enc, channel_pk),
            )
        else:
            await self.conn.execute(
                """UPDATE yt_channels SET access_token_enc = ?, access_expires_at = ?,
                       status = 'active' WHERE id = ?""",
                (access_token_enc, access_expires_at, channel_pk),
            )
        await self.conn.commit()

    async def touch_channel_used(self, channel_pk: int) -> None:
        await self.conn.execute(
            "UPDATE yt_channels SET last_used_at = ?, uploads_count = uploads_count + 1 WHERE id = ?",
            (now(), channel_pk),
        )
        await self.conn.commit()

    async def set_channel_status(self, channel_pk: int, status: str) -> None:
        await self.conn.execute("UPDATE yt_channels SET status = ? WHERE id = ?", (status, channel_pk))
        await self.conn.commit()

    async def delete_channel(self, channel_pk: int) -> None:
        cur = await self.conn.execute("SELECT is_default FROM yt_channels WHERE id = ?", (channel_pk,))
        row = await cur.fetchone()
        await self.conn.execute("DELETE FROM yt_channels WHERE id = ?", (channel_pk,))
        if row and row["is_default"]:
            # Never leave the install with no default: promote the oldest remaining active channel.
            await self.conn.execute(
                """UPDATE yt_channels SET is_default = 1 WHERE id = (
                       SELECT id FROM yt_channels WHERE status = 'active' ORDER BY id LIMIT 1)"""
            )
        await self.conn.commit()

    # -- jobs -------------------------------------------------------------------

    async def create_job(
        self, tg_user_id: int, chat_id: int, *, tg_file_id: Optional[str] = None,
        tg_file_unique_id: Optional[str] = None, source_chat_id: Optional[int] = None,
        source_message_id: Optional[int] = None, file_name: Optional[str] = None,
        size_bytes: Optional[int] = None, mime: Optional[str] = None,
        privacy: str = "private", category_id: Optional[str] = None,
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO jobs (tg_user_id, chat_id, tg_file_id, tg_file_unique_id,
                                  source_chat_id, source_message_id, file_name, size_bytes, mime,
                                  privacy, category_id, made_for_kids, state, bytes_sent, attempts, queued_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'QUEUED', 0, 0, ?)""",
            (tg_user_id, chat_id, tg_file_id, tg_file_unique_id, source_chat_id,
             source_message_id, file_name, size_bytes, mime, privacy, category_id, now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_job(self, job_id: int) -> Optional[Job]:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        return Job.from_row(row) if row else None

    async def list_jobs_for_user(self, tg_user_id: int, limit: int = 10) -> list[Job]:
        cur = await self.conn.execute(
            "SELECT * FROM jobs WHERE tg_user_id = ? ORDER BY queued_at DESC LIMIT ?",
            (tg_user_id, limit),
        )
        rows = await cur.fetchall()
        return [Job.from_row(r) for r in rows]

    async def list_jobs(self, *, state: Optional[str] = None, channel_pk: Optional[int] = None,
                         tg_user_id: Optional[int] = None, limit: int = 100) -> list[Job]:
        clauses, params = [], []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if channel_pk:
            clauses.append("channel_pk = ?")
            params.append(channel_pk)
        if tg_user_id:
            clauses.append("tg_user_id = ?")
            params.append(tg_user_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cur = await self.conn.execute(f"SELECT * FROM jobs {where} ORDER BY queued_at DESC LIMIT ?", params)
        rows = await cur.fetchall()
        return [Job.from_row(r) for r in rows]

    async def list_active_jobs(self) -> list[Job]:
        cur = await self.conn.execute(
            f"SELECT * FROM jobs WHERE state NOT IN ({','.join('?' for _ in JOB_TERMINAL_STATES)}) "
            "ORDER BY queued_at ASC",
            list(JOB_TERMINAL_STATES),
        )
        rows = await cur.fetchall()
        return [Job.from_row(r) for r in rows]

    async def queued_bytes(self) -> int:
        """Total size of jobs accepted but not yet downloading -- disk they will need."""
        cur = await self.conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS n FROM jobs WHERE state = 'QUEUED'"
        )
        row = await cur.fetchone()
        return int(row["n"])

    async def update_job_fields(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [job_id]
        await self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", values)
        await self.conn.commit()

    async def transition_job(self, job_id: int, new_state: str, **extra_fields: Any) -> Job:
        """The single place that enforces the job state machine (see spec section 4)."""
        job = await self.get_job(job_id)
        if job is None:
            raise ValueError(f"job {job_id} not found")
        allowed = JOB_TRANSITIONS.get(job.state, set())
        if new_state not in allowed:
            raise IllegalTransition(f"job {job_id}: {job.state} -> {new_state} not allowed")
        fields = dict(extra_fields)
        fields["state"] = new_state
        if new_state == "DOWNLOADING" and job.download_started_at is None:
            fields.setdefault("download_started_at", now())
        if new_state == "AWAITING_CHANNEL" and job.download_finished_at is None:
            fields.setdefault("download_finished_at", now())
        if new_state == "UPLOADING" and job.upload_started_at is None:
            fields.setdefault("upload_started_at", now())
        if new_state in JOB_TERMINAL_STATES:
            fields.setdefault("finished_at", now())
        await self.update_job_fields(job_id, **fields)
        log.info("job.transition", job_id=job_id, frm=job.state, to=new_state)
        return await self.get_job(job_id)  # type: ignore[return-value]

    async def claim_next_job(self, states: list[str], lease_owner: str, lease_seconds: int = 60) -> Optional[Job]:
        """Atomically claim the oldest job in one of ``states`` with no live lease."""
        n = datetime.now(timezone.utc)
        placeholders = ",".join("?" for _ in states)
        cur = await self.conn.execute(
            f"""SELECT id FROM jobs
                WHERE state IN ({placeholders})
                  AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                ORDER BY queued_at ASC LIMIT 1""",
            (*states, n.isoformat()),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        job_id = row["id"]
        lease_expires = (n + timedelta(seconds=lease_seconds)).isoformat()
        cur = await self.conn.execute(
            f"""UPDATE jobs SET lease_owner = ?, lease_expires_at = ?
                WHERE id = ? AND state IN ({placeholders})
                  AND (lease_expires_at IS NULL OR lease_expires_at < ?)""",
            (lease_owner, lease_expires, job_id, *states, n.isoformat()),
        )
        await self.conn.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_job(job_id)

    async def heartbeat_lease(self, job_id: int, lease_owner: str, lease_seconds: int = 60) -> None:
        lease_expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        await self.conn.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ? AND lease_owner = ?",
            (lease_expires, job_id, lease_owner),
        )
        await self.conn.commit()

    async def delay_reclaim(self, job_id: int, delay_seconds: int) -> None:
        until = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        await self.conn.execute(
            "UPDATE jobs SET lease_owner = NULL, lease_expires_at = ? WHERE id = ?", (until, job_id)
        )
        await self.conn.commit()

    async def release_lease(self, job_id: int) -> None:
        await self.conn.execute(
            "UPDATE jobs SET lease_owner = NULL, lease_expires_at = NULL WHERE id = ?", (job_id,)
        )
        await self.conn.commit()

    async def fail_job(self, job_id: int, error: str) -> None:
        job = await self.get_job(job_id)
        if job is None or job.state in JOB_TERMINAL_STATES:
            return
        await self.transition_job(job_id, "FAILED", last_error=error[:2000])

    # -- videos -------------------------------------------------------------

    async def create_video(self, video_id: str, job_id: int, channel_pk: int, title: str,
                            privacy_actual: str, uploaded_by: int, duration_s: Optional[float],
                            size_bytes: Optional[int], elapsed_s: Optional[float]) -> None:
        await self.conn.execute(
            """INSERT INTO videos (video_id, job_id, channel_pk, title, privacy_actual, uploaded_by,
                                    uploaded_at, duration_s, size_bytes, elapsed_s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (video_id, job_id, channel_pk, title, privacy_actual, uploaded_by, now(),
             duration_s, size_bytes, elapsed_s),
        )
        await self.conn.commit()

    async def get_video(self, video_id: str) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,))
        return await cur.fetchone()

    async def list_videos(self, *, channel_pk: Optional[int] = None, include_deleted: bool = False,
                           search: Optional[str] = None, limit: int = 200) -> list[aiosqlite.Row]:
        clauses, params = [], []
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if channel_pk:
            clauses.append("channel_pk = ?")
            params.append(channel_pk)
        if search:
            clauses.append("title LIKE ?")
            params.append(f"%{search}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cur = await self.conn.execute(
            f"SELECT * FROM videos {where} ORDER BY uploaded_at DESC LIMIT ?", params
        )
        return list(await cur.fetchall())

    async def soft_delete_video(self, video_id: str, deleted_by: str) -> None:
        await self.conn.execute(
            "UPDATE videos SET deleted_at = ?, deleted_by = ? WHERE video_id = ?",
            (now(), deleted_by, video_id),
        )
        await self.conn.commit()

    # -- quota_ledger ---------------------------------------------------------

    async def quota_used(self, bucket: str) -> int:
        cur = await self.conn.execute(
            "SELECT units_used FROM quota_ledger WHERE pt_date = ? AND bucket = ?",
            (pt_date(), bucket),
        )
        row = await cur.fetchone()
        return row["units_used"] if row else 0

    async def quota_spend(self, bucket: str, units: int = 1) -> None:
        await self.conn.execute(
            """INSERT INTO quota_ledger (pt_date, bucket, units_used)
               VALUES (?, ?, ?)
               ON CONFLICT(pt_date, bucket) DO UPDATE SET units_used = units_used + excluded.units_used""",
            (pt_date(), bucket, units),
        )
        await self.conn.commit()

    async def quota_summary(self) -> dict[str, int]:
        cur = await self.conn.execute("SELECT bucket, units_used FROM quota_ledger WHERE pt_date = ?", (pt_date(),))
        rows = await cur.fetchall()
        return {r["bucket"]: r["units_used"] for r in rows}

    # -- flow_sessions (bot conversation state) --------------------------------

    async def get_flow_session(self, tg_user_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM flow_sessions WHERE tg_user_id = ?", (tg_user_id,))
        return await cur.fetchone()

    async def save_flow_session(self, tg_user_id: int, job_id: Optional[int], step: str,
                                 draft: dict[str, Any]) -> None:
        await self.conn.execute(
            """INSERT INTO flow_sessions (tg_user_id, job_id, step, draft_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(tg_user_id) DO UPDATE SET
                   job_id = excluded.job_id, step = excluded.step,
                   draft_json = excluded.draft_json, updated_at = excluded.updated_at""",
            (tg_user_id, job_id, step, json.dumps(draft), now()),
        )
        await self.conn.commit()

    async def clear_flow_session(self, tg_user_id: int) -> None:
        await self.conn.execute("DELETE FROM flow_sessions WHERE tg_user_id = ?", (tg_user_id,))
        await self.conn.commit()

    # -- audit_log --------------------------------------------------------------

    async def write_audit(self, actor_type: str, actor_id: Optional[str], action: str,
                           target: Optional[str] = None, detail: Optional[dict[str, Any]] = None,
                           ip: Optional[str] = None) -> None:
        await self.conn.execute(
            """INSERT INTO audit_log (ts, actor_type, actor_id, action, target, detail_json, ip)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now(), actor_type, actor_id, action, target, json.dumps(detail) if detail else None, ip),
        )
        await self.conn.commit()

    async def list_audit_log(self, limit: int = 200) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,))
        return list(await cur.fetchall())

    # -- settings (key/value) ----------------------------------------------------

    async def get_setting(self, key: str, default: Any = None) -> Any:
        cur = await self.conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return json.loads(row["value_json"]) if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.conn.execute(
            """INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at""",
            (key, json.dumps(value), now()),
        )
        await self.conn.commit()

    async def all_settings(self) -> dict[str, Any]:
        cur = await self.conn.execute("SELECT key, value_json FROM settings")
        rows = await cur.fetchall()
        return {r["key"]: json.loads(r["value_json"]) for r in rows}

    # -- category_cache -----------------------------------------------------

    async def get_cached_categories(self, region_code: str, max_age_days: int = 30) -> list[aiosqlite.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        cur = await self.conn.execute(
            "SELECT * FROM category_cache WHERE region_code = ? AND fetched_at > ? ORDER BY title",
            (region_code, cutoff),
        )
        return list(await cur.fetchall())

    async def replace_category_cache(self, region_code: str, categories: list[tuple[str, str]]) -> None:
        n = now()
        await self.conn.execute("DELETE FROM category_cache WHERE region_code = ?", (region_code,))
        await self.conn.executemany(
            "INSERT INTO category_cache (region_code, category_id, title, fetched_at) VALUES (?, ?, ?, ?)",
            [(region_code, cid, title, n) for cid, title in categories],
        )
        await self.conn.commit()

    # -- dashboard/stats ----------------------------------------------------------

    async def count_jobs_by_state(self) -> dict[str, int]:
        cur = await self.conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
        rows = await cur.fetchall()
        return {r["state"]: r["n"] for r in rows}

    async def count_channels(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS n FROM yt_channels WHERE status != 'revoked'")
        return (await cur.fetchone())["n"]

    async def count_uploads_today(self) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM videos WHERE uploaded_at >= ? AND deleted_at IS NULL",
            ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
        )
        return (await cur.fetchone())["n"]
