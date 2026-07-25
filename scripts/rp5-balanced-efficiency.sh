#!/usr/bin/env bash
set -euo pipefail

PROGRAM="${0##*/}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ -n "${RP5_PROFILE_ASSET_DIR:-}" ]]; then
  ASSET_DIR="${RP5_PROFILE_ASSET_DIR}"
elif [[ -d "${REPO_DIR}/deploy/pi5-balanced-efficiency" ]]; then
  ASSET_DIR="${REPO_DIR}/deploy/pi5-balanced-efficiency"
else
  ASSET_DIR="${SCRIPT_DIR}/assets"
fi
ROOT="${RP5_PROFILE_ROOT:-/}"
SYSTEMCTL_BIN="${RP5_PROFILE_SYSTEMCTL:-systemctl}"

readonly PROFILE_BEGIN="# BEGIN rp5-balanced-efficiency"
readonly PROFILE_END="# END rp5-balanced-efficiency"
readonly ALERT_SKIP_KEY="PI5_ALERTS_SKIP_CHECKS"
readonly ALERT_SKIP_TOKENS="service-wayvnc,k3s-storage-growth"
K3S_CONFIG_CHANGED=0

readonly -a DISABLED_UNITS=(
  lightdm.service
  wayvnc-control.service
  wayvnc.service
  cups.service
  cups.socket
  cups.path
  cups-browsed.service
  ModemManager.service
  bluetooth.service
  hciuart.service
  avahi-daemon.service
  avahi-daemon.socket
  colord.service
  rp5-runner-monitor.service
  netavark-dhcp-proxy.service
  netavark-dhcp-proxy.socket
  cloud-init.service
  cloud-init-main.service
  cloud-init-local.service
  cloud-init-network.service
  cloud-config.service
  cloud-final.service
)

readonly -a USER_DISABLED_UNITS=(
  filter-chain.service
  mpris-proxy.service
  pipewire.service
  pipewire.socket
  pipewire-pulse.service
  pipewire-pulse.socket
  wireplumber.service
  xdg-desktop-portal-rewrite-launchers.service
)

readonly -a USER_STOP_UNITS=(
  gvfs-afc-volume-monitor.service
  gvfs-daemon.service
  gvfs-goa-volume-monitor.service
  gvfs-gphoto2-volume-monitor.service
  gvfs-mtp-volume-monitor.service
  gvfs-udisks2-volume-monitor.service
  xdg-desktop-portal.service
  xdg-document-portal.service
  xdg-permission-store.service
)

usage() {
  cat <<EOF
Usage: ${PROGRAM} audit|apply|verify|finalize

  audit     Print the current profile state without changing it.
  apply     Install and activate the balanced-efficiency profile. Never reboots.
  verify    Verify installed files and runtime state without changing anything.
  finalize  After a successful 72-hour window, reduce trend sampling to 15 minutes.

Offline test overrides:
  RP5_PROFILE_ROOT=/tmp/root
  RP5_PROFILE_SYSTEMCTL=/path/to/systemctl-stub
  RP5_PROFILE_ASSET_DIR=/path/to/assets
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
  local base
  for base in etc/systemd/system run/systemd/system usr/local/lib/systemd/system usr/lib/systemd/system lib/systemd/system; do
    if [[ -e "$(root_path "${base}/${unit}")" || -L "$(root_path "${base}/${unit}")" ]]; then
      return 0
    fi
  done
  run_systemctl list-unit-files "${unit}" --no-legend --no-pager 2>/dev/null |
    awk -v expected="${unit}" '$1 == expected { found = 1 } END { exit !found }'
}

install_if_changed() {
  local source="$1" destination="$2" mode="$3"
  [[ -f "${source}" ]] || die "missing profile asset: ${source}"
  if [[ -f "${destination}" ]] && cmp -s "${source}" "${destination}"; then
    printf 'unchanged %s\n' "${destination}"
    return 0
  fi
  mkdir -p "$(dirname "${destination}")"
  install -m "${mode}" "${source}" "${destination}"
  printf 'installed %s\n' "${destination}"
}

