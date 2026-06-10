# Cabrera Network Memory

## 2026-06-09 Probe-Based Status, Speed + Mobile Pass

- Web app status is now TCP-probe-based (`probe_ports` in `server/app.py`), not `ss` LISTEN-set membership. GRID and Uptime Kuma moved to k3s with `hostPort` exposure, which uses iptables DNAT and never creates LISTEN sockets — the old check showed them "not listening" while they worked. Labels are now `online`/`offline`.
- Uptime Kuma and GRID appear in Services as k3s-backed rows (`kind: "k3s"` in `UNIT_CONFIG`, status from cached K3S workload ready/desired). Restart maps to `kubectl rollout restart`; logs use the new `k3s-workload` source in `/api/logs` (allowlisted via `ALLOWED_WORKLOAD_LOGS`). `container-uptime-kuma.service` is retired.
- Geist/Geist Mono are self-hosted (`src/fonts/*.woff2`, latin variable subsets, @font-face in `styles.css`); no Google Fonts requests.
- `npm run build` precompresses dist via `scripts/precompress.mjs`; Flask serves `.gz` siblings (`send_dist`), gzips JSON >1KB, and sends immutable cache headers for `/assets/`. Flask's built-in static handler is disabled (`static_folder=None`) — don't re-enable it or assets bypass compression/caching.
- `/api/events` SSE ticks every 2s and skips unchanged payloads (keepalive comments instead).
- `overflow-x` on html/body must stay `clip`, never `hidden` — `hidden` makes body a scroll container and silently breaks the sticky `.topbar`/`.mobile-header`.
- `index.html` carries `viewport-fit=cover` + apple-mobile-web-app meta; safe-area insets are applied in the mobile/tablet media queries. The aurora animation is held still on ≤820px for battery.
- Local dev: `vite.config.js` proxies `/api` to the live Pi (`PI4_NOC_API` overrides the target).

## 2026-05-16 Repository Setup

- Canonical Mac source now lives at `/Users/christophercabrera/Desktop/GitlabRepos/CabreraNetwork`.
- Original working source was copied from `/Users/christophercabrera/Desktop/Sandbox/RaspberryPi4/Rp4`.
- Git remote is `git@gitlab.com:quintero4/CabreraNetwork.git` (GitLab).
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
  - GRID Wiki: `http://192.168.0.101:8090/`
  - GRID protected listener/API: `http://192.168.0.101:7777/`
- All Podman-era workloads are decommissioned and must not reappear in the dashboard (web apps, services, or mock data). Podman containers are no longer collected or shown — app workloads run in k3s.

## Deployment And Validation

- Build locally with `npm run build`.
- Backend syntax smoke: `python3 -m py_compile server/app.py server/sudo_ops.py server/tplink_collector.py`.
- Install to the Pi4 with `scripts/install-pi.sh` after a frontend build.
- Installer ensures `python3-pamela` is installed for PAM auth and creates `/etc/pi4-noc/session-secret` when missing.
- Live validation should confirm:
  - `systemctl is-active pi4-noc.service`
  - unauthenticated `/api/snapshot` and `/api/events` return `401`
  - `/api/session` returns `200` with `authenticated: false` before login
  - the Web Apps panel lists the current LAN links with `online` status pills

## Remote Access

- Remote access is private through Tailscale; do not add router port forwarding for the dashboard.
- Pi4 is configured as the subnet router for `192.168.0.0/24`, so approved tailnet devices can use the normal LAN URL `http://192.168.0.101/` away from home.
- The full subnet route keeps existing Web Apps links usable remotely: AdGuard `:8080`, Uptime Kuma `:3001`, and GRID `:8090` / `:7777`.
- Linux clients may need `sudo tailscale set --accept-routes`; macOS, iOS, Windows, and Android normally pick up approved subnet routes automatically.

## Related Systems

- AdGuard Home is active on DNS port `53` and admin UI port `8080`.
- Uptime Kuma runs as k3s deployment `homelab/uptime-kuma` with hostPort `3001` (the old `container-uptime-kuma.service` is retired).
- GRID runs as k3s deployment `homelab/grid` with hostPorts `8090` (wiki) and `7777` (protected listener/API); `grid.service` is retired.
- k3s, Samba, SSH, AdGuard, Uptime Kuma, and GRID are intentionally observed/controlled through fixed allowlists only.
