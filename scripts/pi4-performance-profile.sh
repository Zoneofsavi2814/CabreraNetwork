#!/usr/bin/env bash
set -euo pipefail

PROGRAM="${0##*/}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ASSET_DIR="${PI4_PROFILE_ASSET_DIR:-${REPO_DIR}/config/performance}"
ROOT="${PI4_PROFILE_ROOT:-/}"
SYSTEMCTL_BIN="${PI4_PROFILE_SYSTEMCTL:-systemctl}"

readonly PROFILE_BEGIN="# BEGIN pi4-performance-profile"
readonly PROFILE_END="# END pi4-performance-profile"
readonly CPUFREQ_CONFIG="etc/default/cpufrequtils"
readonly K3S_CONFIG="etc/rancher/k3s/config.yaml"
readonly K3S_OVERRIDE="etc/systemd/system/k3s.service.d/30-performance-profile.conf"
readonly ADGUARD_CREDS="home/pi4/.adguard-home-admin"
readonly ADGUARD_QUERYLOG_INTERVAL_MS="604800000"
readonly CPUFREQ_TEMPLATE="${ASSET_DIR}/cpufrequtils"
readonly K3S_CONFIG_TEMPLATE="${ASSET_DIR}/k3s-config.yaml"
readonly K3S_OVERRIDE_TEMPLATE="${ASSET_DIR}/k3s-execstart.conf"

readonly -a HEADLESS_UNITS=(
  lightdm.service
  wayvnc-control.service
  wayvnc.service
  display-backlight.service
  glamor-test.service
  rpi-display-backlight.service
  wpa_supplicant.service
  bluetooth.service
  hciuart.service
  samba-ad-dc.service
)

