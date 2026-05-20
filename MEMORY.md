# Cabrera Network Memory

## 2026-05-16 Repository Setup

- Canonical Mac source now lives at `/Users/christophercabrera/Desktop/repos/CabreraNetwork`.
- Original working source was copied from `/Users/christophercabrera/Desktop/Sandbox/RaspberryPi4/Rp4`.
- GitHub remote is `git@github.com:Zoneofsavi2814/CabreraNetwork.git`.
- Initial working branch for the imported dashboard is `initial`.

## Dashboard Architecture

- Cabrera Network is a Vite/React frontend plus a Flask sidecar backend.
- The deployed Pi4 copy lives at `/opt/pi4-noc` and is managed by `pi4-noc.service`.
- The service binds to `0.0.0.0:80`; LAN URL is `http://192.168.0.101/`.
- The service name, install path, and environment variable prefix remain `pi4-noc` / `PI4_NOC_*` for compatibility, even though the UI is branded Cabrera Network.
- The frontend entrypoints are `src/main.jsx` and `src/App.jsx`; backend entrypoint is `server/app.py`.
- Privileged read/actions are centralized through `server/sudo_ops.py` and installed with `/etc/sudoers.d/pi4-noc`.

## Auth And Secrets

- The GUI has a PAM-backed login screen labeled `Enter Password`; it validates against the local Pi4 `pi4` login password.
- Browser auth uses an HttpOnly Flask session cookie. Do not add bearer/localStorage URL-token auth for the GUI.
- Unauthenticated routes are limited to `/api/session`, `/api/login`, and `/api/logout`; other `/api/*` routes and `/api/events` require a session.
- The deployed session secret is stored on the Pi at `/etc/pi4-noc/session-secret`; never copy that secret or Pi passwords into the repo.
- Router credentials, AdGuard credentials, and GRID auth token are internal service credentials and must not be removed as part of GUI auth work.

## Live Pi4 Services And Web Apps

- The dashboard web-app inventory currently includes:
  - Cabrera Network: `http://192.168.0.101/`
  - AdGuard Home: `http://192.168.0.101:8080/`
  - Uptime Kuma: `http://192.168.0.101:3001/`
  - Coinbot-Mission-Control: `http://192.168.0.101:8088/`
  - GRID Wiki: `http://192.168.0.101:8090/`
  - GRID protected listener/API: `http://192.168.0.101:7777/`
  - Esty: `http://192.168.0.101:8095/`
- Rootful Podman workloads shown in the GUI are app containers only. Podman infra/pause containers are hidden because they are implementation details of pods.
- `localhost/podman-pause:4.3.1-0` is Podman’s required pod infra image; do not delete it while the pod exists.
- Coinbot and Mission Control run in rootful Podman pod `cabrera-mission-control`.
- The Mission Control container is named `Coinbot-Mission-Control`; Coinbot container is named `coinbot`.
- Esty is managed as `container-esty.service`, publishes port `8095`, and should appear in Services, Containers, and Web Apps when discovered live. Mock fallback data includes the same service/container/web-app entries.

## Deployment And Validation

- Build locally with `npm run build`.
- Backend syntax smoke: `python3 -m py_compile server/app.py server/sudo_ops.py server/tplink_collector.py`.
- Install to the Pi4 with `scripts/install-pi.sh` after a frontend build.
- Installer ensures `python3-pamela` is installed for PAM auth and creates `/etc/pi4-noc/session-secret` when missing.
- Live validation should confirm:
  - `systemctl is-active pi4-noc.service`
  - unauthenticated `/api/snapshot` and `/api/events` return `401`
  - `/api/session` returns `200` with `authenticated: false` before login
  - the Web Apps panel lists the current LAN links
  - the Podman list hides infra/pause containers and shows `Coinbot-Mission-Control`
  - Esty shows as `container-esty.service`, has port `8095` listening, and opens `http://192.168.0.101:8095/`

## Remote Access

- Remote access is private through Tailscale; do not add router port forwarding for the dashboard.
- Pi4 is configured as the subnet router for `192.168.0.0/24`, so approved tailnet devices can use the normal LAN URL `http://192.168.0.101/` away from home.
- The full subnet route keeps existing Web Apps links usable remotely: AdGuard `:8080`, Uptime Kuma `:3001`, Coinbot-Mission-Control `:8088`, GRID `:8090`, and Esty `:8095`.
- Linux clients may need `sudo tailscale set --accept-routes`; macOS, iOS, Windows, and Android normally pick up approved subnet routes automatically.

## Related Systems

- AdGuard Home is active on DNS port `53` and admin UI port `8080`.
- Uptime Kuma is managed by `container-uptime-kuma.service` and listens on port `3001`.
- Esty is managed by `container-esty.service` and listens on port `8095`.
- GRID is managed by `grid.service`, publishing `8090:8080` and `7777:7777`.
- k3s, Samba, SSH, AdGuard, Podman, Uptime Kuma, and Esty are intentionally observed/controlled through fixed allowlists only.
- Coinbot and Mission Control were removed from the RP5 on 2026-05-16 after Pi4 health validation passed.
