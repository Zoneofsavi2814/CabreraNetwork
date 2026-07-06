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