assert_pi5_target() {
  local model_file model
  [[ "${RP5_PROFILE_ALLOW_NON_PI:-0}" == "1" ]] && return 0
  model_file="$(root_path proc/device-tree/model)"
  if [[ -f "${model_file}" ]]; then
    model="$(tr -d '\000' < "${model_file}")"
    [[ "${model}" == *"Raspberry Pi 5"* ]] || die "refusing to apply Pi 5 profile to: ${model}"
  elif [[ "${ROOT}" == "/" ]]; then
    die "cannot confirm Raspberry Pi 5 hardware"
  fi
}

render_managed_file() {
  local source="$1" destination="$2"
  awk -v begin="${PROFILE_BEGIN}" -v end="${PROFILE_END}" '
    $0 == begin { managed = 1; next }
    $0 == end { managed = 0; next }
    managed { next }
    { print }
  ' "${source}" > "${destination}"
}

apply_boot_config() {
  local config rendered stripped mode
  config="$(root_path boot/firmware/config.txt)"
  [[ -f "${config}" ]] || die "missing ${config}"
  rendered="$(mktemp "${config}.profile.XXXXXX")"
  stripped="$(mktemp "${config}.stripped.XXXXXX")"
  render_managed_file "${config}" "${stripped}"
  awk '
    /^[[:space:]]*dtparam=audio=/ { next }
    /^[[:space:]]*camera_auto_detect=/ { next }
    /^[[:space:]]*display_auto_detect=/ { next }
    /^[[:space:]]*arm_boost=/ { next }
    /^[[:space:]]*dtoverlay=disable-bt([,[:space:]]|$)/ { next }
    { print }
  ' "${stripped}" | awk 'NF { last = NR } { line[NR] = $0 } END { for (i = 1; i <= last; i++) print line[i] }' > "${rendered}"
  rm -f "${stripped}"
  {
    printf '\n%s\n' "${PROFILE_BEGIN}"
    printf 'dtparam=audio=off\n'
    printf 'camera_auto_detect=0\n'
    printf 'display_auto_detect=1\n'
    printf 'arm_boost=1\n'
    printf 'dtoverlay=disable-bt\n'
    printf '%s\n' "${PROFILE_END}"
  } >> "${rendered}"
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

apply_cmdline() {
  local cmdline rendered token mode
  local -a kept=()
  cmdline="$(root_path boot/firmware/cmdline.txt)"
  [[ -f "${cmdline}" ]] || die "missing ${cmdline}"
  read -r -a tokens < "${cmdline}"
  for token in "${tokens[@]}"; do
    case "${token}" in
      quiet|splash|plymouth.ignore-serial-consoles|plymouth.enable=*|ds=nocloud*) continue ;;
      *) kept+=("${token}") ;;
    esac
  done
  kept+=("plymouth.enable=0")
  rendered="$(mktemp "${cmdline}.profile.XXXXXX")"
  (IFS=' '; printf '%s\n' "${kept[*]}") > "${rendered}"
  if cmp -s "${cmdline}" "${rendered}"; then
    rm -f "${rendered}"
    printf 'unchanged %s\n' "${cmdline}"
    return 0
  fi
  mode="$(stat -c '%a' "${cmdline}" 2>/dev/null || stat -f '%Lp' "${cmdline}")"
  chmod "${mode}" "${rendered}"
  mv -f "${rendered}" "${cmdline}"
  printf 'updated %s\n' "${cmdline}"
}

ensure_env_value() {
  local file="$1" key="$2" value="$3" temp
  mkdir -p "$(dirname "${file}")"
  [[ -f "${file}" ]] || : > "${file}"
  temp="$(mktemp "${file}.profile.XXXXXX")"
  awk -v key="${key}" -v value="${value}" '
    index($0, key "=") == 1 {
      if (!written) print key "=" value
      written = 1
      next
    }
    { print }
    END { if (!written) print key "=" value }
  ' "${file}" > "${temp}"
  if cmp -s "${file}" "${temp}"; then
    rm -f "${temp}"
    return 0
  fi
  chmod --reference="${file}" "${temp}" 2>/dev/null || chmod 0640 "${temp}"
  chown --reference="${file}" "${temp}" 2>/dev/null || true
  mv -f "${temp}" "${file}"
  printf 'updated %s (%s)\n' "${file}" "${key}"
}

