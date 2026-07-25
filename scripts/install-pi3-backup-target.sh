#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
  echo "usage: sudo $0 /path/to/pi3-backup-key.pub" >&2
}

[[ "${EUID}" -eq 0 ]] || { echo "install-pi3-backup-target must run as root" >&2; exit 1; }
[[ "$#" -eq 1 ]] || { usage; exit 64; }
for dependency in /usr/bin/rrsync /usr/bin/timeout /usr/bin/flock /usr/bin/prlimit /usr/bin/find /usr/sbin/sshd; do
  [[ -x "$dependency" ]] || { echo "required executable is missing: $dependency" >&2; exit 69; }
done

MOUNT_INFO="$(/usr/bin/findmnt --target /mnt/ssd -n -o UUID,FSTYPE,OPTIONS 2>/dev/null || true)"
MOUNT_UUID="$(awk '{print $1}' <<<"$MOUNT_INFO")"
MOUNT_FSTYPE="$(awk '{print $2}' <<<"$MOUNT_INFO")"
MOUNT_OPTIONS="$(awk '{print $3}' <<<"$MOUNT_INFO")"
[[ "$MOUNT_UUID" == "b0a1a356-3c0e-4f68-9c80-3379f662b4bc" && "$MOUNT_FSTYPE" == "ext4" && ",${MOUNT_OPTIONS}," == *,rw,* ]] || {
  echo "/mnt/ssd is not the expected read-write Pi4 external HDD" >&2
  exit 69
}

