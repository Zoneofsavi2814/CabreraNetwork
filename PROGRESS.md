# Cabrera Network Goal Progress

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
- `python3 -m unittest discover -s server -p 'test_*.py' -v` - pass, 39 tests
- `npm run build` - pass
- `git diff --check` - pass
- Pi4 deploy: `rsync ... && ssh pi4@192.168.0.101 'cd /home/pi4/cabrera-network-deploy/CabreraNetwork && scripts/install-pi.sh'` - pass

Live validation:
- `pi4-noc.service` active on Pi4.
- `http://192.168.0.101/` returned HTTP 200.
- `/api/session` returned `authenticated: false`.
- `systemctl show pi4-noc.service` includes `EnvironmentFile=-/etc/pi4-noc/notify.env` and `PI4_NOC_STATE_DIR=/var/lib/pi4-noc`.
- `/etc/pi4-noc/notify.env.example` exists as `root:pi4` `0640`; `/etc/pi4-noc/notify.env` is not configured yet.
- `/var/lib/pi4-noc` exists as `pi4:pi4` `0750`.
- One-off notification import on Pi4 reported `configured False` and state path `/var/lib/pi4-noc/notification-state.json`.
- Forced live `OPS_CENTER` refresh after loading k3s state returned 40 checks: 40 ok, 0 warn, 0 fail.
- No notification state file was created during force refresh.

Blockers:
- None for code/deploy. Delivery still needs `/etc/pi4-noc/notify.env` populated with SMTP and/or webhook settings.

Next action:
- Add real SMTP/webhook settings to `/etc/pi4-noc/notify.env`, restart `pi4-noc.service`, then send a controlled dry-run/test notification.
