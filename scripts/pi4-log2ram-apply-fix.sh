#!/usr/bin/env bash
set -euo pipefail

TARGET="${LOG2RAM_BIN:-/usr/local/bin/log2ram}"
MARKER='optional_params+=("--delete-excluded")'

if [[ "$(id -u)" != "0" ]]; then
  echo "pi4-log2ram-apply-fix must run as root" >&2
  exit 1
fi
if [[ ! -x "$TARGET" ]]; then
  echo "log2ram is not installed at ${TARGET}; skipping" >&2
  exit 0
fi
if grep -Fq "$MARKER" "$TARGET"; then
  bash -n "$TARGET"
  exit 0
fi

stamp="$(date +%Y%m%dT%H%M%S%z)"
install -d -o root -g root -m 0700 /var/backups/pi4-log2ram
install -o root -g root -m 0755 "$TARGET" "/var/backups/pi4-log2ram/log2ram-${stamp}"
sed -i '/optional_params+=("--exclude=journal\/\*\/\*")/a\        optional_params+=("--delete-excluded")' "$TARGET"
grep -Fq "$MARKER" "$TARGET"
bash -n "$TARGET"

if systemctl is-active --quiet log2ram.service; then
  journalctl --sync
  systemctl reload log2ram.service
fi
