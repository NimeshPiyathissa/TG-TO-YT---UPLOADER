# TG→YT Uploader (v2, IP-only)

Send a video to your bot on Telegram, pick a channel, add a caption, and it
uploads to YouTube with a resumable, restart-safe upload and live progress —
plus a web admin panel for managing channels, users, jobs, uploaded videos,
logs, and settings. No domain name required.

## What changed from v1

- **No domain.** Everything is reachable from the VPS's public IP. Google
  won't accept a bare IP as an OAuth redirect URI, so there are two supported
  networking modes (below) instead of one hosted-domain callback.
- **Web admin UI** at `https://<host>:<WEB_PORT>` — dashboard, channels,
  users, jobs, uploaded videos (with delete), logs, settings. Argon2 login,
  optional TOTP, per-account and per-IP lockout, CSRF on every form.
- **Channel linking is driven by uploading JSON**, not a hosted callback on a
  domain you own — see "Adding a channel" below.
- **One process, one systemd unit** (`tgyt.service`) — the bot and the web UI
  run together (`bot/main.py`), instead of v1's separate `tgyt-bot` +
  `tgyt-oauth` units.
- Simplified bot flow: video → pick channel → caption → upload → link +
  elapsed time. Delete a video from the web UI and it's deleted on YouTube
  too.

## Read this first: three things that will break the build if ignored

### 1. The JSON file you upload is not a channel credential

"Upload a JSON and the channel is added" isn't quite how this works — there
are two different JSON files, and it matters which one you're holding:

- **`client_secret_*.json`** (Google Cloud Console → Credentials → your
  OAuth client → Download JSON) identifies the **API project**, not a
  channel. One of these covers *every* channel you'll ever link. Upload it
  **once**, in **Settings**.
- **A service-account JSON will never work.** Service accounts have no
  associated YouTube channel and can't be given one; the API returns
  `youtubeSignupRequired`. The upload endpoint rejects a service-account JSON
  immediately with: *"Service accounts can't upload to YouTube. Download an
  OAuth Client ID JSON (Desktop app) instead."*
- **`token.json`** (produced by `tools/link_channel.py`, or internally after
  a completed consent) *is* a per-channel credential — a saved refresh token.

So "add a channel" = upload `client_secret.json` once (Settings), then run a
consent flow per channel (Channels → Add). The UI makes that feel like one
flow; the two JSON types are still handled separately underneath.

### 2. Google will not accept a bare IP as a redirect URI

Google's rules: redirect URIs must be HTTPS (not plain HTTP), the host can't
be a raw IP address, and the host's TLD must be on the public suffix list.
`localhost`/loopback IPs are the one exemption to both of those rules. So
`http://20.1.2.3/oauth/callback` is rejected outright. Two modes work
around this, both domain-free:

**Mode A — `OAUTH_MODE=hostname` (default)**
Uses a wildcard-DNS hostname that maps to your IP:
`https://20-1-2-3.sslip.io` (dashes instead of dots). It's a real hostname on
a real public TLD, so Google accepts it, and certbot can issue a real Let's
Encrypt certificate for it over HTTP-01 — nothing registered, nothing paid
for. `deploy/install.sh` derives this from your detected public IP
automatically; you don't type anything. Redirect URI to register in Google
Cloud Console:

```
https://<ip-with-dashes>.sslip.io/oauth/callback
```

nginx holds the real cert and proxies to the app internally; the web UI gets
real HTTPS with no browser warning. If certificate issuance fails (rate
limit, port 80 blocked in your NSG), the installer says so loudly and tells
you to fall back to Mode B.

**Mode B — `OAUTH_MODE=loopback` (fallback, zero third-party DNS)**
The OAuth client is created as a **Desktop app** type, redirect
`http://127.0.0.1:8721/oauth/callback` (`LOOPBACK_PORT` in the env file).
uvicorn serves the web UI directly with a self-signed cert (fingerprint
printed once at install — verify it in your browser the first time). Two
ways to finish linking a channel in this mode:

1. **SSH tunnel.** The "Add a channel" page under Advanced linking shows a
   ready-to-copy command with your IP filled in:
   ```
   ssh -L 8721:127.0.0.1:8721 <your-ssh-user>@<your-vps-ip>
   ```
   Run that locally, click the consent link the UI gives you, approve in
   your browser — the redirect to `127.0.0.1:8721` tunnels straight to the
   listener on the VPS, and the channel appears in the panel.
