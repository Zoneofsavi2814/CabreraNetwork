# Cabrera Network Goal Progress

## 2026-07-14 Pi4 Balanced Performance And Low-Idle Optimization

Current milestone: implementation and maintenance-window rollout complete; the user waived the 24-hour soak.

Implemented:

- Split the dashboard collector into bounded 2/15/60/120/1800-second workers, moved NAS `du` to its own stale-retaining worker, cached static host facts, batched systemd/k3s reads, bounded topology name resolution, shared serialized SSE snapshots by revision, and cached unchanged backup archive/checksum validation.
- Added a reusable verified direct k3s API client with anonymous in-memory certificate/key descriptors and one batched kubectl fallback. Added a shared certificate-verifying TLS opener for operations checks; no kubectl process appeared in 8,945 0.1-second child scans during the final run.
- Retired Uptime Kuma, `homelab-smoke`, and `local-registry` from k3s and the dashboard, removed their obsolete manifests, and retained only the shared `homelab` namespace manifest. Historical Kuma/registry data directories were not deleted.
- Added and applied the idempotent `pi4-performance-profile.sh`: ondemand 600 MHz–1.8 GHz, headless multi-user boot, unused desktop/display/radio/Samba-AD units disabled, conservative boot overlays, canonical k3s config with `metrics-server`/`local-storage` disabled, and seven-day AdGuard query-log retention with the 64 MiB DNS cache unchanged.
- Restored and verified Tailscale subnet advertisement for `192.168.0.0/24` after the final reboot audit found the persisted route preference empty.

Acceptance results:

| Gate | Result | Status |
| --- | ---: | --- |
| Dashboard idle CPU | `9.586533%` → `1.425082%` of one core | Pass (≤1.5%) |
| Dashboard + k3s CPU | `18.099065%` → `11.365591%` | Fail (≤8%); k3s alone is `9.940509%` |
| Available RAM p50 | +745.9 MiB | Pass (≥500 MiB) |
| CPU frequency | 600 MHz min, 1.0 GHz p50, 1.8 GHz p95/max | Pass |
| Throttling | `0x0` | Pass |
| OpenSSL throughput | +3.34% | Pass (within 5%) |
| DNS/dashboard/GRID p95 | Apples-to-apples post-run ratios within 10%; final 61/61 probes passed | Pass |
| Median temperature | 38.9°C → 37.485°C (-1.415°C) | Improved; below the approximate 2°C target |
| Required services/listeners | DNS, SMB, SSH, Tailscale route, dashboard auth, k3s node, GRID/CoreDNS, both GRID listeners | Pass |

Validation:

- Full repository suite: 134 Python tests pass; Vite production build and precompression pass; Python compileall, Bash syntax, ShellCheck, and `git diff --check` pass.
- Pi-side targeted tests, profile `verify`, systemd state, canonical config, retained-manifest client/server dry-runs, direct k3s TLS/memfd smoke, endpoints, kernel-error scan, and deployed-source hashes pass.
- Final exact sample ran `2026-07-14 21:20:17.997`–`21:35:17.998 MDT` for `900.000508s`; no failed units, kernel error matches, throttling flags, endpoint errors, or kubectl fallback were observed.

Remaining constraint:

- A separate 240-second attribution measured `k3s.service` at 9.251% and the k3s-server process at 8.117%, before retained pod CPU. No evidence-backed low-risk k3s flag remains within this plan. Rolling back successful dashboard/host changes would worsen CPU/RAM, so no cohort was reverted; meeting the strict combined 8% gate requires a separately approved architecture change such as moving GRID out of k3s.

## 2026-07-12 Fleet-Wide Email Restyle, Missing Digest Root Cause

Current milestone: extend the readable email format to Pi5 and Jetson monitors; diagnose and mitigate the missing 07:00 digest.

