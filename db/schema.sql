-- TG->YT Uploader v2 schema. SQLite (WAL), written to be reasonably Postgres-portable
-- (explicit PKs, no SQLite-only type affinities beyond INTEGER/TEXT/REAL/BLOB).

CREATE TABLE IF NOT EXISTS app_users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    totp_secret_enc      BLOB,
    role                TEXT NOT NULL DEFAULT 'admin',   -- owner | admin
    last_login_at       TEXT,
    failed_attempts     INTEGER NOT NULL DEFAULT 0,
    locked_until        TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_throttle (
    ip                  TEXT PRIMARY KEY,
    failed_attempts     INTEGER NOT NULL DEFAULT 0,
    locked_until        TEXT,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tg_users (
    tg_user_id          INTEGER PRIMARY KEY,
    username            TEXT,
    first_name          TEXT,
    role                TEXT NOT NULL DEFAULT 'blocked',  -- owner | admin | uploader | blocked
    added_by            INTEGER,
    added_at            TEXT NOT NULL,
    last_seen_at        TEXT,
    uploads_count       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_client (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    client_id           TEXT NOT NULL,
    client_secret_enc   BLOB NOT NULL,
    client_type         TEXT NOT NULL,             -- web | installed
    project_id          TEXT,
    uploaded_at         TEXT NOT NULL,
    uploaded_by         TEXT
);

CREATE TABLE IF NOT EXISTS yt_channels (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          TEXT NOT NULL UNIQUE,
    channel_title       TEXT NOT NULL,
    handle              TEXT,
    thumbnail_url       TEXT,
    refresh_token_enc   BLOB NOT NULL,
    access_token_enc    BLOB,
    access_expires_at   TEXT,
    scopes              TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',   -- active | needs_reauth | revoked
    is_default          INTEGER NOT NULL DEFAULT 0,
    added_at            TEXT NOT NULL,
    added_by            TEXT,
    last_used_at        TEXT,
    uploads_count       INTEGER NOT NULL DEFAULT 0,
    display_label       TEXT                              -- operator-chosen name; NULL = use channel_title
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state               TEXT PRIMARY KEY,
    created_by          TEXT,
    mode                TEXT NOT NULL,             -- hostname | loopback
    code_verifier_enc   BLOB NOT NULL,
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    consumed_at         TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id          INTEGER NOT NULL,
    channel_pk          INTEGER REFERENCES yt_channels(id) ON DELETE SET NULL,
    chat_id             INTEGER NOT NULL,
    status_message_id   INTEGER,
    tg_file_id          TEXT,
    tg_file_unique_id   TEXT,
    source_chat_id      INTEGER,
    source_message_id   INTEGER,
    file_name           TEXT,
    size_bytes          INTEGER,
    duration_s          REAL,
    width               INTEGER,
    height               INTEGER,
    mime                TEXT,
    local_path          TEXT,
    caption             TEXT,
    title               TEXT,
    description         TEXT,
    privacy             TEXT NOT NULL DEFAULT 'private',
    category_id         TEXT,
    made_for_kids       INTEGER NOT NULL DEFAULT 0,
    state               TEXT NOT NULL,
    upload_session_uri  TEXT,
    bytes_sent          INTEGER NOT NULL DEFAULT 0,
    video_id            TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    lease_owner         TEXT,
    lease_expires_at    TEXT,
    queued_at           TEXT NOT NULL,
    download_started_at TEXT,
    download_finished_at TEXT,
    upload_started_at   TEXT,
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_tg_user ON jobs(tg_user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(lease_expires_at);

CREATE TABLE IF NOT EXISTS videos (
    video_id            TEXT PRIMARY KEY,
    job_id               INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    channel_pk           INTEGER REFERENCES yt_channels(id) ON DELETE SET NULL,
    title                TEXT,
    privacy_actual       TEXT,
    uploaded_by           INTEGER,
    uploaded_at           TEXT NOT NULL,
    duration_s            REAL,
    size_bytes            INTEGER,
    elapsed_s             REAL,
    deleted_at            TEXT,
    deleted_by            TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_pk);

CREATE TABLE IF NOT EXISTS quota_ledger (
    pt_date             TEXT NOT NULL,
    bucket              TEXT NOT NULL,             -- uploads | search | general
    units_used          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pt_date, bucket)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    actor_type          TEXT NOT NULL,              -- app_user | tg_user | system
    actor_id            TEXT,
    action              TEXT NOT NULL,
    target               TEXT,
    detail_json          TEXT,
    ip                   TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS settings (
    key                 TEXT PRIMARY KEY,
    value_json          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_cache (
    region_code         TEXT NOT NULL,
    category_id         TEXT NOT NULL,
    title               TEXT NOT NULL,
    fetched_at          TEXT NOT NULL,
    PRIMARY KEY (region_code, category_id)
);

-- Per-user Telegram conversation state (video -> channel -> caption flow).
CREATE TABLE IF NOT EXISTS flow_sessions (
    tg_user_id          INTEGER PRIMARY KEY,
    job_id              INTEGER,
    step                TEXT NOT NULL,
    draft_json          TEXT NOT NULL DEFAULT '{}',
    updated_at          TEXT NOT NULL
);
