# Cabrera Network Memory

## 2026-07-21 Pi5 Application Migration

- Pi4 now hosts Wedding (`wedding` k3s namespace, public Funnel 443), EagleEye (`eagleeye`, host port 8098, DB `/mnt/ssd/apps/eagleeye/eagleeye.db`), Work Website (`cabrera-work-website`, host port 8081), CabreraPortfolio (`cabrera-portfolio.service`, loopback 8099/private Serve 9443), and CabreraPrograms (`cabrera-programs.service`, LAN 8096). Pi4's Tailscale DNS name is exactly `ann-and-chris.tail83be27.ts.net`; Pi5 is `rp5-spare.tail83be27.ts.net`.
- HomeTwin, Coinbot/Mission Control, Legacy Mission Control, and SoftwareOps were not migrated. Pi5's custom application units and k3s were stopped at `2026-07-21T20:42:03-06:00`; no Pi5 deletion is allowed before `2026-07-24T20:42:03-06:00` and a fresh gate audit.
- The Mac LaunchAgent `com.cabrera.portfolio.manual-sync` is the Portfolio browser collector and writes to `pi4@192.168.0.101:/var/lib/cabrera-portfolio/manual-portal-dashboard-values.json`. Pi4 has no Chromium profile or browser-sync service/timer.
- The durable alert relay now runs on Pi4 from `/opt/pi4-noc/scripts/cabrera-alert-relay.py`; its outbox remains `/var/lib/cabrera-alert-relay/outbox.sqlite3`. Pi5's relay and critical-alert timers are stopped.
- Backups are intentionally Pi4-local-only at `/mnt/ssd/backups/pi4`. They include retained application state/config, manifests, image digests, consistent SQLite snapshots, and Tailscale state. Isolated drills restore under `/mnt/ssd/restore-drills/pi4`; there is no Pi5, Mac, or cloud backup. This accepts simultaneous Pi4/HDD loss.
- Current five-minute checks cover Wedding public TLS/health, EagleEye, Work Website, CabreraPrograms, Portfolio loopback/Serve, GRID, WAN, DNS, and Pi4 power. Hourly/nightly checks cover retained k3s readiness/resource guardrails, local backup/checksum/drill health, ports, temperature, mounts, and HDD SMART.

## 2026-07-14 Pi4 Balanced Performance Profile

- The live Pi4 now boots headless into `multi-user.target` with the `ondemand` governor, a 600 MHz minimum, the stock 1.8 GHz `arm_boost=1` ceiling, KMS retained, and the unused audio/camera/display autodetection, Wi-Fi, Bluetooth, LightDM, WayVNC/display helpers, and Samba AD DC disabled. Canonical assets live in `config/performance/`; `scripts/pi4-performance-profile.sh audit|apply|verify` is idempotent and never reboots from `apply`.
- `/etc/rancher/k3s/config.yaml` is canonical and preserves the Pi4 node IP, advertise address, TLS SAN, `/mnt/ssd/k3s` data directory, `/mnt/ssd/k3s-storage` default path, and kubeconfig mode. Packaged `metrics-server` and `local-storage` are disabled (so `kubectl top` is intentionally absent); only GRID and CoreDNS remain. Uptime Kuma, `homelab-smoke`, and `local-registry` resources/manifests are retired, while `/mnt/ssd/podman/uptime-kuma-data` and `/mnt/ssd/registry` remain untouched.
- Dashboard collectors run at 2/15/60/120/1800-second hot/service/heavy/router/storage cadences. Static host facts, NAS size, batched systemd data, topology helpers, archive integrity, verified operations TLS, direct k3s API inventory, and revision-scoped SSE serialization are cached; the old single batched kubectl read remains only as a stale-value fallback.
- AdGuard query-log retention is 7 days (`604800000` ms) through its current API. All other query-log fields were preserved, the log was not cleared, and the DNS cache remains 64 MiB (`67108864`).
- Exact 15-minute before/after results: dashboard CPU fell from `9.586533%` to `1.425082%` of one core (passes the 1.5% gate); available-RAM p50 rose from `6,062,492` to `6,826,328` KiB (+745.9 MiB); frequency now spans 600 MHz–1.8 GHz; OpenSSL throughput improved 3.34%; and `throttled=0x0` throughout. Final temperature p50 was 37.485°C versus 38.9°C baseline (-1.415°C), short of a strict 2°C median target but directionally improved.
- The combined CPU gate remains structurally unattainable in the retained architecture: final `k3s.service` was `9.940509%` before adding the dashboard, and a separate attribution measured the k3s server process itself at `8.117%`. Final dashboard+k3s was `11.365591%` versus `18.099065%` baseline. Do not add speculative controller-disable flags; if ≤8% is non-negotiable, the next measured option is moving GRID out of k3s in a separate pass.
- Maintenance boot ID is `58288ad4-3f72-4f78-bc19-281cac2eda14`. Retained services, DNS, SMB listeners, dashboard auth, GRID listeners, CoreDNS/GRID readiness, Ethernet, and Tailscale are healthy; Tailscale advertises `192.168.0.0/24`. The user explicitly waived the 24-hour soak, and no soak automation remains scheduled.

