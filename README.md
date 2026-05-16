# Cabrera Network

Raspberry Pi 4 control-room dashboard for the home network. It combines a Vite/React UI with a Flask sidecar that reports host metrics, services, Podman containers, AdGuard, k3s, topology, logs, allowlisted actions, and web app links.

## Local Development

```bash
npm ci
npm run build
python3 -m py_compile server/app.py server/sudo_ops.py server/tplink_collector.py
```

## Pi4 Deployment

The deployed Pi4 copy lives at `/opt/pi4-noc` and runs as `pi4-noc.service` on `http://192.168.0.101/`.

```bash
npm run build
scripts/install-pi.sh
```

See `MEMORY.md` for project memory, live service notes, and gotchas.
