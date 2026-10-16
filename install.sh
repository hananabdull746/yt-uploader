#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="yt-uploader"
APP_USER="yt-uploader"
APP_GROUP="yt-uploader"
INSTALL_DIR="/opt/yt-uploader"
STATE_DIR="/var/lib/yt-uploader"
LOG_DIR="/var/log/yt-uploader"
ENV_DIR="/etc/yt-uploader"
ENV_FILE="$ENV_DIR/yt-uploader.env"
SERVICE_FILE="/etc/systemd/system/yt-uploader.service"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi
if [[ ! -f "$SOURCE_DIR/requirements.txt" || ! -d "$SOURCE_DIR/app" ]]; then
  echo "Run install.sh from a complete cloned YT Uploader repository." >&2
  exit 1
fi
if [[ ! -r /etc/os-release ]]; then echo "Cannot detect Linux distribution." >&2; exit 1; fi
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || ! "${VERSION_ID:-}" =~ ^(22\.04|24\.04)$ ]]; then
  echo "Supported operating systems: Ubuntu 22.04 and 24.04. Detected: ${PRETTY_NAME:-unknown}." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip ca-certificates curl ffmpeg fonts-montserrat

if ! getent group "$APP_GROUP" >/dev/null; then groupadd --system "$APP_GROUP"; fi
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --gid "$APP_GROUP" --home-dir "$STATE_DIR" --create-home --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o root -g root -m 0755 "$INSTALL_DIR"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$STATE_DIR" "$STATE_DIR/downloads"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$LOG_DIR"
install -d -o root -g "$APP_GROUP" -m 0750 "$ENV_DIR"

# Install from the checked-out repository, preserving local state and configuration outside the code directory.
tar --exclude='./.git' --exclude='./.venv' --exclude='./venv' --exclude='./data' --exclude='./downloads' --exclude='./logs' \
  -C "$SOURCE_DIR" -cf - . | tar -C "$INSTALL_DIR" -xf -
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

GENERATED_PASSWORD=""
if [[ ! -f "$ENV_FILE" ]]; then
  GENERATED_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  ENCRYPTION_KEY="$(python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
  cat > "$ENV_FILE" <<EOF
APP_HOST=127.0.0.1
APP_PORT=8000
BASE_URL=http://localhost:8000
COOKIE_SECURE=false
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$GENERATED_PASSWORD
SESSION_SECRET=$SESSION_SECRET
ENCRYPTION_KEY=$ENCRYPTION_KEY
DATABASE_PATH=$STATE_DIR/yt_uploader.sqlite3
DOWNLOADS_PATH=$STATE_DIR/downloads
LOG_PATH=$LOG_DIR/app.log
POLL_INTERVAL_SECONDS=300
MAX_DOWNLOAD_BYTES=4294967296
REQUEST_TIMEOUT_SECONDS=30
WORKER_ENABLED_ON_START=false
EOF
  chmod 0640 "$ENV_FILE"
  chown root:"$APP_GROUP" "$ENV_FILE"
fi
# Migrate the previous default callback host while preserving custom HTTPS/domain URLs.
if grep -qx 'BASE_URL=http://127.0.0.1:8000' "$ENV_FILE"; then
  sed -i 's|^BASE_URL=http://127\.0\.0\.1:8000$|BASE_URL=http://localhost:8000|' "$ENV_FILE"
fi

install -m 0644 "$INSTALL_DIR/deploy/yt-uploader.service" "$SERVICE_FILE"
chown -R root:root "$INSTALL_DIR"
chmod -R u=rwX,go=rX "$INSTALL_DIR"
chown -R "$APP_USER":"$APP_GROUP" "$STATE_DIR" "$LOG_DIR"
systemctl daemon-reload
systemctl enable --now "$APP_NAME.service"
# Reload the newly copied application code even when the service was already active.
systemctl restart "$APP_NAME.service"

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! curl -fsS http://127.0.0.1:8000/health; then
  echo "Service did not become healthy. Inspect: sudo journalctl -u $APP_NAME -n 100 --no-pager" >&2
  exit 1
fi

echo
echo "YT Uploader installed and running as a loopback-only service."
echo "Dashboard: http://127.0.0.1:8000"
echo "Remote access from your workstation: ssh -L 8000:127.0.0.1:8000 <user>@<server-ip>"
if [[ -n "$GENERATED_PASSWORD" ]]; then
  echo "Initial login username: admin"
  echo "Initial login password: $GENERATED_PASSWORD"
  echo "Save this password now; it will not be printed again."
else
  echo "Existing environment file preserved: $ENV_FILE"
fi
echo "Next: add Google OAuth and provider credentials in Settings; keep automation paused until you validate the configuration."