## 2026-07-12 Fleet Email Pipeline Facts

- The email leg for ALL hosts is: monitor → shared private ntfy topic → Pi5 `cabrera-alert-relay` (`/opt/cabrera-alert-relay/`, oneshot+timer, outbox at `/var/lib/cabrera-alert-relay/outbox.sqlite3`) → FormSubmit → zoneofsavi@gmail.com. Pi4's "delivered" state usually means ntfy accepted it, not that the email went out — check the relay outbox when email is missing.
- FormSubmit rate-limits the form under alert bursts (HTTP 429; observed 2026-07-11 18:30 through at least 2026-07-12 12:19). The relay drains max 1 msg/run (~5 min timer) with capped exponential backoff. When a storm floods the topic, prune duplicates by setting `delivered_at` + a note in `last_error` (never DELETE rows).
- Pi5 monitor: `/opt/pi5-critical-alerts/pi5_alerts.py`, config `/etc/pi5-critical-alerts/{notify,monitor}.env`, staging `/home/pi5/cabrera-alert-deploy/`. New `PI5_ALERTS_SKIP_CHECKS` (comma-separated ids; `service-` prefix optional) is set to `service-nginx` because nginx was deliberately stopped+disabled 2026-07-11 22:30.
- Jetson monitor: `/opt/jetson-noc/jetson_monitor.py` (+`tests/`, run `python3 tests/test_jetson_monitor.py` from `/opt/jetson-noc`), config `/etc/jetson-noc/notify.env`, staging `/home/jetson/cabrera-alert-deploy/`. SSH aliases: `jetson` (192.168.0.227), `jetson-ts` (Tailscale), `frame-jetson` (robot subnet via Pi5 jump). Files are `jetson:jetson`-owned.
- Pi5/Jetson `redact()` strips ALL URLs (public-ntfy hardening) — never put dashboard links in their notification bodies/fields; that's why their `*_NOTIFY_LINK_URL` envs stay unset.
- Both monitors + relay now use the same email design language as Pi4 (emoji status-light subjects, structured NBSP-labeled fields, humanized times). Relay `pretty_subject()` avoids double icon prefixes for new-style titles.
- Pi5/Jetson monitor sources have no Mac-side canonical repo — deployed + staging copies are truth (historical copies in ~/Documents/Codex/* are stale).
- Known pending app issue: `cabrera-portfolio-manual-sync.service` on Pi5 exits 1 daily — "providers not modeled: canvas-credit-union" (CabreraPortfolio repo); it re-alerts on cooldown until modeled.

## 2026-07-11 Readable Notification Emails

- Notification emails (FormSubmit form mode) now send structured emoji-labeled fields instead of one `message` blob; each field renders as its own row in the provider's `table`/`box` template. Subjects lead with a status icon (🟢/🟡/🔴/🚨). Field labels use non-breaking spaces because PHP-style form backends rewrite spaces/dots in field names to underscores — keep `field_label()` when adding fields.
- `PI4_NOC_NOTIFY_LINK_URL` (falls back to `PI4_NOC_NOTIFY_NTFY_CLICK_URL`) controls the Dashboard row in emails; live Pi4 `notify.env` sets it to `http://192.168.0.101/` while `PI4_NOC_NOTIFY_NTFY_CLICK_URL` intentionally stays `http://192.168.0.101:8090/` (GRID Wiki) for ntfy taps.
- The legacy body-blob form payload (`name`/`email`/`subject`/`message`/...) remains the fallback when `send_webhook` gets no `fields`; JSON webhook mode gains an optional `fields` object.
- `cabrera-alert-relay.py` deploys to Pi5 at `/opt/cabrera-alert-relay/cabrera-alert-relay.py` (pi5:pi5 0755, oneshot+timer, no restart needed); ops center `notifications.py` deploys to Pi4 `/opt/pi4-noc/server/` (root:root 0644) + `systemctl restart pi4-noc.service`. Targeted single-file deploys are fine for backend-only changes; full `install-pi.sh` requires a fresh `dist/` build.
- Observed 2026-07-11: FormSubmit rate-limited the Pi5 relay (HTTP 429) with ~9 alerts queued in the durable outbox; the outbox drains with capped exponential backoff, so bursts of alerts can arrive hours late.

## 2026-07-09 Power, Storage, And Off-Host Monitoring

- 2026-07-09 power/storage follow-through adds atomic clean-shutdown tracking in `/var/lib/pi4-boot-state/state.json`, a SMART health helper and self-test timers pinned to the HDD's persistent WWN, strict UUID/filesystem/rw mount validation, a guarded log2ram journal fix, backup service-result checks, and exact local-to-Pi5 backup artifact parity.
- `/mnt/ssd` remains the compatibility path but is physically a rotational, bus-powered WDC WD20SDRW USB HDD. Dashboard labels and storage metadata say `Data HDD`; a real SSD migration remains hardware-gated.
- Uptime Kuma was retired from Pi4 on 2026-07-14 after its database was confirmed to contain zero monitors; its historical data directory remains untouched. The earlier attempted Pi5 migration was abandoned and its temporary Pi5 resources were removed. Notification state records delivery attempts/successes/failures and retries a failed morning digest on a bounded 30-minute cadence.
- Backup jobs now fail on missing critical sources, snapshot k3s and Kuma with SQLite's backup API, and require checksum plus SQLite integrity validation during the off-host restore drill.

## 2026-07-05 Personal Operations Center

- 2026-07-06 follow-up added Pi4 backup/restore-drill automation: `/usr/local/sbin/pi4-backup`, `/usr/local/sbin/pi4-restore-drill`, `pi4-backup.timer`, and `pi4-restore-drill.timer`. Local archives live under `/mnt/ssd/backups/pi4`; off-host copies go to `pi5@192.168.0.94:/home/pi5/backups/pi4`; restore drills extract on Pi5 under `/home/pi5/restore-drills/pi4`.
- Ops Center now watches Pi4 power/throttle flags, off-host backup freshness, backup and restore-drill timers, k3s deployment resource guardrails, and stable open-port drift. Existing kernel storage checks also include under-voltage messages.
- Pi4 k3s hygiene target: keep GRID in `homelab` for now, with resource guardrails and custom role labels. Future namespace separation can move core/apps without changing current hostPort ownership in this pass.
- The Ops panel now includes `OPS_CENTER` from the Flask sidecar: five-minute, hourly, morning, and nightly cadences are configured in `server/app.py`.
- Five-minute checks watch gateway, DNS, WAN HTTPS, Pi4 dashboard/GRID, and Pi5 service endpoints for Coinbot, Mission Control, EagleEye, CabreraPrograms, and Portfolio API.
- Hourly checks include a lightweight WAN speed sample, latest k3s GitHub release lookup, `/mnt/ssd/backups` freshness, Pi4 k3s node/workload readiness, GRID schema health, and Pi5 portfolio web.
- Hourly backup verification checks the newest backup set for real file content, and Pi5 k3s readiness is checked from Pi4 over SSH via `PI4_NOC_PI5_SSH_TARGET` (default `pi5@192.168.0.94`) using `sudo -n k3s kubectl`.
- Follow-up hardening added read-only deeper health checks: timer last-trigger freshness, kernel storage/I/O journal pattern scans, inode usage and read-only mount detection for disk checks, GRID vault/index sync freshness from `/api/stats`, backup retention pressure, and a morning overnight-storage-events scan.
- Backup artifact integrity checks now read recent `.tgz` archives and verify `.sha256` files whose targets live in the same backup folder; cross-backup/protected checksum references are counted as skipped metadata.
- Five-minute internet monitoring now includes multi-endpoint HTTP latency and DNS latency probes. Nightly sync/parity coverage includes a GRID vault path parity check between `/mnt/nas/brain` and `/mnt/ssd/nas/brain`; unreadable protected note contents fall back to metadata parity.
- Morning and nightly are wall-clock schedules, not service-start intervals: morning next-runs at `07:00`, nightly at `23:55` local time. Nightly checks watch log/cleanup timers, package DB backup timer, filesystem trim, Data HDD health/mount/SMART, GRID brain mount, and GRID brain freshness.
- Ops notifications are configurable through `/etc/pi4-noc/notify.env` with a sample at `/etc/pi4-noc/notify.env.example`; delivery supports SMTP email, a generic JSON webhook, or `PI4_NOC_NOTIFY_WEBHOOK_FORMAT=form` for form-encoded relay providers such as FormSubmit, with dedupe state in `/var/lib/pi4-noc/notification-state.json`.
- The compact UI prioritizes warn/fail checks before OK checks and shows a `+N more checks tracked` row when a cadence has more than five checks. Current live warning after deploy was `Coinbot API: degraded=true`; all other live ops checks passed.

## 2026-06-09 Probe-Based Status, Speed + Mobile Pass

- Web app status is now TCP-probe-based (`probe_ports` in `server/app.py`), not `ss` LISTEN-set membership. GRID uses k3s `hostPort` exposure, which relies on iptables DNAT and never creates a LISTEN socket. Labels are `online`/`offline`; Uptime Kuma has since been retired.
- GRID appears in Services as a local k3s-backed row (`kind: "k3s"` in `UNIT_CONFIG`, status from cached K3S workload ready/desired). Restart maps to `kubectl rollout restart`; logs use the `k3s-workload` source in `/api/logs`.
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
- The full subnet route keeps existing Web Apps links usable remotely: AdGuard `:8080` and GRID `:8090` / `:7777`.
- Linux clients may need `sudo tailscale set --accept-routes`; macOS, iOS, Windows, and Android normally pick up approved subnet routes automatically.

## Related Systems

- AdGuard Home is active on DNS port `53` and admin UI port `8080`.
- Uptime Kuma, `homelab-smoke`, and `local-registry` were retired from Pi4 k3s on 2026-07-14. `/mnt/ssd/podman/uptime-kuma-data` and `/mnt/ssd/registry` remain untouched; the old `container-uptime-kuma.service` remains retired.
- GRID runs as k3s deployment `homelab/grid` with hostPorts `8090` (wiki) and `7777` (protected listener/API); `grid.service` is retired.
- k3s, Samba, SSH, AdGuard, and GRID are intentionally observed/controlled through fixed allowlists only.
