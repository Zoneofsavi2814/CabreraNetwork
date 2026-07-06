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

Observed/controlled services include AdGuard Home, k3s, Samba, SSH, and the k3s-hosted Uptime Kuma and GRID deployments (`homelab` namespace, exposed via hostPorts).

```bash
npm run build
scripts/install-pi.sh
```

## Ops Notifications

The Ops Center can send the `Every morning` digest and any critical red alert (`fail` checks) by email or webhook. Copy `/etc/pi4-noc/notify.env.example` to `/etc/pi4-noc/notify.env`, fill in SMTP and/or webhook settings, and restart `pi4-noc.service`.

Notification state is stored in `/var/lib/pi4-noc/notification-state.json` so morning digests send once per day and critical failures are deduped until recovery or the configured cooldown.

Webhook delivery defaults to JSON. Set `PI4_NOC_NOTIFY_WEBHOOK_FORMAT=form` for form-encoded relay providers such as FormSubmit.

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
- Live validation: `systemctl is-active pi4-noc.service`, the Web Apps panel shows every link `online`, and the GRID/Uptime Kuma service rows report `1/1 ready`.

See `MEMORY.md` for project memory, live service notes, and gotchas.