Root cause of the missing 2026-07-12 morning digest:
- Pi4's ops center delivers via ntfy first (recorded as success); the actual EMAIL leg is the Pi5 relay draining the shared ntfy topic into FormSubmit.
- An alert storm (26x Pi5 nginx, 15x Jetson failed units, 14x Pi5 failed units, 10x Pi4 relays of the same) got the FormSubmit form rate-limited (HTTP 429) starting 2026-07-11 ~18:30; the backlog grew to 73 and the digest was queued behind it. FormSubmit still returned 429 to a clean single attempt at 12:19 on 07-12; delivery auto-resumes when the provider lifts the throttle.
- Storm roots: Pi5 nginx was deliberately stopped+disabled 2026-07-11 22:30 but the Pi5 monitor still watched it (fixed via new `PI5_ALERTS_SKIP_CHECKS=service-nginx` in `/etc/pi5-critical-alerts/monitor.env`); Jetson `gitlab-runner-storage-maintenance.service` failed on a missing `/srv/gitlab-runner/shell/cache` dir (created `2770 gitlab-runner:gitlab-runner`, unit now clean); Pi5 `cabrera-portfolio-manual-sync.service` exits 1 on an app-level gap ("providers not modeled: canvas-credit-union") — NOT fixed here, belongs to CabreraPortfolio.
- Mitigation: 73 stale duplicate alerts in the relay outbox were marked delivered with `last_error='pruned 2026-07-12: ...'` (reversible); the digest is the sole pending row and retries with backoff.

Files changed (this repo):
- `scripts/cabrera-alert-relay.py` — `pretty_subject()` guard so new-style titles that already lead with a status icon are not double-prefixed; deployed to Pi5.
- `server/test_alert_relay.py` — double-prefix regression test (5 tests total).
- `PROGRESS.md`, `MEMORY.md`.

Deployed outside this repo (no local canonical source; on-device staging dirs refreshed):
- Pi5 `/opt/pi5-critical-alerts/pi5_alerts.py` (+ `/home/pi5/cabrera-alert-deploy/`) — full restyle: emoji status-light subjects, humanized times, structured FormSubmit fields, `PI5_ALERTS_SKIP_CHECKS` env filter, `PI5_ALERTS_NOTIFY_LINK_URL` support (left unset: its redact() strips URLs by design), restyled `--send-test`.
- Jetson `/opt/jetson-noc/jetson_monitor.py` + `tests/` (+ `/home/jetson/cabrera-alert-deploy/`) — same restyle; on-device tests pass (7/7).

Validation:
- Repo: `python3 -m unittest server.test_alert_relay` — 5 tests pass.
- Pi5: `--json --no-notify` run shows 40 checks with nginx skipped; systemd-triggered run clean ("1 failed, 2 warning" — the portfolio sync, known).
- Jetson: 7/7 on-device tests; systemd-triggered run clean with new-format output; failed-units back to 0.

Blockers:
- FormSubmit 429 persists as of 12:19; digest delivery is queued and will self-resume. If throttling recurs, consider SMTP or ntfy phone app as the alert channel.
- `canvas-credit-union` provider modeling in CabreraPortfolio still fails daily and will re-alert (by design) until fixed.

Next action:
- Confirm digest arrival (watcher armed); fix canvas-credit-union modeling in the CabreraPortfolio repo; consider re-pointing `PI5_ALERTS_SKIP_CHECKS` if nginx ever returns.

## 2026-07-11 Readable Notification Emails

Current milestone: redesigned FormSubmit email format for ops digests, critical alerts, and the alert relay.

Files changed:
- `server/notifications.py`
- `server/test_ops_center.py`
- `server/test_alert_relay.py`
- `scripts/cabrera-alert-relay.py`
- `scripts/pi4-noc.notify.env.example`
- `PROGRESS.md`

What changed:
- Subjects now lead with a status icon so the inbox reads like a status light: `🟢/🟡/🔴 Cabrera Network · Morning report — ...` and `🚨 Cabrera Network · <check> on <host> is failing`.
- Form-mode webhook delivery (FormSubmit) now sends structured fields instead of one `message` blob; each field renders as its own labeled row in the provider's `table`/`box` template: Report, Scorecard, Failing now / Warnings / All clear, Morning checks, Snapshot, Dashboard (and What broke / Still failing / Detected for criticals).
- Legacy plumbing fields (`name`, `email`, `subject`, `source`, `severity`, `sentAt`) no longer appear as visible email rows in structured mode; the body-blob payload remains as fallback when no fields are provided.
- Field labels use non-breaking spaces because PHP-style form backends rewrite spaces/dots in field names to underscores.
- Check lines are now `🔴 Pi4 · Data HDD SMART — message (cadence)`; timestamps are humanized (`Saturday, July 11 · 7:00 AM`); all-green digests get friendly all-clear copy.
- Plain-text bodies (SMTP, ntfy, JSON webhook) restructured with the same sections; JSON webhook payload gains an optional `fields` object.
- New optional `PI4_NOC_NOTIFY_LINK_URL` adds a dashboard link row (falls back to `PI4_NOC_NOTIFY_NTFY_CLICK_URL`).
- `cabrera-alert-relay.py` form deliveries now send `🚨 Alert / 📝 Details / 📟 Severity / 🕐 Received / 📨 Trace` rows with a severity-icon subject.