remove_env_key() {
  local file="$1" key="$2" temp
  [[ -f "${file}" ]] || return 0
  temp="$(mktemp "${file}.profile.XXXXXX")"
  awk -v key="${key}" 'index($0, key "=") != 1 { print }' "${file}" > "${temp}"
  if cmp -s "${file}" "${temp}"; then
    rm -f "${temp}"
    return 0
  fi
  chmod --reference="${file}" "${temp}" 2>/dev/null || chmod 0644 "${temp}"
  chown --reference="${file}" "${temp}" 2>/dev/null || true
  mv -f "${temp}" "${file}"
  printf 'removed obsolete %s from %s\n' "${key}" "${file}"
}

append_env_tokens() {
  local file="$1" key="$2" additions="$3" current token merged
  current="$(sed -n "s/^${key}=//p" "${file}" 2>/dev/null | tail -1)"
  merged="${current}"
  IFS=',' read -r -a tokens <<< "${additions}"
  for token in "${tokens[@]}"; do
    if [[ ",${merged}," != *",${token},"* ]]; then
      merged="${merged:+${merged},}${token}"
    fi
  done
  ensure_env_value "${file}" "${key}" "${merged}"
}

apply_alert_patch() {
  local app_dir patch_file
  app_dir="$(root_path opt/pi5-critical-alerts)"
  patch_file="${ASSET_DIR}/pi5-alerts-remove-traefik.patch"
  if [[ ! -f "${app_dir}/pi5_alerts.py" ]]; then
    printf 'skipped absent %s\n' "${app_dir}/pi5_alerts.py"
    return 0
  fi
  if grep -Fq '("Deployment", "kube-system", "traefik"),' "${app_dir}/pi5_alerts.py"; then
    patch --directory="${app_dir}" --strip=0 --forward < "${patch_file}"
    printf 'removed retired Traefik workload from Pi5 alert inventory\n'
  else
    printf 'Traefik already absent from Pi5 alert inventory\n'
  fi
}

install_assets() {
  install_if_changed "${ASSET_DIR}/rp5-desktop-maintenance.target" "$(root_path etc/systemd/system/rp5-desktop-maintenance.target)" 0644
  install_if_changed "${ASSET_DIR}/cabrera-portfolio-manual-sync.conf" "$(root_path etc/systemd/system/cabrera-portfolio-manual-sync.service.d/20-balanced-efficiency.conf)" 0644
  install_if_changed "${ASSET_DIR}/pi5-storage-growth.service" "$(root_path etc/systemd/system/pi5-storage-growth.service)" 0644
  install_if_changed "${ASSET_DIR}/pi5-storage-growth.timer" "$(root_path etc/systemd/system/pi5-storage-growth.timer)" 0644
  install_if_changed "${ASSET_DIR}/pi5_storage_growth.py" "$(root_path opt/pi5-critical-alerts/pi5_storage_growth.py)" 0755
  install_if_changed "${ASSET_DIR}/cloud-init.disabled" "$(root_path etc/cloud/cloud-init.disabled)" 0644
}

install_k3s_config() {
  local source target legacy legacy_value env_file
  source="${ASSET_DIR}/k3s-config.yaml"
  target="$(root_path etc/rancher/k3s/config.yaml.d/90-rp5-balanced-efficiency.yaml)"
  legacy="$(root_path etc/rancher/k3s/config.yaml)"
  env_file="$(root_path etc/systemd/system/k3s.service.env)"

  if [[ ! -f "${target}" ]] || ! cmp -s "${source}" "${target}"; then
    install_if_changed "${source}" "${target}" 0644
    K3S_CONFIG_CHANGED=1
  else
    printf 'unchanged %s\n' "${target}"
  fi

  if [[ -f "${legacy}" ]]; then
    legacy_value="$(sed '/^[[:space:]]*$/d' "${legacy}")"
    if [[ "${legacy_value}" == $'disable:\n  - traefik' ]]; then
      rm -f "${legacy}"
      K3S_CONFIG_CHANGED=1
      printf 'removed superseded profile-owned %s\n' "${legacy}"
    fi
  fi

  if grep -q '^K3S_DISABLE=' "${env_file}" 2>/dev/null; then
    remove_env_key "${env_file}" K3S_DISABLE
    K3S_CONFIG_CHANGED=1
  fi
}

