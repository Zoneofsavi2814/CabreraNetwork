#!/usr/bin/env bash
set -euo pipefail

TARGET="${LOG2RAM_BIN:-/usr/local/bin/log2ram}"
MIRROR="${LOG2RAM_MIRROR:-/var/hdd.log}"
MARKER='optional_params+=("--delete-excluded")'

[[ -x "$TARGET" ]]
grep -Fq "$MARKER" "$TARGET"
bash -n "$TARGET"

machine_id="$(cat /etc/machine-id)"
[[ ! -e "${MIRROR}/journal/${machine_id}/system.journal" ]]
[[ ! -e "${MIRROR}/journal/${machine_id}/user-1000.journal" ]]
