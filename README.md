# Cabrera Network

Raspberry Pi 4 control-room dashboard for the home network. It combines a Vite/React UI with a Flask sidecar that reports host metrics, services, Podman containers, AdGuard, k3s, topology, logs, allowlisted actions, and web app links.

## Local Development

```bash
npm ci
npm run build
python3 -m py_compile server/app.py server/sudo_ops.py server/tplink_collector.py
```

## Pi4 Deployment

The deployed Pi4 copy lives at `/opt/pi4-noc` and runs as `pi4-noc.service` on `http://192.168.0.101/` and `http://cabrera.home.arpa/`.

Observed/controlled services include AdGuard Home, k3s, Uptime Kuma, Samba, SSH, and the Podman socket.

```bash
npm run build
scripts/install-pi.sh
```

## Remote Access

Cabrera Network is intended to stay private. Remote access should use Tailscale with the Pi4 as a subnet router for `192.168.0.0/24`; do not expose the dashboard with router port forwarding.

Once Tailscale is connected on an approved device, use the normal LAN URL:

```text
http://192.168.0.101/
```

The same subnet route keeps the dashboard's Web Apps links usable remotely, including `adguard.home.arpa:8080`, `kuma.home.arpa:3001`, and `grid.home.arpa:8090`.

## Validation Notes

- Backend syntax smoke: `python3 -m py_compile server/app.py server/sudo_ops.py server/tplink_collector.py`.
- Frontend build smoke: `npm run build`.
- Live validation should confirm the Web Apps panel lists only current LAN links.

See `MEMORY.md` for project memory, live service notes, and gotchas.