disable_units() {
  local unit
  for unit in "${DISABLED_UNITS[@]}"; do
    if unit_known "${unit}"; then
      run_systemctl disable --now "${unit}" || printf 'warning: could not disable %s\n' "${unit}" >&2
    else
      printf 'skipped absent unit %s\n' "${unit}"
    fi
  done
}

disable_user_units() {
  local unit
  [[ "${ROOT}" == "/" ]] || return 0
  id pi5 >/dev/null 2>&1 || return 0
  "${SYSTEMCTL_BIN}" --global disable "${USER_DISABLED_UNITS[@]}" ||
    printf 'warning: could not disable globally enabled user units\n' >&2
  [[ -d /run/user/1000 ]] || return 0
  for unit in "${USER_DISABLED_UNITS[@]}"; do
    runuser -u pi5 -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user disable --now "${unit}" ||
      printf 'warning: could not disable user unit %s\n' "${unit}" >&2
  done
  for unit in "${USER_STOP_UNITS[@]}"; do
    runuser -u pi5 -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop "${unit}" ||
      printf 'warning: could not stop user unit %s\n' "${unit}" >&2
  done
}

apply_profile() {
  assert_pi5_target
  apply_boot_config
  apply_cmdline
  install_assets
  install_k3s_config
  append_env_tokens "$(root_path etc/pi5-critical-alerts/monitor.env)" "${ALERT_SKIP_KEY}" "${ALERT_SKIP_TOKENS}"
  apply_alert_patch
  run_systemctl daemon-reload
  run_systemctl set-default multi-user.target
  disable_units
  disable_user_units
  if [[ "${K3S_CONFIG_CHANGED}" == "1" ]]; then
    run_systemctl stop pi5-storage-growth.timer pi5-storage-growth.service || true
    if run_systemctl is-active --quiet k3s.service; then
      run_systemctl restart k3s.service
      run_systemctl reset-failed pi5-storage-growth.service || true
      run_systemctl enable --now pi5-storage-growth.timer
    else
      printf 'warning: k3s config changed while k3s is inactive; storage timer was not started\n' >&2
    fi
  elif ! run_systemctl is-active --quiet pi5-storage-growth.timer ||
    [[ "$(run_systemctl is-enabled pi5-storage-growth.timer 2>/dev/null || true)" != "enabled" ]]; then
    run_systemctl enable --now pi5-storage-growth.timer
  fi
  printf 'balanced-efficiency profile applied; reboot was not requested\n'
}

audit_profile() {
  printf 'Raspberry Pi 5 balanced-efficiency audit\n'
  printf 'default target: '
  run_systemctl get-default || true
  printf 'temperature: '
  [[ "${ROOT}" != "/" ]] || vcgencmd measure_temp 2>/dev/null || true
  printf 'throttling: '
  [[ "${ROOT}" != "/" ]] || vcgencmd get_throttled 2>/dev/null || true
  run_systemctl is-active k3s.service rp5-runner-monitor.service lightdm.service wayvnc.service pi5-storage-growth.timer 2>/dev/null || true
}

