#!/usr/bin/env bash
set -euo pipefail

umask 027

BACKUP_ROOT="${PI4_BACKUP_ROOT:-/mnt/ssd/backups/pi4}"
STATE_DIR="${PI4_BACKUP_STATE_DIR:-/var/lib/pi4-backup}"
DRILL_ROOT="${PI4_RESTORE_DRILL_ROOT:-/mnt/ssd/restore-drills/pi4}"
DRILL_RETENTION_DAYS="${PI4_RESTORE_DRILL_RETENTION_DAYS:-45}"
APP_IMAGE_DIR="${PI4_APP_IMAGE_DIR:-/mnt/ssd/backups/application-images}"

if [[ "$(id -u)" != "0" ]]; then
  echo "pi4-restore-drill must run as root" >&2
  exit 1
fi
install -d -o root -g pi4 -m 0750 "$STATE_DIR" "$DRILL_ROOT"

latest="$(find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'pi4-backup-*.tgz' -printf '%T@ %p\n' | sort -nr | awk 'NR==1 {print $2}')"
if [[ -z "$latest" ]]; then
  echo "no local Pi4 backup archives found" >&2
  exit 2
fi
checksum="${latest}.sha256"
test -s "$checksum"
(cd "$(dirname "$latest")" && sha256sum -c "$(basename "$checksum")")
archive_base="$(basename "$latest" .tgz)"
drill_dir="${DRILL_ROOT}/${archive_base}-restore-$(date +%Y%m%dT%H%M%S%z)"
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
test -s "${root}/mnt/ssd/apps/eagleeye/eagleeye.db"
test -s "${root}/var/lib/cabrera-alert-relay/outbox.sqlite3"
test -s "${root}/var/lib/cabrera-portfolio/portfolio.local.json"
test -s "${root}/var/lib/cabrera-portfolio/plaid.items.json"
test -s "${root}/var/lib/cabrera-portfolio/manual-overlay.local.json"
test -s "${root}/var/lib/cabrera-portfolio/manual-portal-dashboard-values.json"
test -s "${root}/etc/cabrera-portfolio/portfolio.env"
test -s "${root}/etc/cabrera-programs/session-secret"
test -e "${root}/etc/cabrera-programs/.pi4-initialized"
test -s "${root}/home/pi4/.kube/cabrera-programs.yaml"
test -s "${root}/etc/systemd/system/cabrera-portfolio.service"
test -s "${root}/etc/systemd/system/cabrera-programs.service"
test "$(stat -c %a "${root}/etc/cabrera-portfolio/portfolio.env")" = 640
test "$(stat -c %a "${root}/etc/cabrera-programs/session-secret")" = 640
case "$(stat -c %a "${root}/home/pi4/.kube/cabrera-programs.yaml")" in
  600|640) ;;
  *) echo "restored CabreraPrograms kubeconfig is not restricted" >&2; exit 3 ;;
esac
test -d "${root}/opt/cabrera-apps/manifests"
test -s "${root}/mnt/ssd/backups/application-images/retained-k3s-images.tar.gz.sha256"
find "${root}/mnt/nas/brain" -type f -name "*.md" -print -quit | grep -q .
k3s_integrity="$(sqlite3 "${root}/mnt/ssd/k3s/server/db/state.db" "PRAGMA quick_check;")"
kuma_integrity="$(sqlite3 "${root}/mnt/ssd/podman/uptime-kuma-data/kuma.db" "PRAGMA quick_check;")"
eagleeye_integrity="$(sqlite3 "${root}/mnt/ssd/apps/eagleeye/eagleeye.db" "PRAGMA quick_check;")"
relay_integrity="$(sqlite3 "${root}/var/lib/cabrera-alert-relay/outbox.sqlite3" "PRAGMA quick_check;")"
test "$k3s_integrity" = ok
test "$kuma_integrity" = ok
test "$eagleeye_integrity" = ok
test "$relay_integrity" = ok
python3 -m json.tool "${root}/var/lib/cabrera-portfolio/portfolio.local.json" >/dev/null
python3 -m json.tool "${root}/var/lib/cabrera-portfolio/plaid.items.json" >/dev/null
python3 -m json.tool "${root}/var/lib/cabrera-portfolio/manual-overlay.local.json" >/dev/null
python3 -m json.tool "${root}/var/lib/cabrera-portfolio/manual-portal-dashboard-values.json" >/dev/null
portfolio_integrity=ok
programs_integrity=ok
(cd "$APP_IMAGE_DIR" && sha256sum -c "${root}/mnt/ssd/backups/application-images/retained-k3s-images.tar.gz.sha256")
kuma_monitors="$(sqlite3 "${root}/mnt/ssd/podman/uptime-kuma-data/kuma.db" "SELECT count(*) FROM monitor;")"
printf "{\"archive\":\"%s\",\"drill_dir\":\"%s\",\"checked_at\":\"%s\",\"k3s_integrity\":\"ok\",\"kuma_integrity\":\"ok\",\"eagleeye_integrity\":\"ok\",\"relay_integrity\":\"ok\",\"portfolio_integrity\":\"%s\",\"programs_integrity\":\"%s\",\"kuma_monitors\":%s,\"status\":\"ok\"}\n" "$(basename "$latest")" "$drill_dir" "$(date -Is)" "$portfolio_integrity" "$programs_integrity" "$kuma_monitors" > "${drill_dir}/RESTORE_DRILL_OK.json"
find "$DRILL_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$DRILL_RETENTION_DAYS" -exec rm -rf {} +

install -m 0640 -o root -g pi4 "${drill_dir}/RESTORE_DRILL_OK.json" "${STATE_DIR}/last-restore-drill.json"
chmod 0640 "${STATE_DIR}/last-restore-drill.json"
chown root:pi4 "${STATE_DIR}/last-restore-drill.json"
cat "${drill_dir}/RESTORE_DRILL_OK.json"
