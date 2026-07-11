#!/usr/bin/env bash
set -euo pipefail

umask 027

BACKUP_ROOT="${PI4_BACKUP_ROOT:-/mnt/ssd/backups/pi4}"
STATE_DIR="${PI4_BACKUP_STATE_DIR:-/var/lib/pi4-backup}"
REMOTE="${PI4_BACKUP_REMOTE:-pi5@192.168.0.94:/home/pi5/backups/pi4}"
LOCAL_RETENTION_DAYS="${PI4_BACKUP_LOCAL_RETENTION_DAYS:-14}"
REMOTE_RETENTION_DAYS="${PI4_BACKUP_REMOTE_RETENTION_DAYS:-30}"
K3S_DATA_DIR="${K3S_DATA_DIR:-/mnt/ssd/k3s}"
STAMP="$(date +%Y%m%dT%H%M%S%z)"
BACKUP_ID="pi4-backup-${STAMP}"

if [[ "$(id -u)" != "0" ]]; then
  echo "pi4-backup must run as root" >&2
  exit 1
fi

install -d -o root -g pi4 -m 0750 "$BACKUP_ROOT" "$STATE_DIR"
WORK_DIR="$(mktemp -d "${BACKUP_ROOT}/.work-${STAMP}.XXXXXX")"
PAYLOAD="${WORK_DIR}/${BACKUP_ID}"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

mkdir -p "$PAYLOAD"

log() {
  printf '%s %s\n' "$(date -Is)" "$*"
}

warn() {
  printf '%s WARN %s\n' "$(date -Is)" "$*" >&2
}

fail() {
  printf '%s ERROR %s\n' "$(date -Is)" "$*" >&2
  exit 1
}

copy_if_exists() {
  local source="$1"
  if [[ -e "$source" ]]; then
    rsync -a --numeric-ids --relative "$source" "$PAYLOAD"/
  else
    warn "missing ${source}"
  fi
}

copy_required() {
  local source="$1"
  [[ -e "$source" ]] || fail "required backup source is missing: ${source}"
  rsync -a --numeric-ids --relative "$source" "$PAYLOAD"/
}

copy_adguard_state() {
  copy_required /opt/AdGuardHome/AdGuardHome.yaml
  copy_if_exists /opt/AdGuardHome/data/filters
  copy_if_exists /opt/AdGuardHome/data/sessions.db
  copy_if_exists /opt/AdGuardHome/data/stats.db
}

copy_k3s_datastore() {
  local db="${K3S_DATA_DIR}/server/db/state.db"
  local db_dest="${PAYLOAD}${K3S_DATA_DIR}/server/db"
  mkdir -p "$db_dest"
  if [[ -f "$db" && -x /usr/bin/sqlite3 ]]; then
    /usr/bin/sqlite3 "$db" ".backup '${db_dest}/state.db'"
  elif [[ -f "$db" ]]; then
    fail "sqlite3 is required for a consistent k3s datastore backup"
  else
    fail "missing k3s sqlite datastore at ${db}"
  fi
  [[ "$(/usr/bin/sqlite3 "${db_dest}/state.db" 'PRAGMA quick_check;')" == "ok" ]] || fail "backed-up k3s datastore failed quick_check"
}

copy_kuma_state() {
  local source="/mnt/ssd/podman/uptime-kuma-data"
  local db="${source}/kuma.db"
  local destination="${PAYLOAD}${source}"
  [[ -f "$db" ]] || fail "missing Uptime Kuma database at ${db}"
  [[ -x /usr/bin/sqlite3 ]] || fail "sqlite3 is required for a consistent Uptime Kuma backup"
  mkdir -p "$destination"
  rsync -a --numeric-ids \
    --exclude=/kuma.db \
    --exclude=/kuma.db-wal \
    --exclude=/kuma.db-shm \
    "${source}/" "${destination}/"
  /usr/bin/sqlite3 "$db" ".backup '${destination}/kuma.db'"
  [[ "$(/usr/bin/sqlite3 "${destination}/kuma.db" 'PRAGMA quick_check;')" == "ok" ]] || fail "backed-up Uptime Kuma database failed quick_check"
}