2. **Offline helper, `tools/link_channel.py`.** No SSH, no tunnel. Download
   it from the same Advanced-linking panel (or find it at
   `tools/link_channel.py` in this repo), run it on your own machine with
   the same `client_secret.json`:
   ```
   pip install google-auth-oauthlib google-api-python-client
   python tools/link_channel.py /path/to/client_secret.json
   ```
   It opens your browser, you approve, it writes `token.json` next to
   itself. Upload that file back into the panel's Advanced-linking
   drop-zone and the channel is linked.

Both paths are always available under "Advanced linking" regardless of which
mode is active as the default; Mode A (hostname) is what a fresh install
tries first.

### 3. Carried over from v1 — still true

| Constraint | Consequence |
|---|---|
| **Audit lock**: uploads via `videos.insert` from an unaudited API project are forced Private no matter what privacy you request | Shown on `/start`, on the Add-channel screen, and after every upload. The truthful status is read back via `videos.list(part=status)`, not assumed. |
| **Quota**: `videos.insert` and `search.list` each have their own 100-calls/day bucket (1 unit/call); everything else shares 10,000 units/day (`thumbnails.set` 50, `playlistItems.insert` 50, `videos.delete` 50, `videos.update` 50) | Tracked in `quota_ledger`, dashboard shows both. Resets midnight America/Los_Angeles. |
| **Testing-mode OAuth**: refresh tokens expire after 7 days while the consent screen is in Testing; `youtube.upload` is a restricted scope | `invalid_grant` on refresh flips the channel to `needs_reauth`, DMs the owner, shows a red badge in the UI. |
| **Telegram size caps**: Bot API `getFile` stops at 20MB; the MTProto relay account reaches `MAX_FILE_BYTES` (2GB default, up to 4GB with Telegram Premium on that account) | Without `USER_SESSION` set, anything over 20MB is rejected with a clear in-chat message instead of hanging. |
| **YouTube limits** | 256GB max file size, MIME `video/*`. Unverified channels are capped at 15-minute videos — the bot warns before uploading, not after it fails. |

## Google Cloud setup, start to finish

1. Console → new project (or reuse one) → **APIs & Services → Library** →
   enable **YouTube Data API v3**.
2. **APIs & Services → OAuth consent screen**:
   - User type: External.
   - Scopes: add `youtube.upload` and `youtube.readonly`.
   - While in **Testing** (the default): add every Google account that will
     link a channel under **Test users** (max 100; refresh tokens for these
     users expire after 7 days, so re-authorizing periodically is expected).
   - Publishing the app lifts both the 7-day expiry and the 100-user cap, but
     requires Google's verification review for restricted scopes.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Mode A (hostname): **Web application**, Authorized redirect URI exactly
     `https://<your-sslip.io-host>/oauth/callback`.
   - Mode B (loopback): **Desktop app**, redirect
     `http://127.0.0.1:8721/oauth/callback`.
   - `deploy/install.sh` prints the exact string to paste, computed for your
     box.
   - Download the JSON and upload it in the web panel: **Settings** (or the
     drop-zone on **Channels → Add**).
4. **YouTube API Services Compliance Audit**: required before any upload can
   actually go public. Apply from inside the API Services dashboard, or via
   https://support.google.com/youtube/contact/yt_api_form. Until it passes,
   leave `PROJECT_AUDITED=false`.

## Generating the string session (MTProto relay)

Files over 20MB can't go through the Bot API's `getFile` (20MB cap), so a
second Telegram login — a normal user account, not a bot — relays the bytes
over MTProto. This account needs `API_ID`/`API_HASH` from
https://my.telegram.org, and must be a member of a private channel
(`RELAY_CHAT_ID`) that the bot account is also an admin of: the bot forwards
the incoming video there, the user session downloads it from that channel,
and the bot deletes the forwarded copy afterward. Generate the session
string once, locally:

```bash
python -c "
from pyrogram import Client
with Client('gen', api_id=YOUR_API_ID, api_hash='YOUR_API_HASH', in_memory=True) as app:
    print(app.export_session_string())
"
```

Log in with the phone number of the relay account when prompted, then paste
the printed string into `USER_SESSION` in `/etc/tgyt/tgyt.env`. Treat it
exactly like a password — anyone with it has full access to that Telegram
account. Without `USER_SESSION` set, files over 20MB are rejected with a
clear in-chat message instead of hanging.

## The web admin panel

- **Dashboard** — linked channels, uploads today vs the 100/day bucket,
  shared-quota units used, active jobs (live, refreshes every few seconds),
  disk free on the jobs volume, bot connection status, uptime.