verify_profile() {
  local unit enabled governor min_freq max_freq
  [[ "$(run_systemctl get-default)" == "multi-user.target" ]] || die "default target is not multi-user.target"
  for unit in "${DISABLED_UNITS[@]}"; do
    if unit_known "${unit}" && run_systemctl is-active --quiet "${unit}"; then
      die "${unit} is active"
    fi
    if unit_known "${unit}"; then
      enabled="$(run_systemctl is-enabled "${unit}" 2>/dev/null || true)"
      case "${enabled}" in
        disabled|masked|static|not-found|"") ;;
        *) die "${unit} remains enabled (${enabled})" ;;
      esac
    fi
  done
  grep -Fqx 'disable+:' "$(root_path etc/rancher/k3s/config.yaml.d/90-rp5-balanced-efficiency.yaml)" || die "Traefik drop-in is missing"
  grep -Fqx '  - traefik' "$(root_path etc/rancher/k3s/config.yaml.d/90-rp5-balanced-efficiency.yaml)" || die "Traefik disable setting is missing"
  [[ -f "$(root_path etc/cloud/cloud-init.disabled)" ]] || die "cloud-init disable marker is missing"
  grep -Fq 'k3s-storage-growth' "$(root_path etc/pi5-critical-alerts/monitor.env)" || die "fast alert timer still includes storage growth scans"
  grep -Fqx 'dtparam=audio=off' "$(root_path boot/firmware/config.txt)" || die "audio is not disabled"
  grep -Fqx 'camera_auto_detect=0' "$(root_path boot/firmware/config.txt)" || die "camera auto-detect is not disabled"
  grep -Fqx 'display_auto_detect=1' "$(root_path boot/firmware/config.txt)" || die "maintenance display support is not retained"
  grep -Fqx 'arm_boost=1' "$(root_path boot/firmware/config.txt)" || die "stock arm boost is not retained"
  grep -Fqx 'dtparam=cooling_fan=on' "$(root_path boot/firmware/config.txt)" || die "stock Pi 5 cooling fan policy is not retained"
  grep -Fqx 'dtoverlay=disable-bt' "$(root_path boot/firmware/config.txt)" || die "Bluetooth overlay is not disabled"
  if grep -Eq '^[[:space:]]*(force_turbo|arm_freq(_min)?|core_freq(_min)?|gpu_freq(_min)?|over_voltage(_delta)?(_min)?)[[:space:]]*=' "$(root_path boot/firmware/config.txt)"; then
    die "custom clock or voltage tuning is active"
  fi
  if grep -Eq '^[[:space:]]*dtparam=fan_temp' "$(root_path boot/firmware/config.txt)"; then
    die "custom fan thresholds are active"
  fi
  if grep -Eq '(^| )(quiet|splash|plymouth\.ignore-serial-consoles|ds=nocloud[^ ]*)( |$)' "$(root_path boot/firmware/cmdline.txt)"; then
    die "headless cmdline still includes splash/cloud-init arguments"
  fi
  grep -Eq '(^| )plymouth\.enable=0( |$)' "$(root_path boot/firmware/cmdline.txt)" || die "Plymouth is not explicitly disabled"
  run_systemctl is-active --quiet k3s.service || die "k3s is not active"
  run_systemctl is-active --quiet pi5-storage-growth.timer || die "hourly storage timer is not active"
  [[ "$(run_systemctl is-enabled pi5-storage-growth.timer 2>/dev/null || true)" == "enabled" ]] || die "hourly storage timer is not enabled"
  if [[ "${ROOT}" == "/" ]]; then
    for unit in "${USER_DISABLED_UNITS[@]}"; do
      enabled="$("${SYSTEMCTL_BIN}" --global is-enabled "${unit}" 2>/dev/null || true)"
      case "${enabled}" in
        disabled|masked|static|not-found|"") ;;
        *) die "global user unit ${unit} remains enabled (${enabled})" ;;
      esac
    done
    if [[ -d /run/user/1000 ]]; then
      for unit in "${USER_DISABLED_UNITS[@]}" "${USER_STOP_UNITS[@]}"; do
        if runuser -u pi5 -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active --quiet "${unit}"; then
          die "user unit ${unit} is active"
        fi
      done
    fi
    governor="$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor)"
    min_freq="$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_min_freq)"
    max_freq="$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq)"
    [[ "${governor}" == "ondemand" ]] || die "CPU governor is ${governor}, expected ondemand"
    [[ "${min_freq}" == "1500000" ]] || die "CPU min frequency is ${min_freq}, expected 1500000"
    [[ "${max_freq}" == "2400000" ]] || die "CPU max frequency is ${max_freq}, expected 2400000"
    if ss -H -ltn 'sport = :8097' | grep -q .; then
      die "retired runner monitor port 8097 is still listening"
    fi
  fi
  printf 'balanced-efficiency profile verified\n'
}

finalize_profile() {
  install_if_changed "${ASSET_DIR}/rp5-trend-sample.conf" "$(root_path etc/systemd/system/rp5-trend-sample.timer.d/20-balanced-efficiency.conf)" 0644
  run_systemctl daemon-reload
  run_systemctl restart rp5-trend-sample.timer
  printf 'trend sampling finalized at 15 minutes\n'
}

case "${1:-}" in
  audit) audit_profile ;;
  apply) apply_profile ;;
  verify) verify_profile ;;
  finalize) finalize_profile ;;
  *) usage; exit 2 ;;
esac
