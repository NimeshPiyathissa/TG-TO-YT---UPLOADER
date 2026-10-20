#!/usr/bin/env bash
# Idempotent installer for tgyt-uploader v2 on Ubuntu 24.04.
# Run as root (or with sudo) from the repo root: sudo deploy/install.sh
#
# v1 -> v2 upgrade note: v1 ran two systemd units (tgyt-bot.service,
# tgyt-oauth.service). v2 runs bot + web UI in one process (bot/main.py), one
# unit (tgyt.service). This script removes the old units if found.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/tgyt-uploader"
DATA_DIR="/var/lib/tgyt"
ENV_DIR="/etc/tgyt"
ENV_FILE="$ENV_DIR/tgyt.env"
TLS_DIR="$ENV_DIR/tls"
FERNET_KEY_PATH="$ENV_DIR/fernet.key"

if [[ $EUID -ne 0 ]]; then
    echo "Run as root (sudo deploy/install.sh)" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
echo "==> Detecting public IP and deriving PUBLIC_HOST"
# ---------------------------------------------------------------------------
PUBLIC_IP="${PUBLIC_IP:-}"
if [[ -z "$PUBLIC_IP" ]]; then
    PUBLIC_IP="$(curl -fsSL --max-time 5 https://api.ipify.org || true)"
fi
if [[ -z "$PUBLIC_IP" ]]; then
    PUBLIC_IP="$(curl -fsSL --max-time 5 https://ifconfig.me || true)"
fi
if [[ -z "$PUBLIC_IP" ]]; then
    echo "    WARNING: could not auto-detect the public IP. Re-run with PUBLIC_IP=x.x.x.x set, or fill PUBLIC_IP/PUBLIC_HOST into $ENV_FILE by hand afterward."
fi
# Set PUBLIC_HOST=your.domain.tld to use a real domain; otherwise fall back to sslip.io.
PUBLIC_HOST_DASHED="$(echo "$PUBLIC_IP" | tr '.' '-')"
PUBLIC_HOST="${PUBLIC_HOST:-${PUBLIC_HOST_DASHED:+${PUBLIC_HOST_DASHED}.sslip.io}}"
echo "    PUBLIC_IP=$PUBLIC_IP  PUBLIC_HOST=$PUBLIC_HOST"

# ---------------------------------------------------------------------------
echo "==> Removing v1 units if present (tgyt-bot.service, tgyt-oauth.service)"
# ---------------------------------------------------------------------------
for unit in tgyt-bot.service tgyt-oauth.service; do
    if systemctl list-unit-files | grep -q "^${unit}"; then
        echo "    Stopping/disabling/removing $unit"
        systemctl stop "$unit" 2>/dev/null || true
        systemctl disable "$unit" 2>/dev/null || true
        rm -f "/etc/systemd/system/$unit"
    fi
done
systemctl daemon-reload || true

# ---------------------------------------------------------------------------
echo "==> Installing system packages"
# ---------------------------------------------------------------------------
apt-get update -y
apt-get install -y python3 python3-venv python3-pip python3-dev build-essential \
    ffmpeg nginx certbot python3-certbot-nginx git openssl curl

# ---------------------------------------------------------------------------
echo "==> Creating tgyt system user"
# ---------------------------------------------------------------------------
if ! id -u tgyt >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /home/tgyt --shell /usr/sbin/nologin tgyt
fi
# Lets the web UI's /logs page and the tgyt process itself read `journalctl -u tgyt`.
usermod -aG systemd-journal tgyt

# ---------------------------------------------------------------------------
echo "==> Creating data directories"
# ---------------------------------------------------------------------------
# Deliberately NOT auto-relocating to /mnt or /datadrive even when they have
# more free space: on Azure, /mnt (or /mnt/resource) is the local *ephemeral*
# temp-resource disk -- it is wiped on deallocation, resize, and host healing
# events. Silently putting the SQLite DB (encrypted tokens, job/video records)
# there is a data-loss trap, not an optimization. If you attach a real managed
# data disk and mount it somewhere durable, point JOBS_DIR/DB_PATH at it by
# hand in the env file -- don't rely on this installer to guess.
mkdir -p "$DATA_DIR/jobs"
chown -R tgyt:tgyt "$DATA_DIR"
chmod 0750 "$DATA_DIR"

