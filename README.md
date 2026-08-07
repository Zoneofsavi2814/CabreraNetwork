# Cabrera Network

Raspberry Pi 4 control-room dashboard for the home network. It combines a Vite/React UI with a Flask sidecar that reports host metrics, services, AdGuard, k3s, topology, logs, allowlisted actions, and web app links.

> **Current estate status (2026-07-31):** MacMiniOps on `macmini` (`192.168.0.6`) owns current application health for GRID, Work, Portfolio, and Uptime Kuma. This repository's Pi4 deployment and service-ownership notes below are retained as historical implementation records; Pi4 is now the Wedding-only host, and EagleEye, HomeTwin, Coinbot, and the former standalone Portfolio/Work/Programs rows are not current estate monitors.

## Local Development

```bash
npm ci
npm run build
python3 -m py_compile server/app.py server/k3s_client.py server/sudo_ops.py server/tplink_collector.py
```

## Pi4 Deployment (historical reference)

The former Pi4 copy lives at `/opt/pi4-noc` and ran as `pi4-noc.service` behind the configured HTTPS boundary; the sidecar itself bound to `127.0.0.1:8080` by default.

The former Pi4 service surface included AdGuard Home, k3s, Samba, SSH, GRID, Wedding, EagleEye, CabreraPortfolio, CabreraWorkWebsite, and CabreraPrograms. Those Pi4-local application rows are historical; current remote application health is owned by MacMiniOps.

The current health contract is MacMiniOps `/healthz`, GRID web `https://macmini.tail83be27.ts.net:8090/healthz`, GRID MCP `https://macmini.tail83be27.ts.net:7777/healthz` (HTTP `401` is the expected auth-boundary result), Work `https://macmini.tail83be27.ts.net:8081/healthz`, and Portfolio `https://macmini.tail83be27.ts.net:9443/api/health`. Wedding's public Funnel and TLS health remain separate at `https://ann-and-chris.tail83be27.ts.net/healthz`; override only that historical Pi4 probe with `PI4_NOC_WEDDING_HEALTH_URL` when its canonical route changes.

```bash
npm run build
scripts/install-pi.sh
```

The dashboard now binds to `127.0.0.1:8080` by default. PAM login is accepted only over HTTPS: use an existing HTTPS reverse proxy or Tailscale Serve in front of that loopback listener, configure `PI4_NOC_PUBLIC_URL`, and set `PI4_NOC_TRUSTED_PROXY_CIDRS` (plus the proxy hop count when needed) in `/etc/pi4-noc/notify.env`. The proxy must overwrite `X-Forwarded-Proto` and `X-Forwarded-For`; untrusted or malformed forwarding headers are ignored. Never publish the loopback port as plaintext LAN HTTP.

When no trusted proxy is available, configure direct TLS with `PI4_NOC_TLS_CERT_FILE` and `PI4_NOC_TLS_KEY_FILE`, set `PI4_NOC_HOST`/`PI4_NOC_PORT` to the TLS listener (normally `0.0.0.0`/`443`), and use the matching HTTPS URL. The sidecar refuses a non-loopback bind without both certificate files, and the browser login refuses to submit a PAM password from an HTTP page. A certificate/key pair must be provisioned and renewed outside this repository; the service remains loopback-only until that boundary is configured.

## Ops Notifications

The Ops Center can send the `Every morning` digest and any critical red alert (`fail` checks) through ntfy, email, or a webhook. Copy `/etc/pi4-noc/notify.env.example` to `/etc/pi4-noc/notify.env`, fill in the desired delivery settings, and restart `pi4-noc.service`.

Notification state is stored in `/var/lib/pi4-noc/notification-state.json` so morning digests send once per day and critical failures are deduped until recovery or the configured cooldown.

Failed morning deliveries are retried every 30 minutes until one succeeds that day. Override the bounded retry interval with `PI4_NOC_NOTIFY_MORNING_RETRY_SECONDS` (minimum five minutes). The state file records the latest delivery attempt, success, and failure without storing provider credentials.

