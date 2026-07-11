#!/usr/bin/env bash
set -euo pipefail

umask 027

REMOTE="${PI4_BACKUP_REMOTE:-pi5@192.168.0.94:/home/pi5/backups/pi4}"
STATE_DIR="${PI4_BACKUP_STATE_DIR:-/var/lib/pi4-backup}"
DRILL_ROOT="${PI4_RESTORE_DRILL_ROOT:-/home/pi5/restore-drills/pi4}"
DRILL_RETENTION_DAYS="${PI4_RESTORE_DRILL_RETENTION_DAYS:-45}"

if [[ "$(id -u)" != "0" ]]; then
  echo "pi4-restore-drill must run as root" >&2
  exit 1
fi
if [[ "$REMOTE" != *:* ]]; then
  echo "PI4_BACKUP_REMOTE must be host:path, got ${REMOTE}" >&2
  exit 1
fi

install -d -o root -g pi4 -m 0750 "$STATE_DIR"

REMOTE_HOST="${REMOTE%%:*}"
REMOTE_PATH="${REMOTE#*:}"
REMOTE_SCRIPT='
set -euo pipefail
backup_dir="$1"
drill_root="$2"
retention_days="$3"
latest="$(find "$backup_dir" -maxdepth 1 -type f -name "pi4-backup-*.tgz" -printf "%T@ %p\n" | sort -nr | awk "NR==1 {print \$2}")"
if [[ -z "$latest" ]]; then
  echo "no remote pi4 backup archives found" >&2
  exit 2
fi
checksum="${latest}.sha256"
test -s "$checksum"
(cd "$(dirname "$latest")" && sha256sum -c "$(basename "$checksum")")
archive_base="$(basename "$latest" .tgz)"
drill_dir="${drill_root}/${archive_base}-restore"
rm -rf "$drill_dir"
install -d -m 0700 "$drill_dir"
tar -xzf "$latest" -C "$drill_dir"
root="${drill_dir}/${archive_base}"
test -s "${root}/manifest.txt"
test -d "${root}/mnt/nas/brain"
test -d "${root}/var/lib/grid"
test -s "${root}/mnt/ssd/k3s/server/db/state.db"
test -s "${root}/mnt/ssd/k3s/server/token"
test -d "${root}/etc/rancher/k3s"
test -s "${root}/opt/AdGuardHome/AdGuardHome.yaml"
test -s "${root}/mnt/ssd/podman/uptime-kuma-data/kuma.db"
find "${root}/mnt/nas/brain" -type f -name "*.md" -print -quit | grep -q .
k3s_integrity="$(sqlite3 "${root}/mnt/ssd/k3s/server/db/state.db" "PRAGMA quick_check;")"
kuma_integrity="$(sqlite3 "${root}/mnt/ssd/podman/uptime-kuma-data/kuma.db" "PRAGMA quick_check;")"
test "$k3s_integrity" = ok
test "$kuma_integrity" = ok
kuma_monitors="$(sqlite3 "${root}/mnt/ssd/podman/uptime-kuma-data/kuma.db" "SELECT count(*) FROM monitor;")"
printf "{\"archive\":\"%s\",\"drill_dir\":\"%s\",\"checked_at\":\"%s\",\"k3s_integrity\":\"ok\",\"kuma_integrity\":\"ok\",\"kuma_monitors\":%s,\"status\":\"ok\"}\n" "$(basename "$latest")" "$drill_dir" "$(date -Is)" "$kuma_monitors" > "${drill_dir}/RESTORE_DRILL_OK.json"
find "$drill_root" -mindepth 1 -maxdepth 1 -type d -mtime +"$retention_days" -exec rm -rf {} +
cat "${drill_dir}/RESTORE_DRILL_OK.json"
'

RESULT="$(sudo -u pi4 ssh -o BatchMode=yes -o ConnectTimeout=8 "$REMOTE_HOST" \
  "bash -s -- '$REMOTE_PATH' '$DRILL_ROOT' '$DRILL_RETENTION_DAYS'" <<< "$REMOTE_SCRIPT")"

JSON_RESULT="$(printf '%s\n' "$RESULT" | grep -E '^\{.*\}$' | tail -1)"
if [[ -z "$JSON_RESULT" ]]; then
  echo "restore drill did not return a JSON result" >&2
  printf '%s\n' "$RESULT" >&2
  exit 1
fi

printf '%s\n' "$JSON_RESULT" > "${STATE_DIR}/last-restore-drill.json"
chmod 0640 "${STATE_DIR}/last-restore-drill.json"
chown root:pi4 "${STATE_DIR}/last-restore-drill.json"
printf '%s\n' "$RESULT"
