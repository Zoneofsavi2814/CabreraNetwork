#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
  echo "usage: sudo $0 /path/to/pi3-probe-key.pub" >&2
}

[[ "${EUID}" -eq 0 ]] || { echo "install-pi4-ops-probe must run as root" >&2; exit 1; }
[[ "$#" -eq 1 ]] || { usage; exit 64; }

PUBLIC_KEY_FILE="$1"
[[ -f "$PUBLIC_KEY_FILE" && ! -L "$PUBLIC_KEY_FILE" ]] || { echo "public key file not found or is not regular" >&2; exit 66; }

KEY_TYPE="$(awk 'NR == 1 {print $1}' "$PUBLIC_KEY_FILE")"
KEY_BLOB="$(awk 'NR == 1 {print $2}' "$PUBLIC_KEY_FILE")"
KEY_FIELDS="$(awk 'NR == 1 {print NF}' "$PUBLIC_KEY_FILE")"
[[ "$KEY_FIELDS" -ge 2 && "$KEY_TYPE" == "ssh-ed25519" && "$KEY_BLOB" =~ ^[A-Za-z0-9+/=]+$ ]] || {
  echo "the probe key must be a plain ssh-ed25519 public key" >&2
  exit 65
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

if id pi4probe >/dev/null 2>&1; then
  [[ "$(getent passwd pi4probe | cut -d: -f7)" == "/bin/sh" ]] || {
    echo "existing pi4probe account has an unexpected shell" >&2
    exit 65
  }
else
  /usr/sbin/useradd --system --no-create-home --home-dir /nonexistent --shell /bin/sh pi4probe
fi
/usr/sbin/usermod --lock pi4probe

/usr/bin/install -d -o root -g root -m 0755 /usr/local/libexec /etc/ssh/sshd_config.d /etc/ssh/authorized_keys
/usr/bin/install -o root -g root -m 0755 "$SCRIPT_DIR/pi4-ops-probe.py" /usr/local/libexec/pi4-ops-probe
/usr/bin/install -o root -g root -m 0755 "$SCRIPT_DIR/pi4-ops-probe-ssh" /usr/local/libexec/pi4-ops-probe-ssh

AUTHORIZED_KEYS=/etc/ssh/authorized_keys/pi4probe
SSHD_DROPIN=/etc/ssh/sshd_config.d/90-pi4-ops-probe.conf
ROLLBACK_DIR="$(mktemp -d)"
AUTHORIZED_KEYS_EXISTED=0
SSHD_DROPIN_EXISTED=0
SSH_MUTATED=0

cleanup_rollback() {
  /bin/rm -rf --one-file-system -- "$ROLLBACK_DIR"
}
trap cleanup_rollback EXIT

if [[ -e "$AUTHORIZED_KEYS" || -L "$AUTHORIZED_KEYS" ]]; then
  [[ -f "$AUTHORIZED_KEYS" && ! -L "$AUTHORIZED_KEYS" ]] || { echo "existing probe authorized-key target is unsafe" >&2; exit 65; }
  /bin/cp -a -- "$AUTHORIZED_KEYS" "$ROLLBACK_DIR/authorized_keys.previous"
  AUTHORIZED_KEYS_EXISTED=1
fi
if [[ -e "$SSHD_DROPIN" || -L "$SSHD_DROPIN" ]]; then
  [[ -f "$SSHD_DROPIN" && ! -L "$SSHD_DROPIN" ]] || { echo "existing probe sshd drop-in target is unsafe" >&2; exit 65; }
  /bin/cp -a -- "$SSHD_DROPIN" "$ROLLBACK_DIR/sshd_dropin.previous"
  SSHD_DROPIN_EXISTED=1
fi

/usr/bin/install -o root -g root -m 0440 "$SCRIPT_DIR/pi4-ops-probe.sudoers" "$ROLLBACK_DIR/sudoers.new"
/usr/sbin/visudo -cf "$ROLLBACK_DIR/sudoers.new" >/dev/null
/usr/bin/install -o root -g root -m 0440 "$ROLLBACK_DIR/sudoers.new" /etc/sudoers.d/pi4-ops-probe
printf 'from="192.168.0.142",restrict %s %s pi3-ops-probe\n' "$KEY_TYPE" "$KEY_BLOB" \
  | /usr/bin/install -o root -g root -m 0600 /dev/stdin "$ROLLBACK_DIR/authorized_keys.new"
/usr/bin/install -o root -g root -m 0644 "$SCRIPT_DIR/pi4-ops-probe.sshd_config" "$ROLLBACK_DIR/sshd_dropin.new"

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

SSH_MUTATED=1
/usr/bin/install -o root -g root -m 0600 "$ROLLBACK_DIR/authorized_keys.new" "$AUTHORIZED_KEYS"
/usr/bin/install -o root -g root -m 0644 "$ROLLBACK_DIR/sshd_dropin.new" "$SSHD_DROPIN"
/usr/sbin/sshd -t
/bin/systemctl reload ssh.service

SSH_MUTATED=0
trap - ERR
cleanup_rollback
trap - EXIT

echo "Pi4 operations probe installed for the Pi3 source address 192.168.0.142."
echo "Pin and verify this host key on Pi3 before connecting:"
/usr/bin/ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