Webhook delivery defaults to JSON. Set `PI4_NOC_NOTIFY_WEBHOOK_FORMAT=form` for form-encoded relay providers such as FormSubmit.
Webhook requests allow 30 seconds by default; override this with `PI4_NOC_NOTIFY_WEBHOOK_TIMEOUT_SECONDS` when a provider has a stricter latency requirement.

## Pi4 Backups And Restore Drills (historical reference)

The installer deploys `/usr/local/sbin/pi4-backup` and `/usr/local/sbin/pi4-restore-drill` with `pi4-backup.timer` and `pi4-restore-drill.timer`.

Backups remain local to Pi4 under `/mnt/ssd/backups/pi4`; there is no Pi5, Mac, or cloud copy. The archive includes GRID, consistent SQLite snapshots for k3s, EagleEye, the alert relay, and historical Uptime Kuma state, plus Portfolio data/config, CabreraPrograms config and restricted kubeconfig, retained application manifests/image digests, Tailscale state, AdGuard, Samba, registry data, and host inventory. Critical missing sources fail the job. Restore drills verify the newest archive checksum and retained image archive, extract only into `/mnt/ssd/restore-drills/pi4`, validate every SQLite/JSON state store, and never touch live services.

The Ops Center verifies local archive/checksum integrity, backup freshness, isolated restore-drill freshness, and the last systemd result for both jobs. This local-only design explicitly cannot recover from a simultaneous Pi4 and external-HDD failure. Failed morning and critical notification deliveries use bounded retry cooldowns so an unavailable provider cannot trigger a request every five-minute check cycle.

Off-host alerts publish to an account-free ntfy topic configured with `PI4_NOC_NOTIFY_NTFY_URL`. The public topic name is a secret and must be a cryptographically random 16-64 character value kept only in protected runtime configuration.

Critical notification cooldown state survives brief recoveries. A single healthy sample no longer makes the next transient failure look new inside the configured cooldown window.

Pi4 is the always-on delivery subscriber. `scripts/cabrera-alert-relay.py` polls the topic, commits new messages to the protected SQLite outbox at `/var/lib/cabrera-alert-relay/outbox.sqlite3`, and retries the existing email webhook without blocking the originating monitor. `cabrera-alert-relay.timer` runs the relay every five minutes as user `pi4`. A provider failure opens one durable circuit for the whole outbox, honors `Retry-After`, and otherwise backs off from 30 minutes to six hours; override those bounds with `CABRERA_ALERT_RELAY_PROVIDER_RETRY_BASE_SECONDS` and `CABRERA_ALERT_RELAY_PROVIDER_RETRY_MAX_SECONDS`. Repeated pending alerts with the same title and source tags are coalesced, and the relay's own outbox-health warning stays on ntfy instead of feeding back into the failed email path.

Relay status distinguishes confirmed webhook deliveries from retained rows that were administratively coalesced, suppressed, or pruned. Those retained rows stay in SQLite for auditability under the `discarded` count; they are never reported as email deliveries.

`scripts/install-macos-alert-subscriber.sh` is an optional convenience that shows native macOS notifications while a Mac is online. It is not part of the durable delivery path and may remain disabled.

Other host monitors may use the same protected topic; each publisher identifies its originating device in the alert title. The Pi4 relay now owns eventual FormSubmit delivery for the shared topic. Legacy `PI5_ALERTS_NOTIFY_*` names remain read-only compatibility fallbacks during the migration soak, while Pi4 uses `PI4_NOC_NOTIFY_*` or canonical `CABRERA_ALERT_RELAY_*` settings.

## Pi4 Power And Data HDD Health (historical reference)

`/mnt/ssd` is the compatibility mount path for the current rotational WDC USB data HDD; the dashboard labels it as `Data HDD` and reports the detected model, media type, transport, and rotational flag. Moving write-heavy data to a real SSD still requires a physical device and a controlled migration.

`pi4-boot-state.service` atomically records the running boot ID and marks it clean only from its shutdown `ExecStop`. The first installed boot is an unknown baseline; later unclean boots become critical Ops checks. State lives at `/var/lib/pi4-boot-state/state.json` and contains no secrets.

