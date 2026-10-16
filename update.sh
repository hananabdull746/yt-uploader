#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run with sudo: sudo ./update.sh" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/yt-uploader"

echo "==> Fetching and syncing latest code from GitHub..."
git config --global --add safe.directory "$SOURCE_DIR" || true
git -C "$SOURCE_DIR" fetch origin main
git -C "$SOURCE_DIR" reset --hard origin/main

echo "==> Ensuring system packages (ffmpeg, fonts) are installed..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null 2>&1 || true
apt-get install -y ffmpeg fonts-montserrat >/dev/null 2>&1 || true

echo "==> Syncing updated code into $INSTALL_DIR..."
tar --exclude='./.git' --exclude='./.venv' --exclude='./venv' --exclude='./data' --exclude='./downloads' --exclude='./logs' \
  -C "$SOURCE_DIR" -cf - . | tar -C "$INSTALL_DIR" -xf -

echo "==> Updating Python dependencies..."
if [[ -f "$INSTALL_DIR/venv/bin/pip" ]]; then
  "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
fi

echo "==> Updating file permissions..."
chown -R root:root "$INSTALL_DIR"
chmod -R u=rwX,go=rX "$INSTALL_DIR"

if [[ -n "${SUDO_USER:-}" ]]; then
  chown -R "$SUDO_USER:$SUDO_USER" "$SOURCE_DIR" || true
fi

echo "==> Restarting yt-uploader service..."
systemctl daemon-reload
systemctl restart yt-uploader.service

echo "==> Waiting for service health check..."
sleep 2
if systemctl is-active --quiet yt-uploader.service; then
  echo "=========================================================="
  echo " [SUCCESS] yt-uploader updated and restarted successfully!"
  echo " Current commit: $(git -C "$SOURCE_DIR" log -1 --oneline)"
  echo " Please hard refresh your browser (Ctrl + Shift + R)"
  echo "=========================================================="
else
  echo "[WARNING] Service may not have started properly. Checking logs:"
  journalctl -u yt-uploader.service -n 35 --no-pager
fi