- **Channels** — cards with thumbnail/title/handle/status; add, set default,
  re-authorize, remove.
- **Users** — Telegram access control by numeric ID (`owner` / `admin` /
  `uploader` / `blocked`), plus a feed of denied access attempts with a
  one-click "grant access".
- **Jobs** — active and historical uploads with live progress, filters,
  cancel/retry.
- **Videos** — everything ever uploaded: channel, uploader, size, duration,
  elapsed time, privacy, link, and a delete button that removes it from
  YouTube too (soft-deletes the row; safe to click twice — an
  already-deleted video just gets marked, no error).
- **Logs** — tails `journalctl -u tgyt`, level filter, text search, plus a
  separate audit-log tab (every privileged action: channel added/removed,
  user granted/blocked, video deleted, settings changed, logins) and a
  download-as-file button.
- **Settings** — OAuth client management, the `PROJECT_AUDITED` toggle,
  default privacy/category, concurrency limits, chunk size, job retention,
  quota display, a restart-services button, and a redacted `.env` view.

## Running locally (dev)

```bash
python -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in every value; for local dev, OAUTH_MODE=loopback is simplest
# generate a Fernet key and point FERNET_KEY_PATH at it:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > /tmp/fernet.key
venv/bin/python -m bot.main
```

In production, use `deploy/install.sh` instead of running this by hand.

## Deploying (Ubuntu 24.04)

```bash
sudo deploy/install.sh
```

Idempotent, and safe to re-run to pick up code changes (it won't overwrite an
existing `/etc/tgyt/tgyt.env`). It also detects and cleanly removes a prior
v1 install's `tgyt-bot.service`/`tgyt-oauth.service` units before installing
the single v2 `tgyt.service`.

What it does: installs system packages (`ffmpeg`, `nginx`, `certbot`, build
tools), creates the `tgyt` system user, `/var/lib/tgyt` (0750, `tgyt:tgyt`,
always on the root/OS disk -- deliberately not auto-relocated onto whatever
mounted disk happens to have more free space, since on Azure that's often the
ephemeral `/mnt` temp-resource disk, which is wiped on deallocation/resize; if
you want the data dir on a real attached managed disk, mount it durably and
point `JOBS_DIR`/`DB_PATH` at it by hand in the env file), a
2GB swapfile if RAM is under 2GB, a venv at `/opt/tgyt-uploader/venv`,
generates the Fernet key (`/etc/tgyt/fernet.key`, mode 0400) and a
self-signed TLS cert, then writes `/etc/tgyt/tgyt.env` from `.env.example`
and **interactively asks for everything it needs**, one prompt at a time:
Telegram `API_ID`/`API_HASH`/bot token/your numeric ID, the web panel's
admin username + password, networking mode (`hostname` recommended, or
`loopback`), the port to expose the panel on (443 by default -- no port in
any URL), your YouTube region code, and, optionally, a full walkthrough for
large-file (>20MB) support that logs in a second Telegram account right then
and there and writes `USER_SESSION`/`RELAY_CHAT_ID` for you. Skip any
optional step and fill it in later by editing `/etc/tgyt/tgyt.env` and
running `systemctl restart tgyt`. It then installs the hardened
`tgyt.service` unit, installs nginx + runs certbot **only** when
`OAUTH_MODE=hostname`, adds a narrowly-scoped sudoers rule for the UI's
restart button, and finishes with a checklist -- the exact redirect URI to
paste into Google Cloud Console, and, last, the panel URL and login to open
once you're done there. Piping this script into a shell with no terminal
attached (CI, `curl | bash`) skips every prompt and leaves those fields
blank for you to fill in by hand.

If IP auto-detection picks the wrong address (e.g. behind certain NAT
setups), override it: `sudo PUBLIC_IP=1.2.3.4 deploy/install.sh`.

**Switching modes later**: edit `OAUTH_MODE` in `/etc/tgyt/tgyt.env`, then
re-run `sudo deploy/install.sh` (installs/removes the nginx vhost as
appropriate) followed by `sudo systemctl restart tgyt`. You'll need to
re-register your OAuth client's redirect URI in Google Cloud Console to
match the new mode.

Disk, not RAM: job files live at `$JOBS_DIR/<job_id>/` (default
`/var/lib/tgyt/jobs`), never `/tmp` — a 2GB download into tmpfs could OOM a
small VM.

## Backup / restore

