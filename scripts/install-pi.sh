#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-/opt/pi4-noc}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRET_DIR="${PI4_NOC_SECRET_DIR:-/etc/pi4-noc}"
SECRET_FILE="${PI4_NOC_SESSION_SECRET_FILE:-${SECRET_DIR}/session-secret}"
STATE_DIR="${PI4_NOC_STATE_DIR:-/var/lib/pi4-noc}"

if [[ ! -f "$SRC_DIR/dist/index.html" ]]; then
  echo "dist/index.html is missing; run npm run build before installing" >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3-pamela

sudo mkdir -p "$APP_DIR"
sudo rsync -a --delete \
  --exclude node_modules \
  --exclude .vite \
  --exclude 'Pi4 NOC Dashboard.html' \
  "$SRC_DIR"/ "$APP_DIR"/

sudo chown -R root:root "$APP_DIR"
sudo find "$APP_DIR" -type d -exec chmod 0755 {} +
sudo find "$APP_DIR" -type f -exec chmod 0644 {} +
sudo chmod 0755 "$APP_DIR/server/app.py" "$APP_DIR/server/sudo_ops.py" "$APP_DIR/scripts/install-pi.sh"

sudo install -d -o root -g pi4 -m 0750 "$SECRET_DIR"
if [[ ! -f "$SECRET_FILE" ]]; then
  python3 - <<'PY' | sudo tee "$SECRET_FILE" >/dev/null
import secrets
print(secrets.token_urlsafe(48))
PY
fi
sudo chown root:pi4 "$SECRET_FILE"
sudo chmod 0640 "$SECRET_FILE"
sudo install -d -o pi4 -g pi4 -m 0750 "$STATE_DIR"
sudo install -m 0640 -o root -g pi4 "$APP_DIR/scripts/pi4-noc.notify.env.example" "$SECRET_DIR/notify.env.example"
if [[ -f "$SECRET_DIR/notify.env" ]]; then
  sudo chown root:pi4 "$SECRET_DIR/notify.env"
  sudo chmod 0640 "$SECRET_DIR/notify.env"
fi

sudo install -m 0644 "$APP_DIR/scripts/pi4-noc.service" /etc/systemd/system/pi4-noc.service
sudo install -m 0440 "$APP_DIR/scripts/pi4-noc.sudoers" /etc/sudoers.d/pi4-noc
sudo visudo -cf /etc/sudoers.d/pi4-noc

sudo systemctl daemon-reload
sudo systemctl enable pi4-noc.service
sudo systemctl restart pi4-noc.service
sudo systemctl status --no-pager --lines=20 pi4-noc.service