`pi4-log2ram-apply-fix` idempotently applies the log2ram 1.7.2 journal mirror correction and keeps a root-only rollback copy. `pi4-log2ram-guard.service` then fails visibly if a future log2ram update removes the correction or restores stale active journal filenames.

SMART reads use the root-owned fixed `smart_health` helper for the HDD's persistent WWN path with `-d sat`; the web service cannot supply a device or arbitrary smartctl arguments. Mount integrity is verified by filesystem UUID, so USB enumeration changes cannot silently point the checks at a future SSD. A standby-safe health poll runs from the Ops Center. Weekly short and monthly long self-tests are scheduled by `pi4-smart-short.timer` and `pi4-smart-long.timer`, outside the nightly backup window.

The unused Uptime Kuma, synthetic smoke, and local registry workloads were retired from k3s. Their existing host data directories remain untouched so retirement does not destructively erase historical state.

## Pi4 Balanced Performance Profile (historical reference)

`scripts/pi4-performance-profile.sh` provides `audit`, `apply`, and `verify` commands. The profile keeps the 600 MHz–1.8 GHz range and `arm_boost=1`, switches the governor to `ondemand`, installs the canonical k3s network/storage settings while disabling packaged `metrics-server` and `local-storage`, changes AdGuard query-log retention to seven days through its current API, and disables the unused desktop, display helpers, Wi-Fi, Bluetooth, and Samba AD DC services. Removing metrics-server intentionally makes `kubectl top` unavailable on this node.

`apply` restarts cpufrequtils and k3s when present but never reboots. Run it only during a maintenance window, then reboot deliberately once so the boot overlays take effect:

```bash
sudo bash scripts/pi4-performance-profile.sh audit
sudo bash scripts/pi4-performance-profile.sh apply
sudo bash scripts/pi4-performance-profile.sh verify
```

Dashboard collectors default to 2 seconds for host counters, 15 seconds for services/operations/local topology, 60 seconds for AdGuard and k3s, 120 seconds for router/AP polling, and 1800 seconds for NAS sizing. Bounded overrides are available through `PI4_NOC_HOT_REFRESH_SECONDS`, `SERVICE_REFRESH_SECONDS`, `HEAVY_REFRESH_SECONDS`, `ROUTER_REFRESH_SECONDS`, and `STORAGE_REFRESH_SECONDS`.

The k3s inventory collector reuses a verified local API client between refreshes and retains the single batched `kubectl` read only as a fallback. Snapshot serialization is shared by revision across SSE clients, operations checks share one certificate-verifying TLS opener, and unchanged backup archives/checksums reuse integrity results keyed by path, size, and modification time.

## Pi4 Update Policy (historical reference)

APT refresh and unattended security updates are enabled. `config/apt/52pi4-maintenance.conf` explicitly disables unattended reboots, so kernel, EEPROM, and k3s restarts stay inside a maintenance window with backup, boot-state, storage, pod, and endpoint validation. Retain the previous k3s binary and kernel packages until the updated node has passed those checks.

## Remote Access

Cabrera Network is intended to stay private. The former Pi4 dashboard used Tailscale subnet routing for `192.168.0.0/24`; current estate access to MacMiniOps and its application endpoints uses the configured Mac mini Tailnet routes. Do not expose either surface with router port forwarding.

Once Tailscale is connected on an approved device, use the configured HTTPS dashboard URL (for example, the HTTPS hostname published by Tailscale Serve or the direct TLS listener):

```text
https://dashboard.example.tailnet/
```

The Mac mini Tailnet routes keep the current Web Apps links usable remotely. The links are navigation only; authoritative health comes from the MacMiniOps monitor contract above.

## Validation Notes

- Backend syntax smoke: `python3 -m py_compile server/app.py server/k3s_client.py server/sudo_ops.py server/tplink_collector.py`.
- Frontend build smoke: `npm run build`.
- Historical live validation: former Pi4 unit/deployment checks and Pi4-local Web Apps status are retained in the dated progress record above, not as current estate gates.
- Current estate validation: MacMiniOps/Kuma owns the five current application checks listed above; no live-green claim is made by this repository for remote links without a current check result.

See `MEMORY.md` for project memory, live service notes, and gotchas.