write_inventory() {
  local inv="${PAYLOAD}/inventory"
  mkdir -p "$inv"
  {
    echo "backup_id=${BACKUP_ID}"
    echo "created_at=$(date -Is)"
    echo "hostname=$(hostname)"
    echo "kernel=$(uname -a)"
  } > "${PAYLOAD}/manifest.txt"
  chmod 0640 "${PAYLOAD}/manifest.txt"

  df -h > "${inv}/df-h.txt" 2>&1 || true
  df -Pi > "${inv}/df-inodes.txt" 2>&1 || true
  lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS,MODEL,TRAN > "${inv}/lsblk.txt" 2>&1 || true
  findmnt / /mnt/ssd /mnt/nas/brain > "${inv}/findmnt.txt" 2>&1 || true
  systemctl is-active k3s pi4-noc AdGuardHome tailscaled smbd nmbd mnt-nas-brain.mount > "${inv}/systemd-active.txt" 2>&1 || true
  vcgencmd get_throttled > "${inv}/vcgencmd-get-throttled.txt" 2>&1 || true
  vcgencmd measure_temp > "${inv}/vcgencmd-measure-temp.txt" 2>&1 || true
  ss -tuln > "${inv}/ss-tuln.txt" 2>&1 || true
}

write_k3s_exports() {
  local out="${PAYLOAD}/k3s-export"
  mkdir -p "$out"
  KUBECTL="/usr/local/bin/kubectl"
  if [[ ! -x "$KUBECTL" ]]; then
    KUBECTL="/usr/bin/kubectl"
  fi
  if [[ -x "$KUBECTL" ]]; then
    "$KUBECTL" get nodes,pods,svc,pv,pvc -A -o wide > "${out}/status-wide.txt" 2>&1 || true
    "$KUBECTL" get deploy,statefulset,daemonset,svc,cm,secret,pv,pvc,ingress -A -o yaml > "${out}/resources.yaml" 2>&1 || true
    "$KUBECTL" get events -A --sort-by=.lastTimestamp > "${out}/events.txt" 2>&1 || true
    chmod 0640 "${out}/resources.yaml" || true
  else
    warn "kubectl not found; skipping k3s resource export"
  fi
}

sync_remote() {
  local archive="$1"
  local checksum="$2"
  [[ -n "$REMOTE" ]] || return 0
  if [[ "$REMOTE" != *:* ]]; then
    warn "PI4_BACKUP_REMOTE must be host:path, got ${REMOTE}"
    return 0
  fi
  local remote_host="${REMOTE%%:*}"
  local remote_path="${REMOTE#*:}"
  sudo -u pi4 ssh -o BatchMode=yes -o ConnectTimeout=8 "$remote_host" "install -d -m 0700 '$remote_path'"
  sudo -u pi4 rsync -a --chmod=F600,D700 "$archive" "$checksum" "${REMOTE%/}/"
  sudo -u pi4 ssh -o BatchMode=yes -o ConnectTimeout=8 "$remote_host" \
    "find '$remote_path' -type f \\( -name 'pi4-backup-*.tgz' -o -name 'pi4-backup-*.tgz.sha256' \\) -mtime +${REMOTE_RETENTION_DAYS} -delete"
}

log "collecting ${BACKUP_ID}"
write_inventory
write_k3s_exports

copy_k3s_datastore
copy_required "${K3S_DATA_DIR}/server/token"
copy_required "${K3S_DATA_DIR}/server/cred"
copy_required "${K3S_DATA_DIR}/server/tls"
copy_required "${K3S_DATA_DIR}/server/manifests"
copy_required /etc/rancher/k3s
copy_required /etc/systemd/system/k3s.service.d
copy_required /etc/systemd/system/mnt-nas-brain.mount
copy_required /etc/systemd/system/pi4-noc.service
copy_required /etc/samba
copy_adguard_state
copy_required /mnt/nas/brain
copy_required /var/lib/grid
copy_required /mnt/ssd/registry
copy_kuma_state

find "$PAYLOAD" -type d -exec chmod 0750 {} +
find "$PAYLOAD" -type f -exec chmod g+r,o-rwx {} +

ARCHIVE="${BACKUP_ROOT}/${BACKUP_ID}.tgz"
CHECKSUM="${ARCHIVE}.sha256"
tar -C "$WORK_DIR" -czf "$ARCHIVE" "$BACKUP_ID"
chmod 0640 "$ARCHIVE"
chown root:pi4 "$ARCHIVE"
(cd "$BACKUP_ROOT" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$CHECKSUM")")
chmod 0640 "$CHECKSUM"
chown root:pi4 "$CHECKSUM"
tar -tzf "$ARCHIVE" >/dev/null

sync_remote "$ARCHIVE" "$CHECKSUM"

find "$BACKUP_ROOT" -maxdepth 1 -type f \( -name 'pi4-backup-*.tgz' -o -name 'pi4-backup-*.tgz.sha256' \) -mtime +"$LOCAL_RETENTION_DAYS" -delete

cat > "${STATE_DIR}/last-backup.json" <<EOF
{"backup_id":"${BACKUP_ID}","created_at":"$(date -Is)","archive":"${ARCHIVE}","remote":"${REMOTE}"}
EOF
chmod 0640 "${STATE_DIR}/last-backup.json"
chown root:pi4 "${STATE_DIR}/last-backup.json"

log "created ${ARCHIVE}"