mkdir -p "$ENV_DIR"
# Group-owned by tgyt (not root:root) so the service user can traverse into it --
# 0700 here would block tgyt from ever opening tgyt.env/fernet.key/tls/* even
# though those individual files are group-readable. 0770 (not 0750): the app
# rewrites tgyt.env in place (admin-password blanking on first boot, the
# PROJECT_AUDITED toggle from the settings page) via a tmpfile-then-rename in
# this same directory, which needs group *write*, not just read+traverse.
chown root:tgyt "$ENV_DIR"
chmod 0770 "$ENV_DIR"

# ---------------------------------------------------------------------------
echo "==> Checking RAM / swap"
# ---------------------------------------------------------------------------
TOTAL_MB=$(free -m | awk '/^Mem:/{print $2}')
if [[ "$TOTAL_MB" -lt 2048 ]] && [[ ! -f /swapfile ]]; then
    echo "    ${TOTAL_MB}MB RAM detected (<2GB); creating a 2GB swapfile"
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---------------------------------------------------------------------------
echo "==> Installing tmpfiles rule"
# ---------------------------------------------------------------------------
install -m 0644 "$REPO_DIR/deploy/tmpfiles.conf" /etc/tmpfiles.d/tgyt.conf
systemd-tmpfiles --create /etc/tmpfiles.d/tgyt.conf || true

# ---------------------------------------------------------------------------
echo "==> Syncing repo to $INSTALL_DIR"
# ---------------------------------------------------------------------------
mkdir -p "$INSTALL_DIR"
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude ".venv" --exclude "venv" --exclude "__pycache__" --exclude ".git" \
        "$REPO_DIR/" "$INSTALL_DIR/"
else
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name venv -exec rm -rf {} +
    (cd "$REPO_DIR" && tar --exclude=.git --exclude=venv --exclude=.venv --exclude=__pycache__ -cf - .) \
        | (cd "$INSTALL_DIR" && tar -xf -)
fi
chown -R tgyt:tgyt "$INSTALL_DIR"

# ---------------------------------------------------------------------------
echo "==> Creating venv and installing dependencies"
# ---------------------------------------------------------------------------
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    sudo -u tgyt python3 -m venv "$INSTALL_DIR/venv"
fi
sudo -u tgyt "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
sudo -u tgyt "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# ---------------------------------------------------------------------------
echo "==> Generating Fernet key (token/secret encryption at rest)"
# ---------------------------------------------------------------------------
if [[ ! -f "$FERNET_KEY_PATH" ]]; then
    sudo -u tgyt "$INSTALL_DIR/venv/bin/python" -c \
        "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > "$FERNET_KEY_PATH"
    chown tgyt:tgyt "$FERNET_KEY_PATH"
    chmod 0400 "$FERNET_KEY_PATH"
    echo "    Wrote $FERNET_KEY_PATH"
else
    echo "    $FERNET_KEY_PATH already exists, leaving it untouched."
fi

# ---------------------------------------------------------------------------
echo "==> Generating self-signed TLS cert (used directly in loopback mode; kept as a fallback in hostname mode)"
# ---------------------------------------------------------------------------
mkdir -p "$TLS_DIR"
if [[ ! -f "$TLS_DIR/selfsigned.crt" || ! -f "$TLS_DIR/selfsigned.key" ]]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$TLS_DIR/selfsigned.key" -out "$TLS_DIR/selfsigned.crt" \
        -subj "/CN=tgyt-uploader" >/dev/null 2>&1
    echo "    Generated. SHA-256 fingerprint (verify this once in your browser):"
    openssl x509 -noout -fingerprint -sha256 -in "$TLS_DIR/selfsigned.crt" | sed 's/^/    /'
fi
chown -R tgyt:tgyt "$TLS_DIR"
chmod 0750 "$TLS_DIR"
chmod 0400 "$TLS_DIR/selfsigned.key"