- **Database**: `/var/lib/tgyt/tgyt.db` (plus `-wal`/`-shm` while running).
  Stop `tgyt` first for a consistent copy, or use
  `sqlite3 /var/lib/tgyt/tgyt.db ".backup /path/to/backup.db"` live.
- **Fernet key** (`/etc/tgyt/fernet.key`): back this up separately from the
  DB and store it somewhere durable — losing it makes every stored
  refresh/access token and client secret permanently undecryptable, and
  every linked channel has to be re-linked from scratch.
- Restore: stop the service, put the DB file back, put the Fernet key back,
  `systemctl start tgyt`.

## Logs

```bash
journalctl -u tgyt -f
```

Also viewable, filterable, and downloadable from the web panel's **Logs**
page. Structured JSON (`structlog`) with a redaction processor
(`bot/logging_setup.py`) that strips tokens, session strings, and resumable
upload session URIs down to a 6-character prefix before they're ever
written — grep for a full credential in these logs and you should never
find one.

## Security notes

- Azure NSG: allow 22 and `WEB_PORT` from your own IP only. Leave port 80
  closed except transiently during certbot issuance/renewal in Mode A.
- Fernet-encrypts every token and client secret at rest; TOTP secrets are
  encrypted too.
- Login throttling is both per-account and per-IP (5 attempts → 15-minute
  lockout), independent of each other.
- `tgyt.service` runs hardened: `NoNewPrivileges`, `PrivateTmp`,
  `ProtectSystem=strict`, `ProtectHome`, a narrow `ReadWritePaths`, and a
  768MB memory cap.

## Assumptions made while building this (v2)

Stated inline per the build spec's own instruction, collected here for
visibility:

- **`RELAY_CHAT_ID`** (carried over from v1) is required alongside
  `USER_SESSION` for the MTProto relay pattern described above — it isn't
  spelled out in the v2 spec's own `.env.example` listing, but the
  underlying Telegram size-cap constraint (spec section 1.3) is unchanged
  from v1, and the relay is how v1 solved it.
- **`INTERNAL_WEB_PROXY_PORT` is fixed at `8001`**, not configurable via env
  — it's purely an internal detail of how nginx talks to uvicorn in Mode A,
  never exposed externally, so there's no operator-facing reason to make it
  tunable. `deploy/nginx.conf` hardcodes the same value.
- **`WEB_PORT` (default `443`) is the one externally-exposed port in both
  modes** — nginx listens there directly in Mode A (proxying internally to
  8001), and uvicorn binds it directly with a self-signed cert in Mode B.
  `bot/config.py`'s `redirect_uri`/`ui_base_url` only omit the port from a
  URL when `WEB_PORT` is exactly `443` (the HTTPS default); pick any other
  port at install time (the installer will ask) and it's included
  automatically everywhere it needs to be — Google Cloud Console redirect
  URI, the panel URL, the bot's `/addyt` message.
- The Fernet key moved from an env var (v1) to a **file** at
  `FERNET_KEY_PATH` (v2 spec section 8: "the key lives at
  `/etc/tgyt/fernet.key`") — this also means it's generated once by the
  installer and never touches the env file, unlike v1's `FERNET_KEY=`.
- `ADMIN_PASSWORD` is written to the env file in plaintext for exactly one
  boot; `bot/main.py` hashes it into the DB and blanks the env file itself
  on first successful start. If the installer runs non-interactively (no
  TTY, e.g. scripted provisioning), it leaves `ADMIN_PASSWORD` blank and the
  service will refuse to start with a clear error until it's set by hand.
- The installer does **not** auto-relocate `/var/lib/tgyt` onto whatever
  mounted disk has the most free space, even though that looked tempting for
  a "big video downloads shouldn't fill the root disk" heuristic. Checked
  against a real Azure VM: `/mnt` is mounted there with far more free space
  than `/`, and it's the ephemeral temp-resource disk -- wiped on
  deallocation/resize/host-healing. Silently putting the encrypted-token
  SQLite DB there would be a data-loss trap, so this stays a manual opt-in
  (point `JOBS_DIR`/`DB_PATH` at a real durable mount yourself) rather than
  something the installer guesses at.
- `certbot --nginx ... --redirect` is used for cert issuance, which may add
  its own HTTP→HTTPS redirect logic alongside the one already present in
  `deploy/nginx.conf`'s port-80 server block; both end up redirecting to the
  same place, so this is redundant rather than conflicting, but worth
  knowing if you're hand-editing the generated vhost later.