Commands run:
- `python3 -m unittest discover -s server -p 'test_*.py'` - pass, 82 tests (5 new).
- `python3 -m py_compile server/app.py server/notifications.py scripts/cabrera-alert-relay.py` - pass.

Blockers:
- None. Frontend untouched; no rebuild needed. Redeploy `server/notifications.py` (pi4-noc) and `scripts/cabrera-alert-relay.py` to the Pi to take effect.

Next action:
- Deploy to the Pi and eyeball the next morning digest; optionally set `PI4_NOC_NOTIFY_LINK_URL=http://192.168.0.101/` in `/etc/pi4-noc/notify.env` and try `PI4_NOC_NOTIFY_WEBHOOK_TEMPLATE=box` if the boxed layout reads better than the table.

## 2026-07-05 Homelab Personal Operations Center

Current milestone: Ops Center email/webhook notifications for the Pi4/Pi5 operations center.

Files changed:
- `server/app.py`
- `server/notifications.py`
- `server/test_ops_center.py`
- `scripts/install-pi.sh`
- `scripts/pi4-noc.service`
- `scripts/pi4-noc.notify.env.example`
- `README.md`
- `MEMORY.md`
- `PROGRESS.md`

Commands run:
- `python3 -m py_compile server/app.py server/notifications.py server/sudo_ops.py server/tplink_collector.py server/test_ops_center.py` - pass
- `python3 -m unittest discover -s server -p 'test_*.py' -v` - pass, 41 tests
- `npm run build` - pass
- `git diff --check` - pass
- Pi4 deploy: `rsync ... && ssh pi4@192.168.0.101 'cd /home/pi4/cabrera-network-deploy/CabreraNetwork && scripts/install-pi.sh'` - pass

Live validation:
- `pi4-noc.service` active on Pi4.
- `http://192.168.0.101/` returned HTTP 200.
- `/api/session` returned `authenticated: false`.
- `systemctl show pi4-noc.service` includes `EnvironmentFile=-/etc/pi4-noc/notify.env` and `PI4_NOC_STATE_DIR=/var/lib/pi4-noc`.
- `/etc/pi4-noc/notify.env.example` exists as `root:pi4` `0640`; `/etc/pi4-noc/notify.env` exists as `root:pi4` `0640` and points form webhook delivery at FormSubmit for `zoneofsavi@gmail.com`.
- `/var/lib/pi4-noc` exists as `pi4:pi4` `0750`.
- One-off notification import with `/etc/pi4-noc/notify.env` sourced reported `configured True`, `webhook_configured True`, and `email_configured False`.
- FormSubmit activation email was accepted by the recipient, and Pi4 controlled test notifications returned `sent True`.
- Forced live `OPS_CENTER` refresh after loading k3s state returned 40 checks: 39 ok, 1 warn, 0 fail; the warning was `Coinbot API: degraded=true`.
- No notification state file was created by force refresh or direct test sends.

Blockers:
- None for code/deploy. FormSubmit is active; a future SMTP account can replace or supplement it if wanted.

Next action:
- Watch the next `07:00` morning digest and any future `fail` check to confirm normal scheduled dispatch writes `/var/lib/pi4-noc/notification-state.json`.
# 2026-07-21 Pi5 → Pi4 Migration

- Wedding, EagleEye, CabreraPortfolio, CabreraWorkWebsite, and CabreraPrograms are live on Pi4; the exact wedding hostname/QR now resolves to Pi4 and Portfolio remains private on Tailscale `:9443`.
- Pi4 passed retained-state checks, a reboot, a local backup, and two isolated restore drills. The Mac Portfolio collector completed a Pi4-targeted run; the durable alert relay/outbox is also on Pi4.
- Pi5 is `rp5-spare`; all custom application services/timers and k3s were stopped and runtime-masked at `2026-07-21T20:42:03-06:00`. Logical application/data deletion is gated until at least `2026-07-24T20:42:03-06:00` plus a fresh acceptance audit.