PUBLIC_KEY_FILE="$1"
[[ -f "$PUBLIC_KEY_FILE" && ! -L "$PUBLIC_KEY_FILE" ]] || { echo "public key file not found or is not regular" >&2; exit 66; }
KEY_TYPE="$(awk 'NR == 1 {print $1}' "$PUBLIC_KEY_FILE")"
KEY_BLOB="$(awk 'NR == 1 {print $2}' "$PUBLIC_KEY_FILE")"
KEY_FIELDS="$(awk 'NR == 1 {print NF}' "$PUBLIC_KEY_FILE")"
[[ "$KEY_FIELDS" -ge 2 && "$KEY_TYPE" == "ssh-ed25519" && "$KEY_BLOB" =~ ^[A-Za-z0-9+/=]+$ ]] || {
  echo "the backup key must be a plain ssh-ed25519 public key" >&2
  exit 65
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

if id pi3backup >/dev/null 2>&1; then
  [[ "$(getent passwd pi3backup | cut -d: -f7)" == "/bin/sh" ]] || {
    echo "existing pi3backup account has an unexpected shell" >&2
    exit 65
  }
else
  /usr/sbin/useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /bin/sh pi3backup
fi
/usr/sbin/usermod --lock pi3backup
getent group pi3backup >/dev/null || { echo "pi3backup group is missing" >&2; exit 65; }

if id pi3verify >/dev/null 2>&1; then
  [[ "$(getent passwd pi3verify | cut -d: -f7)" == "/usr/sbin/nologin" ]] || {
    echo "existing pi3verify account has an unexpected shell" >&2
    exit 65
  }
else
  /usr/sbin/useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin pi3verify
fi
/usr/sbin/usermod --lock pi3verify
getent group pi3verify >/dev/null || { echo "pi3verify group is missing" >&2; exit 65; }

/usr/bin/install -d -o root -g root -m 0755 /usr/local/libexec /etc/ssh/sshd_config.d /etc/ssh/authorized_keys
/usr/bin/install -d -o root -g root -m 0755 /mnt/ssd/backups/pi3
/usr/bin/install -o root -g pi3backup -m 0640 /dev/null /mnt/ssd/backups/pi3/.upload.lock
/usr/bin/install -d -o root -g pi3backup -m 1770 /mnt/ssd/backups/pi3/incoming
/usr/bin/install -d -o root -g pi3verify -m 0750 /mnt/ssd/backups/pi3/processing
/usr/bin/install -d -o root -g root -m 0700 /mnt/ssd/backups/pi3/verified /mnt/ssd/backups/pi3/rejected
/usr/bin/install -d -o root -g pi3verify -m 0730 /mnt/ssd/backups/pi3/restore-check
/usr/bin/install -d -o root -g pi3verify -m 0750 /var/lib/pi3-backup
/usr/bin/install -d -o root -g pi3verify -m 0730 /var/lib/pi3-backup/results
/usr/bin/install -o root -g root -m 0755 "$SCRIPT_DIR/pi3-backup-receiver-ssh" /usr/local/libexec/pi3-backup-receiver-ssh
/usr/bin/install -o root -g root -m 0755 "$SCRIPT_DIR/pi3-backup-claim.py" /usr/local/libexec/pi3-backup-claim
/usr/bin/install -o root -g root -m 0755 "$SCRIPT_DIR/pi3-backup-verify.py" /usr/local/libexec/pi3-backup-verify
/usr/bin/install -o root -g root -m 0644 "$SCRIPT_DIR/pi3-backup-verify.service" /etc/systemd/system/pi3-backup-verify.service
/usr/bin/install -o root -g root -m 0644 "$SCRIPT_DIR/pi3-backup-verify.timer" /etc/systemd/system/pi3-backup-verify.timer

AUTHORIZED_KEYS=/etc/ssh/authorized_keys/pi3backup
SSHD_DROPIN=/etc/ssh/sshd_config.d/91-pi3-backup-target.conf
ROLLBACK_DIR="$(mktemp -d)"
AUTHORIZED_KEYS_EXISTED=0
SSHD_DROPIN_EXISTED=0
SSH_MUTATED=0

cleanup_rollback() {
  /bin/rm -rf --one-file-system -- "$ROLLBACK_DIR"
}
trap cleanup_rollback EXIT

if [[ -e "$AUTHORIZED_KEYS" || -L "$AUTHORIZED_KEYS" ]]; then
  [[ -f "$AUTHORIZED_KEYS" && ! -L "$AUTHORIZED_KEYS" ]] || { echo "existing backup authorized-key target is unsafe" >&2; exit 65; }
  /bin/cp -a -- "$AUTHORIZED_KEYS" "$ROLLBACK_DIR/authorized_keys.previous"
  AUTHORIZED_KEYS_EXISTED=1
fi
if [[ -e "$SSHD_DROPIN" || -L "$SSHD_DROPIN" ]]; then
  [[ -f "$SSHD_DROPIN" && ! -L "$SSHD_DROPIN" ]] || { echo "existing backup sshd drop-in target is unsafe" >&2; exit 65; }
  /bin/cp -a -- "$SSHD_DROPIN" "$ROLLBACK_DIR/sshd_dropin.previous"
  SSHD_DROPIN_EXISTED=1
fi

rollback_ssh() {
  local status=$?
  trap - ERR
  set +e
  if [[ "$SSH_MUTATED" -eq 1 ]]; then
    if [[ "$AUTHORIZED_KEYS_EXISTED" -eq 1 ]]; then
      /bin/cp -a -- "$ROLLBACK_DIR/authorized_keys.previous" "$AUTHORIZED_KEYS"
    else
      /usr/bin/unlink "$AUTHORIZED_KEYS" 2>/dev/null || true
    fi
    if [[ "$SSHD_DROPIN_EXISTED" -eq 1 ]]; then
      /bin/cp -a -- "$ROLLBACK_DIR/sshd_dropin.previous" "$SSHD_DROPIN"
    else
      /usr/bin/unlink "$SSHD_DROPIN" 2>/dev/null || true
    fi
    /usr/sbin/sshd -t && /bin/systemctl reload ssh.service || true
  fi
  cleanup_rollback
  exit "$status"
}
trap rollback_ssh ERR

printf 'from="192.168.0.142",restrict %s %s pi3-backup-target\n' "$KEY_TYPE" "$KEY_BLOB" \
  | /usr/bin/install -o root -g root -m 0600 /dev/stdin "$ROLLBACK_DIR/authorized_keys.new"
/usr/bin/install -o root -g root -m 0644 "$SCRIPT_DIR/pi3-backup-target.sshd_config" "$ROLLBACK_DIR/sshd_dropin.new"

SSH_MUTATED=1
/usr/bin/install -o root -g root -m 0600 "$ROLLBACK_DIR/authorized_keys.new" "$AUTHORIZED_KEYS"
/usr/bin/install -o root -g root -m 0644 "$ROLLBACK_DIR/sshd_dropin.new" "$SSHD_DROPIN"
/usr/sbin/sshd -t
/bin/systemctl reload ssh.service
/bin/systemctl daemon-reload
/bin/systemctl enable pi3-backup-verify.service
/bin/systemctl enable --now pi3-backup-verify.timer

SSH_MUTATED=0
trap - ERR
cleanup_rollback
trap - EXIT

echo "Restricted Pi3 backup target installed with claim/verify/finalize isolation."
echo "Pin and verify this host key on Pi3 before connecting:"
/usr/bin/ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
