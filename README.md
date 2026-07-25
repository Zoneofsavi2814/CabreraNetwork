# Cabrera Network

Raspberry Pi 4 control-room dashboard for the home network. It combines a Vite/React UI with a Flask sidecar that reports host metrics, services, AdGuard, k3s, topology, logs, allowlisted actions, and web app links.

## Local Development

```bash
npm ci
npm run build
python3 -m py_compile server/app.py server/k3s_client.py server/sudo_ops.py server/tplink_collector.py
```

## Pi4 Deployment

The deployed Pi4 copy lives at `/opt/pi4-noc` and runs as `pi4-noc.service` on `http://192.168.0.101/`.

Observed/controlled services include AdGuard Home, k3s, Samba, SSH, GRID, Wedding, EagleEye, CabreraPortfolio, CabreraWorkWebsite, and CabreraPrograms.

Portfolio runs on Pi4 loopback at `127.0.0.1:8099`; Tailscale Serve publishes it privately at `https://ann-and-chris.tail83be27.ts.net:9443/`. Wedding's public Funnel and TLS health are checked independently at `https://ann-and-chris.tail83be27.ts.net/healthz`. Override these probes with `PI4_NOC_PORTFOLIO_HEALTH_URL`, `PI4_NOC_PORTFOLIO_LOOPBACK_HEALTH_URL`, or `PI4_NOC_WEDDING_HEALTH_URL` only when the canonical routes change.

```bash
npm run build
scripts/install-pi.sh
```

## Ops Notifications

The Ops Center can send the `Every morning` digest and any critical red alert (`fail` checks) through ntfy, email, or a webhook. Copy `/etc/pi4-noc/notify.env.example` to `/etc/pi4-noc/notify.env`, fill in the desired delivery settings, and restart `pi4-noc.service`.

Notification state is stored in `/var/lib/pi4-noc/notification-state.json` so morning digests send once per day and critical failures are deduped until recovery or the configured cooldown.

Failed morning deliveries are retried every 30 minutes until one succeeds that day. Override the bounded retry interval with `PI4_NOC_NOTIFY_MORNING_RETRY_SECONDS` (minimum five minutes). The state file records the latest delivery attempt, success, and failure without storing provider credentials.

Webhook delivery defaults to JSON. Set `PI4_NOC_NOTIFY_WEBHOOK_FORMAT=form` for form-encoded relay providers such as FormSubmit.
Webhook requests allow 30 seconds by default; override this with `PI4_NOC_NOTIFY_WEBHOOK_TIMEOUT_SECONDS` when a provider has a stricter latency requirement.

## Pi4 Backups And Restore Drills

The installer deploys `/usr/local/sbin/pi4-backup` and `/usr/local/sbin/pi4-restore-drill` with `pi4-backup.timer` and `pi4-restore-drill.timer`.

Backups remain local to Pi4 under `/mnt/ssd/backups/pi4`; there is no Pi5, Mac, or cloud copy. The archive includes GRID, consistent SQLite snapshots for k3s, EagleEye, the alert relay, and historical Uptime Kuma state, plus Portfolio data/config, CabreraPrograms config and restricted kubeconfig, retained application manifests/image digests, Tailscale state, AdGuard, Samba, registry data, and host inventory. Critical missing sources fail the job. Restore drills verify the newest archive checksum and retained image archive, extract only into `/mnt/ssd/restore-drills/pi4`, validate every SQLite/JSON state store, and never touch live services.

The Ops Center verifies local archive/checksum integrity, backup freshness, isolated restore-drill freshness, and the last systemd result for both jobs. This local-only design explicitly cannot recover from a simultaneous Pi4 and external-HDD failure. Failed morning and critical notification deliveries use bounded retry cooldowns so an unavailable provider cannot trigger a request every five-minute check cycle.

Off-host alerts publish to an account-free ntfy topic configured with `PI4_NOC_NOTIFY_NTFY_URL`. The public topic name is a secret and must be a cryptographically random 16-64 character value kept only in protected runtime configuration.

Critical notification cooldown state survives brief recoveries. A single healthy sample no longer makes the next transient failure look new inside the configured cooldown window.

Pi4 is the always-on delivery subscriber. `scripts/cabrera-alert-relay.py` polls the topic, commits new messages to the protected SQLite outbox at `/var/lib/cabrera-alert-relay/outbox.sqlite3`, and retries the existing email webhook without blocking the originating monitor. `cabrera-alert-relay.timer` runs the relay every five minutes as user `pi4`. A provider failure opens one durable circuit for the whole outbox, honors `Retry-After`, and otherwise backs off from 30 minutes to six hours; override those bounds with `CABRERA_ALERT_RELAY_PROVIDER_RETRY_BASE_SECONDS` and `CABRERA_ALERT_RELAY_PROVIDER_RETRY_MAX_SECONDS`. Repeated pending alerts with the same title and source tags are coalesced, and the relay's own outbox-health warning stays on ntfy instead of feeding back into the failed email path.

Relay status distinguishes confirmed webhook deliveries from retained rows that were administratively coalesced, suppressed, or pruned. Those retained rows stay in SQLite for auditability under the `discarded` count; they are never reported as email deliveries.