# ---------------------------------------------------------------------------
echo "==> Setting up environment file"
# ---------------------------------------------------------------------------
# Small prompt helpers -- used only when we have a real terminal (-t 0). A
# non-interactive run (CI, `curl | bash` piped without -i, etc.) falls back to
# leaving these blank with a loud warning, same as before.
prompt_required() {
    # $1 = prompt text. Echoes the answer; re-prompts on empty input.
    local text="$1" value=""
    while [[ -z "$value" ]]; do
        read -r -p "  $text: " value
    done
    printf '%s' "$value"
}
prompt_required_numeric() {
    local text="$1" value=""
    while ! [[ "$value" =~ ^[0-9]+$ ]]; do
        read -r -p "  $text: " value
        [[ "$value" =~ ^[0-9]+$ ]] || echo "    Please enter digits only."
    done
    printf '%s' "$value"
}
prompt_required_hex() {
    # Telegram's API_HASH is always 32 lowercase hex chars. Validating that
    # shape here isn't just UX -- this value gets interpolated into a
    # double-quoted bash string that's fed to `python -c` further down, so an
    # unvalidated value could break out of that string. Reject anything else.
    local text="$1" value=""
    while ! [[ "$value" =~ ^[a-f0-9]+$ ]]; do
        read -r -p "  $text: " value
        [[ "$value" =~ ^[a-f0-9]+$ ]] || echo "    Expected a hex string (letters a-f and digits only)."
    done
    printf '%s' "$value"
}
prompt_secret_required() {
    local text="$1" value=""
    while [[ -z "$value" ]]; do
        read -r -s -p "  $text: " value
        echo
    done
    printf '%s' "$value"
}
prompt_default() {
    local text="$1" default="$2" value=""
    read -r -p "  $text [$default]: " value
    printf '%s' "${value:-$default}"
}

ENV_IS_NEW=0
if [[ ! -f "$ENV_FILE" ]]; then
    ENV_IS_NEW=1
    cp "$REPO_DIR/.env.example" "$ENV_FILE"
    sed -i "s#^PUBLIC_IP=.*#PUBLIC_IP=$PUBLIC_IP#" "$ENV_FILE"
    sed -i "s#^PUBLIC_HOST=.*#PUBLIC_HOST=$PUBLIC_HOST#" "$ENV_FILE"
    sed -i "s#^FERNET_KEY_PATH=.*#FERNET_KEY_PATH=$FERNET_KEY_PATH#" "$ENV_FILE"
    sed -i "s#^JOBS_DIR=.*#JOBS_DIR=$DATA_DIR/jobs#" "$ENV_FILE"
    sed -i "s#^DB_PATH=.*#DB_PATH=$DATA_DIR/tgyt.db#" "$ENV_FILE"

    SESSION_SECRET="$(openssl rand -hex 32)"
    sed -i "s#^SESSION_SECRET=.*#SESSION_SECRET=$SESSION_SECRET#" "$ENV_FILE"
    # Blank these two down to a bare `KEY=` regardless of path below --
    # .env.example carries them as inline-commented placeholders, and leaving
    # the comment text in place breaks config parsing (it becomes the literal
    # value). Interactive answers below overwrite them for real.
    sed -i "s#^ADMIN_PASSWORD=.*#ADMIN_PASSWORD=#" "$ENV_FILE"
    sed -i "s#^USER_SESSION=.*#USER_SESSION=#" "$ENV_FILE"
    sed -i "s#^RELAY_CHAT_ID=.*#RELAY_CHAT_ID=#" "$ENV_FILE"

    if [[ -t 0 ]]; then
        echo
        echo "  ---------------------------------------------------------------"
        echo "  A few details to finish setup. Have these ready:"
        echo "    - API_ID / API_HASH from https://my.telegram.org (API development tools)"
        echo "    - A bot token from @BotFather"
        echo "    - Your own numeric Telegram ID (message @userinfobot to get it)"
        echo "  ---------------------------------------------------------------"
        echo

        API_ID_VALUE="$(prompt_required_numeric "Telegram API_ID")"
        API_HASH_VALUE="$(prompt_required_hex "Telegram API_HASH")"
        BOT_TOKEN_VALUE="$(prompt_required "Bot token (from @BotFather)")"
        OWNER_ID_VALUE="$(prompt_required_numeric "Your numeric Telegram ID (becomes the owner)")"
        sed -i "s#^API_ID=.*#API_ID=$API_ID_VALUE#" "$ENV_FILE"
        sed -i "s#^API_HASH=.*#API_HASH=$API_HASH_VALUE#" "$ENV_FILE"
        sed -i "s#^BOT_TOKEN=.*#BOT_TOKEN=$BOT_TOKEN_VALUE#" "$ENV_FILE"
        sed -i "s#^OWNER_TG_ID=.*#OWNER_TG_ID=$OWNER_ID_VALUE#" "$ENV_FILE"

        echo
        ADMIN_USER_VALUE="$(prompt_default "Web panel admin username" "admin")"
        ADMIN_PW_VALUE="$(prompt_secret_required "Web panel admin password (used once, then hashed + wiped from this file)")"
        sed -i "s#^ADMIN_USERNAME=.*#ADMIN_USERNAME=$ADMIN_USER_VALUE#" "$ENV_FILE"
        sed -i "s#^ADMIN_PASSWORD=.*#ADMIN_PASSWORD=$ADMIN_PW_VALUE#" "$ENV_FILE"
        unset ADMIN_PW_VALUE

        echo
        echo "  Networking mode for the OAuth redirect + web panel:"
        echo "    1) hostname (recommended) -- real HTTPS via a free sslip.io hostname"
        echo "       + Let's Encrypt. Needs inbound port 80 reachable briefly, and"
        echo "       WEB_PORT reachable always."
        echo "    2) loopback -- self-signed cert, link channels via SSH tunnel or an"
        echo "       offline helper script instead. No inbound port 80 needed ever."
        MODE_CHOICE="$(prompt_default "Choose 1 or 2" "1")"
        if [[ "$MODE_CHOICE" == "2" ]]; then
            sed -i "s#^OAUTH_MODE=.*#OAUTH_MODE=loopback#" "$ENV_FILE"
        else
            sed -i "s#^OAUTH_MODE=.*#OAUTH_MODE=hostname#" "$ENV_FILE"
        fi
        WEB_PORT_VALUE="$(prompt_default "Port to expose the web panel on (443 = no port in the URL)" "443")"
        sed -i "s#^WEB_PORT=.*#WEB_PORT=$WEB_PORT_VALUE#" "$ENV_FILE"
        REGION_VALUE="$(prompt_default "Default YouTube region code (ISO 3166-1 alpha-2)" "US")"
        sed -i "s#^YT_REGION_CODE=.*#YT_REGION_CODE=$REGION_VALUE#" "$ENV_FILE"

        echo
        echo "  Optional: uploading files over 20MB needs a second Telegram login"
        echo "  (a real account, not the bot) that relays large files through a"
        echo "  private channel both accounts belong to. You can set this up now,"
        echo "  or skip it and follow the README's 'Generating the string session'"
        echo "  section later -- nothing else in this install depends on it."
        SETUP_RELAY="$(prompt_default "Set up large-file support now? [y/N]" "n")"
        if [[ "$SETUP_RELAY" =~ ^[Yy] ]]; then
            echo "  In Telegram: create a private channel, add your bot as admin there,"
            echo "  and make sure your own account has joined it too."
            read -r -p "  Press Enter once that's done (or Ctrl+C to abort just this step)... " _
            set +e
            sudo -u tgyt "$INSTALL_DIR/venv/bin/python" -c "
