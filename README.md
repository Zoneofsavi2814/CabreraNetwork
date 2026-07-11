# Cabrera Network

Raspberry Pi 4 control-room dashboard for the home network. It combines a Vite/React UI with a Flask sidecar that reports host metrics, services, AdGuard, k3s, topology, logs, allowlisted actions, and web app links.

## Local Development

```bash
npm ci
npm run build
python3 -m py_compile server/app.py server/sudo_ops.py server/tplink_collector.py
```

## Pi4 Deployment

The deployed Pi4 copy lives at `/opt/pi4-noc` and runs as `pi4-noc.service` on `http://192.168.0.101/`.

Observed/controlled services include AdGuard Home, k3s, Samba, SSH, and the local k3s-hosted Uptime Kuma and GRID deployments.

The Pi5 portfolio health check uses its Tailscale Serve HTTPS endpoint because the API itself is intentionally loopback-only. Override the full probe URL with `PI4_NOC_PI5_PORTFOLIO_HEALTH_URL` when the tailnet hostname changes.

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

Backups are staged under `/mnt/ssd/backups/pi4` and copied off-host to `pi5@192.168.0.94:/home/pi5/backups/pi4` by default. The archive includes GRID vault/data, consistent SQLite snapshots for k3s and Uptime Kuma, k3s config/resource exports, AdGuard config/state except the large query log, Samba config, registry data, and host inventory. Critical missing sources fail the job. Restore drills require the archive checksum, extract the newest Pi5 archive into `/home/pi5/restore-drills/pi4`, run SQLite integrity checks, and record the Kuma monitor count without changing live services.

The Ops Center verifies local archive/checksum pairs against Pi5 by filename and byte size, and separately checks the last systemd result for both backup jobs. This catches a missed remote copy even while a newer backup remains fresh. Failed morning and critical notification deliveries use bounded retry cooldowns so an unavailable provider cannot trigger a request every five-minute check cycle.

Off-host alerts publish to an account-free ntfy topic configured with `PI4_NOC_NOTIFY_NTFY_URL`. The public topic name is a secret and must be a cryptographically random 16-64 character value kept only in protected runtime configuration.

Pi5 is the always-on delivery subscriber. `scripts/cabrera-alert-relay.py` polls the topic, commits new messages to the protected SQLite outbox at `/var/lib/cabrera-alert-relay/outbox.sqlite3`, and retries the existing email webhook without blocking the originating monitor. `cabrera-alert-relay.timer` runs the relay every five minutes; unsuccessful deliveries remain queued with bounded exponential backoff until the provider recovers. The Pi5 critical monitor reports relay timer freshness and pending/outdated outbox messages.

`scripts/install-macos-alert-subscriber.sh` is an optional convenience that shows native macOS notifications while a Mac is online. It is not part of the durable delivery path and may remain disabled.

Pi5 and Jetson host monitors use the same protected topic with their respective `PI5_ALERTS_NOTIFY_NTFY_*` and `JETSON_NOC_NOTIFY_NTFY_*` variables. `scripts/cabrera_notify.py` is their shared ntfy publisher; each monitor identifies the originating device in the alert title. The Pi5 relay owns eventual FormSubmit delivery for messages from all three hosts.

## Pi4 Power And Data HDD Health

`/mnt/ssd` is the compatibility mount path for the current rotational WDC USB data HDD; the dashboard labels it as `Data HDD` and reports the detected model, media type, transport, and rotational flag. Moving write-heavy data to a real SSD still requires a physical device and a controlled migration.

`pi4-boot-state.service` atomically records the running boot ID and marks it clean only from its shutdown `ExecStop`. The first installed boot is an unknown baseline; later unclean boots become critical Ops checks. State lives at `/var/lib/pi4-boot-state/state.json` and contains no secrets.

`pi4-log2ram-apply-fix` idempotently applies the log2ram 1.7.2 journal mirror correction and keeps a root-only rollback copy. `pi4-log2ram-guard.service` then fails visibly if a future log2ram update removes the correction or restores stale active journal filenames.

SMART reads use the root-owned fixed `smart_health` helper for the HDD's persistent WWN path with `-d sat`; the web service cannot supply a device or arbitrary smartctl arguments. Mount integrity is verified by filesystem UUID, so USB enumeration changes cannot silently point the checks at a future SSD. A standby-safe health poll runs from the Ops Center. Weekly short and monthly long self-tests are scheduled by `pi4-smart-short.timer` and `pi4-smart-long.timer`, outside the nightly backup window.

Uptime Kuma remains on Pi4 at `http://192.168.0.101:3001/`. The inspected database contained zero configured monitors, so the attempted Pi5 migration added no independent coverage and was abandoned; an independently configured off-host monitor remains future work.

## Pi4 Update Policy

APT refresh and unattended security updates are enabled. `config/apt/52pi4-maintenance.conf` explicitly disables unattended reboots, so kernel, EEPROM, and k3s restarts stay inside a maintenance window with backup, boot-state, storage, pod, and endpoint validation. Retain the previous k3s binary and kernel packages until the updated node has passed those checks.

## Remote Access

Cabrera Network is intended to stay private. Remote access should use Tailscale with the Pi4 as a subnet router for `192.168.0.0/24`; do not expose the dashboard with router port forwarding.

Once Tailscale is connected on an approved device, use the normal LAN URL:

```text
http://192.168.0.101/
```

The same subnet route keeps the dashboard's Web Apps links usable remotely, including AdGuard `:8080`, Uptime Kuma `:3001`, and GRID `:8090` / `:7777`.

## Validation Notes

- Backend syntax smoke: `python3 -m py_compile server/app.py server/sudo_ops.py server/tplink_collector.py`.
- Frontend build smoke: `npm run build`.
- Live validation: `systemctl is-active pi4-noc.service`, the Web Apps panel shows every link `online`, and the local GRID/Uptime Kuma service rows report `1/1 ready`.

See `MEMORY.md` for project memory, live service notes, and gotchas.