`scripts/install-macos-alert-subscriber.sh` is an optional convenience that shows native macOS notifications while a Mac is online. It is not part of the durable delivery path and may remain disabled.

Other host monitors may use the same protected topic; each publisher identifies its originating device in the alert title. The Pi4 relay now owns eventual FormSubmit delivery for the shared topic. Legacy `PI5_ALERTS_NOTIFY_*` names remain read-only compatibility fallbacks during the migration soak, while Pi4 uses `PI4_NOC_NOTIFY_*` or canonical `CABRERA_ALERT_RELAY_*` settings.

## Pi4 Power And Data HDD Health

`/mnt/ssd` is the compatibility mount path for the current rotational WDC USB data HDD; the dashboard labels it as `Data HDD` and reports the detected model, media type, transport, and rotational flag. Moving write-heavy data to a real SSD still requires a physical device and a controlled migration.

`pi4-boot-state.service` atomically records the running boot ID and marks it clean only from its shutdown `ExecStop`. The first installed boot is an unknown baseline; later unclean boots become critical Ops checks. State lives at `/var/lib/pi4-boot-state/state.json` and contains no secrets.

`pi4-log2ram-apply-fix` idempotently applies the log2ram 1.7.2 journal mirror correction and keeps a root-only rollback copy. `pi4-log2ram-guard.service` then fails visibly if a future log2ram update removes the correction or restores stale active journal filenames.

SMART reads use the root-owned fixed `smart_health` helper for the HDD's persistent WWN path with `-d sat`; the web service cannot supply a device or arbitrary smartctl arguments. Mount integrity is verified by filesystem UUID, so USB enumeration changes cannot silently point the checks at a future SSD. A standby-safe health poll runs from the Ops Center. Weekly short and monthly long self-tests are scheduled by `pi4-smart-short.timer` and `pi4-smart-long.timer`, outside the nightly backup window.

The unused Uptime Kuma, synthetic smoke, and local registry workloads were retired from k3s. Their existing host data directories remain untouched so retirement does not destructively erase historical state.

## Pi4 Balanced Performance Profile

`scripts/pi4-performance-profile.sh` provides `audit`, `apply`, and `verify` commands. The profile keeps the 600 MHz–1.8 GHz range and `arm_boost=1`, switches the governor to `ondemand`, installs the canonical k3s network/storage settings while disabling packaged `metrics-server` and `local-storage`, changes AdGuard query-log retention to seven days through its current API, and disables the unused desktop, display helpers, Wi-Fi, Bluetooth, and Samba AD DC services. Removing metrics-server intentionally makes `kubectl top` unavailable on this node.

`apply` restarts cpufrequtils and k3s when present but never reboots. Run it only during a maintenance window, then reboot deliberately once so the boot overlays take effect:

```bash
sudo bash scripts/pi4-performance-profile.sh audit
sudo bash scripts/pi4-performance-profile.sh apply
sudo bash scripts/pi4-performance-profile.sh verify
```

Dashboard collectors default to 2 seconds for host counters, 15 seconds for services/operations/local topology, 60 seconds for AdGuard and k3s, 120 seconds for router/AP polling, and 1800 seconds for NAS sizing. Bounded overrides are available through `PI4_NOC_HOT_REFRESH_SECONDS`, `SERVICE_REFRESH_SECONDS`, `HEAVY_REFRESH_SECONDS`, `ROUTER_REFRESH_SECONDS`, and `STORAGE_REFRESH_SECONDS`.

The k3s inventory collector reuses a verified local API client between refreshes and retains the single batched `kubectl` read only as a fallback. Snapshot serialization is shared by revision across SSE clients, operations checks share one certificate-verifying TLS opener, and unchanged backup archives/checksums reuse integrity results keyed by path, size, and modification time.

## Pi4 Update Policy

APT refresh and unattended security updates are enabled. `config/apt/52pi4-maintenance.conf` explicitly disables unattended reboots, so kernel, EEPROM, and k3s restarts stay inside a maintenance window with backup, boot-state, storage, pod, and endpoint validation. Retain the previous k3s binary and kernel packages until the updated node has passed those checks.

## Remote Access

Cabrera Network is intended to stay private. Remote access should use Tailscale with the Pi4 as a subnet router for `192.168.0.0/24`; do not expose the dashboard with router port forwarding.

Once Tailscale is connected on an approved device, use the normal LAN URL:

```text
http://192.168.0.101/
```

The same subnet route keeps the dashboard's Web Apps links usable remotely, including AdGuard `:8080` and GRID `:8090` / `:7777`.

## Validation Notes

- Backend syntax smoke: `python3 -m py_compile server/app.py server/k3s_client.py server/sudo_ops.py server/tplink_collector.py`.
- Frontend build smoke: `npm run build`.
- Live validation: `systemctl is-active pi4-noc.service cabrera-portfolio.service cabrera-programs.service`, the Wedding/EagleEye/Work deployments are Ready, the Web Apps panel shows every retained link `online`, and GRID reports `1/1 ready`.

See `MEMORY.md` for project memory, live service notes, and gotchas.