from pyrogram import Client
with Client('gen', api_id=$API_ID_VALUE, api_hash='$API_HASH_VALUE', in_memory=True) as app:
    print()
    print('Your recent chats -- note the relay channel id (looks like -100...):')
    for d in app.get_dialogs(limit=20):
        print(' ', d.chat.id, '-', (d.chat.title or d.chat.first_name or ''))
    with open('/tmp/tgyt_session_out', 'w') as f:
        f.write(app.export_session_string())
"
            RELAY_RC=$?
            set -e
            if [[ $RELAY_RC -eq 0 && -f /tmp/tgyt_session_out ]]; then
                USER_SESSION_VALUE="$(cat /tmp/tgyt_session_out)"
                shred -u /tmp/tgyt_session_out 2>/dev/null || rm -f /tmp/tgyt_session_out
                sed -i "s#^USER_SESSION=.*#USER_SESSION=$USER_SESSION_VALUE#" "$ENV_FILE"
                unset USER_SESSION_VALUE
                RELAY_ID_VALUE="$(prompt_required "Relay channel id from the list above")"
                sed -i "s#^RELAY_CHAT_ID=.*#RELAY_CHAT_ID=$RELAY_ID_VALUE#" "$ENV_FILE"
                echo "    Large-file relay configured."
            else
                echo "    Skipped -- large-file support isn't configured. Set USER_SESSION"
                echo "    and RELAY_CHAT_ID in $ENV_FILE later, see README."
            fi
        fi
    else
        echo "    Non-interactive shell: leaving Telegram/admin fields blank. Fill in $ENV_FILE by hand before starting the service."
    fi
    echo
    echo "    Wrote $ENV_FILE."
else
    echo "    $ENV_FILE already exists, leaving it untouched."