usage() {
  cat <<EOF
Usage: ${PROGRAM} audit|apply|verify

  audit   Print the current profile state without changing it.
  apply   Install the profile, stop/disable unused services, and restart k3s.
          This command never reboots the host.
  verify  Check the installed and runtime profile; return non-zero on drift.

Testing/offline overrides:
  PI4_PROFILE_ROOT=/tmp/root
  PI4_PROFILE_SYSTEMCTL=/path/to/systemctl-stub
  PI4_PROFILE_ASSET_DIR=/path/to/config/performance
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

root_path() {
  local relative="${1#/}"
  if [[ "${ROOT}" == "/" ]]; then
    printf '/%s\n' "${relative}"
  else
    printf '%s/%s\n' "${ROOT%/}" "${relative}"
  fi
}

run_systemctl() {
  if [[ "${ROOT}" == "/" ]]; then
    "${SYSTEMCTL_BIN}" "$@"
  else
    "${SYSTEMCTL_BIN}" "--root=${ROOT}" "$@"
  fi
}

unit_known() {
  local unit="$1"
  local base candidate

  for base in etc/systemd/system run/systemd/system usr/local/lib/systemd/system usr/lib/systemd/system lib/systemd/system; do
    candidate="$(root_path "${base}/${unit}")"
    if [[ -e "${candidate}" || -L "${candidate}" ]]; then
      return 0
    fi
  done

  run_systemctl list-unit-files "${unit}" --no-legend --no-pager 2>/dev/null |
    awk -v expected="${unit}" '$1 == expected { found = 1 } END { exit !found }'
}

boot_config_path() {
  local candidate
  for candidate in boot/firmware/config.txt boot/config.txt; do
    if [[ -f "$(root_path "${candidate}")" ]]; then
      root_path "${candidate}"
      return 0
    fi
  done
  return 1
}

require_assets() {
  local asset
  for asset in "${CPUFREQ_TEMPLATE}" "${K3S_CONFIG_TEMPLATE}" "${K3S_OVERRIDE_TEMPLATE}"; do
    [[ -f "${asset}" ]] || die "missing profile asset: ${asset}"
  done
}

adguard_querylog_config() {
  local mode="$1"
  local creds
  creds="${PI4_PROFILE_ADGUARD_CREDS:-$(root_path "${ADGUARD_CREDS}")}"

  if [[ ! -f "${creds}" ]]; then
    if [[ "${ROOT}" != "/" ]]; then
      printf 'skipped AdGuard query-log check in offline root (credentials absent)\n'
      return 0
    fi
    printf 'AdGuard credentials are unavailable at %s\n' "${creds}" >&2
    return 1
  fi

  command -v python3 >/dev/null || die "python3 is required for the AdGuard query-log API"
  python3 - "${mode}" "${creds}" "${ADGUARD_QUERYLOG_INTERVAL_MS}" <<'PY'
import base64
import copy
import json
import sys
from pathlib import Path
from urllib import request

mode, creds_path, target_interval = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
creds = {}
for line in creds_path.read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        creds[key.strip()] = value.strip()

username = creds.get("username")
password = creds.get("password")
base_url = creds.get("url", "http://127.0.0.1:8080").rstrip("/")
if not username or not password:
    raise SystemExit("AdGuard credential file is missing username or password")

headers = {
    "Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode(),
    "Content-Type": "application/json",
}

def api(path, method="GET", body=None):
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    req = request.Request(base_url + path, data=payload, headers=headers, method=method)
    with request.urlopen(req, timeout=5) as response:
        raw = response.read().decode()
    return json.loads(raw) if raw else {}

current = api("/control/querylog/config")
if not isinstance(current, dict) or "interval" not in current:
    raise SystemExit("AdGuard returned an invalid query-log configuration")

if mode == "audit":
    print(f"AdGuard query-log retention: {current['interval']} ms")
elif mode == "verify":
    if current.get("interval") != target_interval:
        raise SystemExit(f"AdGuard query-log retention is {current.get('interval')} ms, expected {target_interval} ms")
elif mode == "apply":
    if current.get("interval") == target_interval:
        print("AdGuard query-log retention unchanged (7 days)")
    else:
        updated = copy.deepcopy(current)
        updated["interval"] = target_interval
        api("/control/querylog/config/update", method="PUT", body=updated)
        verified = api("/control/querylog/config")
        if verified.get("interval") != target_interval:
            raise SystemExit("AdGuard did not retain the requested seven-day interval")
        for key, value in current.items():
            if key != "interval" and verified.get(key) != value:
                raise SystemExit(f"AdGuard query-log field changed unexpectedly: {key}")
        print("AdGuard query-log retention updated to 7 days")
else:
    raise SystemExit(f"unsupported AdGuard mode: {mode}")
PY
}

assert_pi4_target() {
  local model_file model
  [[ "${PI4_PROFILE_ALLOW_NON_PI:-0}" == "1" ]] && return 0

  model_file="$(root_path proc/device-tree/model)"
  if [[ -f "${model_file}" ]]; then
    model="$(tr -d '\000' < "${model_file}")"
    [[ "${model}" == *"Raspberry Pi 4"* ]] || die "refusing to apply Pi 4 profile to: ${model}"
  elif [[ "${ROOT}" == "/" ]]; then
    die "cannot confirm this host is a Raspberry Pi 4 (set PI4_PROFILE_ALLOW_NON_PI=1 to override)"
  fi
}

assert_ondemand_available() {
  local available_file available
  local -a files=()
  shopt -s nullglob
  files=("$(root_path sys/devices/system/cpu/cpufreq)"/policy*/scaling_available_governors)
  shopt -u nullglob

  for available_file in "${files[@]}"; do
    available="$(<"${available_file}")"
    [[ " ${available} " == *" ondemand "* ]] || die "ondemand governor is unavailable for ${available_file}"
  done
}

install_if_changed() {
  local source="$1"
  local destination="$2"
  local mode="$3"

  if [[ -f "${destination}" ]] && cmp -s "${source}" "${destination}"; then
    printf 'unchanged %s\n' "${destination}"
    return 0
  fi

  mkdir -p "$(dirname "${destination}")"
  install -m "${mode}" "${source}" "${destination}"
  printf 'installed %s\n' "${destination}"
}

validate_managed_block() {
  local config="$1"
  local begin_count end_count
  begin_count="$(grep -Fxc "${PROFILE_BEGIN}" "${config}" || true)"
  end_count="$(grep -Fxc "${PROFILE_END}" "${config}" || true)"
  if [[ "${begin_count}" -gt 1 || "${end_count}" -gt 1 || "${begin_count}" != "${end_count}" ]]; then
    die "malformed managed block in ${config}"
  fi
}

render_boot_config() {
  local source="$1"
  local destination="$2"
  local stripped
  local kms_line=""

  validate_managed_block "${source}"
  stripped="$(mktemp "${destination}.stripped.XXXXXX")"
  awk -v begin="${PROFILE_BEGIN}" -v end="${PROFILE_END}" '
    $0 == begin { managed = 1; next }
    $0 == end { managed = 0; next }
    managed { next }
    /^[[:space:]]*dtparam=audio=/ { next }
    /^[[:space:]]*camera_auto_detect=/ { next }
    /^[[:space:]]*display_auto_detect=/ { next }
    /^[[:space:]]*arm_boost=/ { next }
    /^[[:space:]]*dtoverlay=disable-wifi([,[:space:]]|$)/ { next }
    /^[[:space:]]*dtoverlay=disable-bt([,[:space:]]|$)/ { next }
    { print }
  ' "${source}" > "${stripped}"

  if ! grep -Eq '^[[:space:]]*dtoverlay=vc4-kms-v3d([,[:space:]]|$)' "${stripped}"; then
    kms_line="dtoverlay=vc4-kms-v3d"
  fi

  awk 'NF { last = NR } { line[NR] = $0 } END { for (i = 1; i <= last; i++) print line[i] }' "${stripped}" > "${destination}"
  rm -f "${stripped}"

  {
    printf '\n%s\n' "${PROFILE_BEGIN}"
    printf '[all]\n'
    printf 'dtparam=audio=off\n'
    printf 'camera_auto_detect=0\n'
    printf 'display_auto_detect=0\n'
    printf 'arm_boost=1\n'
    [[ -z "${kms_line}" ]] || printf '%s\n' "${kms_line}"
    printf 'dtoverlay=disable-wifi\n'
    printf 'dtoverlay=disable-bt\n'
    printf '%s\n' "${PROFILE_END}"
  } >> "${destination}"
}

apply_boot_config() {
  local config rendered mode
  config="$(boot_config_path)" || die "could not find /boot/firmware/config.txt or /boot/config.txt"
  rendered="$(mktemp "${config}.profile.XXXXXX")"
  render_boot_config "${config}" "${rendered}"

  if cmp -s "${config}" "${rendered}"; then
    rm -f "${rendered}"
    printf 'unchanged %s\n' "${config}"
    return 0
  fi

  mode="$(stat -c '%a' "${config}" 2>/dev/null || stat -f '%Lp' "${config}")"
  chmod "${mode}" "${rendered}"
  mv -f "${rendered}" "${config}"
  printf 'updated %s\n' "${config}"
}

disable_headless_units() {
  local unit
  for unit in "${HEADLESS_UNITS[@]}"; do
    if unit_known "${unit}"; then
      run_systemctl disable --now "${unit}"
    else
      printf 'skipped absent unit %s\n' "${unit}"
    fi
  done
}

apply_profile() {
  [[ "${ROOT}" == /* ]] || die "PI4_PROFILE_ROOT must be an absolute path"
  [[ -d "${ROOT}" ]] || die "PI4_PROFILE_ROOT does not exist: ${ROOT}"
  if [[ "${ROOT}" == "/" && "${EUID}" -ne 0 ]]; then
    die "apply must run as root (for example: sudo ${PROGRAM} apply)"
  fi

  command -v install >/dev/null || die "install command is unavailable"
  command -v "${SYSTEMCTL_BIN}" >/dev/null || die "systemctl command is unavailable: ${SYSTEMCTL_BIN}"
  require_assets
  assert_pi4_target
  assert_ondemand_available
  adguard_querylog_config apply

  install_if_changed "${CPUFREQ_TEMPLATE}" "$(root_path "${CPUFREQ_CONFIG}")" 0644
  install_if_changed "${K3S_CONFIG_TEMPLATE}" "$(root_path "${K3S_CONFIG}")" 0600
  install_if_changed "${K3S_OVERRIDE_TEMPLATE}" "$(root_path "${K3S_OVERRIDE}")" 0644
  apply_boot_config

  run_systemctl set-default multi-user.target
  disable_headless_units
  run_systemctl daemon-reload

  if unit_known cpufrequtils.service; then
    run_systemctl try-restart cpufrequtils.service
  else
    printf 'NOTICE: cpufrequtils.service is absent; settings will require a compatible boot-time governor service.\n'
  fi

  if unit_known k3s.service; then
    run_systemctl restart k3s.service
  else
    printf 'NOTICE: k3s.service is absent; configuration was staged but not activated.\n'
  fi

  printf 'Profile applied. A deliberate reboot is still required for boot overlays; no reboot was requested.\n'
}

active_setting_count() {
  local config="$1"
  local expression="$2"
  grep -Ec "^[[:space:]]*${expression}[[:space:]]*$" "${config}" || true
}

read_value_or_unknown() {
  local file="$1"
  if [[ -r "${file}" ]]; then
    tr -d '\n' < "${file}"
  else
    printf 'unknown'
  fi
}

audit_profile() {
  local config target unit state active
  local -a policies=()

  printf 'Pi 4 performance profile audit (root=%s)\n' "${ROOT}"
  config="$(root_path "${CPUFREQ_CONFIG}")"
  if [[ -f "${config}" ]]; then
    printf '\nCPU frequency configuration (%s):\n' "${config}"
    grep -E '^(ENABLE|GOVERNOR|MIN_SPEED|MAX_SPEED)=' "${config}" || true
  else
    printf '\nCPU frequency configuration: absent\n'
  fi

  shopt -s nullglob
  policies=("$(root_path sys/devices/system/cpu/cpufreq)"/policy*)
  shopt -u nullglob
  for config in "${policies[@]}"; do
    printf 'Runtime %s: governor=%s min=%s max=%s\n' \
      "${config##*/}" \
      "$(read_value_or_unknown "${config}/scaling_governor")" \
      "$(read_value_or_unknown "${config}/scaling_min_freq")" \
      "$(read_value_or_unknown "${config}/scaling_max_freq")"
  done

  if target="$(boot_config_path)"; then
    printf '\nBoot settings (%s):\n' "${target}"
    grep -E '^[[:space:]]*(dtparam=audio|camera_auto_detect|display_auto_detect|arm_boost|dtoverlay=(vc4-kms-v3d|disable-wifi|disable-bt))' "${target}" || true
  else
    printf '\nBoot settings: config.txt absent\n'
  fi

  printf '\nk3s canonical configuration: '
  cmp -s "${K3S_CONFIG_TEMPLATE}" "$(root_path "${K3S_CONFIG}")" 2>/dev/null && printf 'current\n' || printf 'missing or drifted\n'
  printf 'k3s ExecStart override: '
  cmp -s "${K3S_OVERRIDE_TEMPLATE}" "$(root_path "${K3S_OVERRIDE}")" 2>/dev/null && printf 'current\n' || printf 'missing or drifted\n'
  if unit_known k3s.service; then
    printf 'k3s service: %s\n' "$(run_systemctl is-active k3s.service 2>/dev/null || true)"
  else
    printf 'k3s service: absent\n'
  fi

  target="$(run_systemctl get-default 2>/dev/null || true)"
  printf '\nDefault target: %s\n' "${target:-unknown}"
  for unit in "${HEADLESS_UNITS[@]}"; do
    if unit_known "${unit}"; then
      state="$(run_systemctl is-enabled "${unit}" 2>/dev/null || true)"
      active="$(run_systemctl is-active "${unit}" 2>/dev/null || true)"
      printf '%-34s enabled=%-10s active=%s\n' "${unit}" "${state:-unknown}" "${active:-unknown}"
    else
      printf '%-34s absent\n' "${unit}"
    fi
  done

  if [[ "${ROOT}" == "/" ]] && command -v vcgencmd >/dev/null 2>&1; then
    printf '\nFirmware telemetry:\n'
    vcgencmd measure_temp || true
    vcgencmd get_throttled || true
    vcgencmd measure_clock arm || true
  fi

  printf '\n'
  adguard_querylog_config audit || true
}

verify_profile() {
  local failures=0
  local config target unit state active policy value
  local -a policies=()

  verify_ok() { printf 'OK   %s\n' "$*"; }
  verify_fail() { printf 'FAIL %s\n' "$*" >&2; failures=$((failures + 1)); }
  verify_exact_setting() {
    local source="$1"
    local expression="$2"
    local success_message="$3"
    local failure_message="$4"
    if [[ "$(active_setting_count "${source}" "${expression}")" == "1" ]]; then
      verify_ok "${success_message}"
    else
      verify_fail "${failure_message}"
    fi
  }

  require_assets

  config="$(root_path "${CPUFREQ_CONFIG}")"
  if cmp -s "${CPUFREQ_TEMPLATE}" "${config}" 2>/dev/null; then
    verify_ok "cpufrequtils profile is current"
  else
    verify_fail "cpufrequtils profile is missing or drifted"
  fi

  if target="$(boot_config_path)"; then
    validate_managed_block "${target}"
    verify_exact_setting "${target}" 'dtparam=audio=off' "audio overlay is disabled" "expected exactly one dtparam=audio=off"
    verify_exact_setting "${target}" 'camera_auto_detect=0' "camera autodetection is disabled" "expected exactly one camera_auto_detect=0"
    verify_exact_setting "${target}" 'display_auto_detect=0' "display autodetection is disabled" "expected exactly one display_auto_detect=0"
    verify_exact_setting "${target}" 'arm_boost=1' "1.8 GHz arm_boost ceiling is retained" "expected exactly one arm_boost=1"
    verify_exact_setting "${target}" 'dtoverlay=disable-wifi' "Wi-Fi overlay is disabled" "expected exactly one dtoverlay=disable-wifi"
    verify_exact_setting "${target}" 'dtoverlay=disable-bt' "Bluetooth overlay is disabled" "expected exactly one dtoverlay=disable-bt"
    if grep -Eq '^[[:space:]]*dtoverlay=vc4-kms-v3d([,[:space:]]|$)' "${target}"; then
      verify_ok "KMS remains configured"
    else
      verify_fail "vc4-kms-v3d is not configured"
    fi
  else
    verify_fail "boot config is absent"
  fi

  if cmp -s "${K3S_CONFIG_TEMPLATE}" "$(root_path "${K3S_CONFIG}")" 2>/dev/null; then
    verify_ok "canonical k3s configuration is current"
  else
    verify_fail "canonical k3s configuration is missing or drifted"
  fi
  if cmp -s "${K3S_OVERRIDE_TEMPLATE}" "$(root_path "${K3S_OVERRIDE}")" 2>/dev/null; then
    verify_ok "k3s ExecStart override is current"
  else
    verify_fail "k3s ExecStart override is missing or drifted"
  fi
  if adguard_querylog_config verify; then
    verify_ok "AdGuard query-log retention is 7 days"
  else
    verify_fail "AdGuard query-log retention is not 7 days"
  fi
  if unit_known k3s.service; then
    active="$(run_systemctl is-active k3s.service 2>/dev/null || true)"
    if [[ "${active}" == "active" ]]; then
      verify_ok "k3s service is active"
    else
      verify_fail "k3s service active state is ${active:-unknown}"
    fi
  else
    verify_fail "k3s.service is absent"
  fi

  target="$(run_systemctl get-default 2>/dev/null || true)"
  if [[ "${target}" == "multi-user.target" ]]; then
    verify_ok "default target is multi-user.target"
  else
    verify_fail "default target is ${target:-unknown}"
  fi

  for unit in "${HEADLESS_UNITS[@]}"; do
    if ! unit_known "${unit}"; then
      verify_ok "${unit} is absent"
      continue
    fi
    state="$(run_systemctl is-enabled "${unit}" 2>/dev/null || true)"
    active="$(run_systemctl is-active "${unit}" 2>/dev/null || true)"
    case "${state}" in
      disabled|masked) verify_ok "${unit} is ${state}" ;;
      *) verify_fail "${unit} enable state is ${state:-unknown}" ;;
    esac
    case "${active}" in
      inactive|unknown) verify_ok "${unit} is not running" ;;
      *) verify_fail "${unit} active state is ${active:-unknown}" ;;
    esac
  done

  shopt -s nullglob
  policies=("$(root_path sys/devices/system/cpu/cpufreq)"/policy*)
  shopt -u nullglob
  if [[ "${#policies[@]}" -eq 0 ]]; then
    verify_fail "no cpufreq policies were found"
  fi
  for policy in "${policies[@]}"; do
    for value in scaling_governor scaling_min_freq scaling_max_freq; do
      [[ -r "${policy}/${value}" ]] || verify_fail "${policy}/${value} is unreadable"
    done
    if [[ "$(read_value_or_unknown "${policy}/scaling_governor")" == "ondemand" ]]; then
      verify_ok "${policy##*/} governor is ondemand"
    else
      verify_fail "${policy##*/} governor is not ondemand"
    fi
    if [[ "$(read_value_or_unknown "${policy}/scaling_min_freq")" == "600000" ]]; then
      verify_ok "${policy##*/} minimum is 600 MHz"
    else
      verify_fail "${policy##*/} minimum is not 600 MHz"
    fi
    if [[ "$(read_value_or_unknown "${policy}/scaling_max_freq")" == "1800000" ]]; then
      verify_ok "${policy##*/} maximum is 1.8 GHz"
    else
      verify_fail "${policy##*/} maximum is not 1.8 GHz"
    fi
  done

  if [[ "${failures}" -ne 0 ]]; then
    printf '%s verification check(s) failed.\n' "${failures}" >&2
    return 1
  fi
  printf 'Pi 4 performance profile verified.\n'
}

case "${1:-}" in
  audit) audit_profile ;;
  apply) apply_profile ;;
  verify) verify_profile ;;
  -h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac
