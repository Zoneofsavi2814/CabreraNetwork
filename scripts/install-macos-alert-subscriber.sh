#!/usr/bin/env bash
set -euo pipefail

LABEL="com.cabrera.alerts.ntfy"
ROOT="${HOME}/Library/Application Support/Cabrera Alerts"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3)"
TOPIC_URL="${NTFY_TOPIC_URL:-}"

if [[ -z "$TOPIC_URL" ]]; then
  echo "NTFY_TOPIC_URL is required" >&2
  exit 1
fi
"$PYTHON" "$SRC_DIR/cabrera-alerts-subscriber.py" --validate-url "$TOPIC_URL"

install -d -m 0700 "$ROOT" "${HOME}/Library/LaunchAgents"
install -m 0700 "$SRC_DIR/cabrera-alerts-subscriber.py" "$ROOT/subscriber.py"
umask 077
printf 'NTFY_TOPIC_URL=%s\n' "$TOPIC_URL" > "$ROOT/ntfy.env"
chmod 0600 "$ROOT/ntfy.env"

launchctl bootout "gui/${UID}/${LABEL}" >/dev/null 2>&1 || true
plutil -create xml1 "$PLIST"
/usr/libexec/PlistBuddy -c "Add :Label string ${LABEL}" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string ${PYTHON}" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string ${ROOT}/subscriber.py" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:2 string --config" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :ProgramArguments:3 string ${ROOT}/ntfy.env" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :RunAtLoad bool true" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :KeepAlive bool true" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :ThrottleInterval integer 15" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :ProcessType string Background" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardOutPath string ${ROOT}/launch.out.log" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :StandardErrorPath string ${ROOT}/launch.err.log" "$PLIST"
chmod 0600 "$PLIST"
plutil -lint "$PLIST"
launchctl bootstrap "gui/${UID}" "$PLIST"
launchctl enable "gui/${UID}/${LABEL}"
launchctl kickstart -k "gui/${UID}/${LABEL}"