fi
chown root:tgyt "$ENV_FILE"
chmod 0640 "$ENV_FILE"

# Read back OAUTH_MODE so the nginx step below matches whatever's actually configured
# (respects a manually-edited env file on re-runs, not just what this run just wrote).
CURRENT_OAUTH_MODE="$(grep -E '^OAUTH_MODE=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
CURRENT_OAUTH_MODE="${CURRENT_OAUTH_MODE:-hostname}"
CURRENT_PUBLIC_HOST="$(grep -E '^PUBLIC_HOST=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
CURRENT_WEB_PORT="$(grep -E '^WEB_PORT=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
CURRENT_WEB_PORT="${CURRENT_WEB_PORT:-443}"
CURRENT_LOOPBACK_PORT="$(grep -E '^LOOPBACK_PORT=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
CURRENT_LOOPBACK_PORT="${CURRENT_LOOPBACK_PORT:-8721}"
CURRENT_ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"

# ---------------------------------------------------------------------------
echo "==> Installing systemd unit (tgyt.service)"
# ---------------------------------------------------------------------------
install -m 0644 "$REPO_DIR/deploy/tgyt.service" /etc/systemd/system/tgyt.service
sed -i "s#/opt/tgyt-uploader#$INSTALL_DIR#g; s#/etc/tgyt/tgyt.env#$ENV_FILE#g" /etc/systemd/system/tgyt.service
systemctl daemon-reload

# ---------------------------------------------------------------------------
echo "==> Sudoers rule for the web UI's restart-services button"
# ---------------------------------------------------------------------------
SUDOERS_FILE="/etc/sudoers.d/tgyt-restart"
echo "tgyt ALL=(root) NOPASSWD: /bin/systemctl restart tgyt.service" > "$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"
if ! visudo -c -f "$SUDOERS_FILE" >/dev/null 2>&1; then
    echo "    WARNING: sudoers rule failed validation, removing it. The restart-services button in the UI will not work."
    rm -f "$SUDOERS_FILE"
fi

# ---------------------------------------------------------------------------
echo "==> Nginx (Mode A / hostname only)"
# ---------------------------------------------------------------------------
if [[ "$CURRENT_OAUTH_MODE" == "hostname" ]]; then
    if [[ -z "$CURRENT_PUBLIC_HOST" ]]; then
        echo "    WARNING: OAUTH_MODE=hostname but PUBLIC_HOST is empty in $ENV_FILE -- skipping nginx/certbot. Fill it in and re-run."
    else
        NGINX_PORT_SUFFIX=""
        [[ "$CURRENT_WEB_PORT" != "443" ]] && NGINX_PORT_SUFFIX=":${CURRENT_WEB_PORT}"
        sed -e "s#__PUBLIC_HOST__#$CURRENT_PUBLIC_HOST#g" -e "s#__WEB_PORT_SUFFIX__#$NGINX_PORT_SUFFIX#g" -e "s#__WEB_PORT__#$CURRENT_WEB_PORT#g" \
            "$REPO_DIR/deploy/nginx.conf" > /etc/nginx/sites-available/tgyt.conf
        ln -sf /etc/nginx/sites-available/tgyt.conf /etc/nginx/sites-enabled/tgyt.conf
        # certbot's HTTP-01 challenge needs port 80 reachable; it's opened here only
        # transiently for issuance -- close it again in your NSG afterward if you like.
        nginx -t && systemctl reload nginx
        echo "==> Requesting TLS certificate for $CURRENT_PUBLIC_HOST (port 80 must be reachable right now)"
        # CERTBOT_EMAIL is optional; without it the cert is registered with no contact address.
        CERTBOT_ACCOUNT_ARGS=(--register-unsafely-without-email)
        [[ -n "${CERTBOT_EMAIL:-}" ]] && CERTBOT_ACCOUNT_ARGS=(-m "$CERTBOT_EMAIL")
        if certbot --nginx -d "$CURRENT_PUBLIC_HOST" --non-interactive --agree-tos "${CERTBOT_ACCOUNT_ARGS[@]}" --redirect; then
            echo "    Certificate issued."
        else
            echo "    ================================================================"
            echo "    CERTBOT FAILED. This usually means port 80 isn't reachable from"
            echo "    the internet yet (check your cloud firewall/security group) or"
            echo "    sslip.io is blocked."
            echo "    Fallback: set OAUTH_MODE=loopback in $ENV_FILE and re-run this"
            echo "    installer to use the self-signed-cert + SSH-tunnel/offline-helper"
            echo "    linking path instead. See README section on switching modes."
            echo "    ================================================================"
        fi
    fi
else
    echo "    OAUTH_MODE=$CURRENT_OAUTH_MODE -- skipping nginx/certbot entirely (uvicorn serves TLS directly)."
    if [[ -e /etc/nginx/sites-enabled/tgyt.conf ]]; then
        rm -f /etc/nginx/sites-enabled/tgyt.conf
        systemctl reload nginx 2>/dev/null || true
    fi
fi

# ---------------------------------------------------------------------------
echo "==> Enabling and starting tgyt.service"
# ---------------------------------------------------------------------------
systemctl enable --now tgyt.service

# Must mirror bot/config.py's Settings.redirect_uri/ui_base_url exactly: nginx
# only listens on CURRENT_WEB_PORT, so a redirect URI that omits a non-443 port
# silently breaks the post-consent redirect (browser tries the implicit port
# 443, nothing answers there).
WEB_PORT_SUFFIX=""
[[ "$CURRENT_WEB_PORT" != "443" ]] && WEB_PORT_SUFFIX=":${CURRENT_WEB_PORT}"
REDIRECT_URI="https://${CURRENT_PUBLIC_HOST}${WEB_PORT_SUFFIX}/oauth/callback"
UI_URL="https://${CURRENT_PUBLIC_HOST}${WEB_PORT_SUFFIX}"
if [[ "$CURRENT_OAUTH_MODE" == "loopback" ]]; then
    REDIRECT_URI="http://127.0.0.1:${CURRENT_LOOPBACK_PORT}/oauth/callback"
    UI_URL="https://${PUBLIC_IP}${WEB_PORT_SUFFIX}"
fi

CURRENT_RELAY_CHAT_ID="$(grep -E '^RELAY_CHAT_ID=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"

cat <<EOF

==================== Post-install checklist ====================
1. Cloud firewall / security group (Azure NSG, AWS Security Group, GCP
   firewall rule, ...): allow inbound 22 and ${CURRENT_WEB_PORT} from YOUR IP
   only. Port 80 is only needed transiently, while certbot issues/renews a
   cert (hostname mode) -- fine to close it the rest of the time.

2. Google Cloud Console:
   - Create/select a project -> APIs & Services -> Library -> enable
     "YouTube Data API v3".
   - APIs & Services -> OAuth consent screen: User type External, add scopes
       https://www.googleapis.com/auth/youtube.upload
       https://www.googleapis.com/auth/youtube.readonly
     While in Testing mode, add every linking Google account under
     "Test users" (max 100; their refresh tokens expire after 7 days).
   - APIs & Services -> Credentials -> Create credentials -> OAuth client ID:
$( [[ "$CURRENT_OAUTH_MODE" == "loopback" ]] && echo "       Application type: Desktop app" || echo "       Application type: Web application" )
       Authorized redirect URI (must match EXACTLY):
         ${REDIRECT_URI}
   - Download the client_secret_*.json and upload it in the web panel's
     Settings / Add-a-channel page once you're logged in (see the bottom of
     this checklist).

3. Audit: uploads are forced Private until this API project passes YouTube's
   Compliance Audit. Apply at:
   https://support.google.com/youtube/contact/yt_api_form
   Set PROJECT_AUDITED=true in $ENV_FILE once it passes, then restart.

4. Large files (>20MB): $( [[ -n "$CURRENT_RELAY_CHAT_ID" ]] && echo "configured (RELAY_CHAT_ID set)." || echo "not configured yet -- see the README's 'Generating the string session' section." )

5. After editing $ENV_FILE by hand: systemctl restart tgyt

6. Logs: journalctl -u tgyt -f  (also viewable in the web panel under Logs)

7. Back up $FERNET_KEY_PATH and $DATA_DIR/tgyt.db separately, off this box --
   losing the Fernet key makes every stored token permanently undecryptable.

==================================================================
   Log in to the web panel:

     ${UI_URL}
     Username: $(grep -E '^ADMIN_USERNAME=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)
$( if [[ -z "$CURRENT_ADMIN_PASSWORD" ]]; then echo "     Password: NOT SET -- edit ADMIN_PASSWORD in $ENV_FILE, then: systemctl restart tgyt"; else echo "     Password: the one you just entered (hashed into the database and wiped from $ENV_FILE on first boot)"; fi )
==================================================================
EOF
