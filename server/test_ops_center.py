import importlib.util
import json
import time
import unittest
import sys
import tempfile
import threading
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

try:
    import psutil  # noqa: F401
except ModuleNotFoundError:
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.net_io_counters = lambda **kwargs: types.SimpleNamespace(bytes_recv=0, bytes_sent=0)
    fake_psutil.disk_io_counters = lambda **kwargs: types.SimpleNamespace(read_bytes=0, write_bytes=0)
    fake_psutil.disk_usage = lambda path: types.SimpleNamespace(percent=0)
    fake_psutil.cpu_percent = lambda interval=None: 0
    fake_psutil.boot_time = lambda: 0
    sys.modules["psutil"] = fake_psutil

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}

        def after_request(self, fn):
            return fn

        def before_request(self, fn):
            return fn

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def post(self, *args, **kwargs):
            return lambda fn: fn

        def delete(self, *args, **kwargs):
            return lambda fn: fn

        def run(self, *args, **kwargs):
            return None

    fake_flask.Flask = FakeFlask
    fake_flask.Response = lambda *args, **kwargs: None
    fake_flask.jsonify = lambda value=None, *args, **kwargs: value
    fake_flask.request = types.SimpleNamespace(args={}, remote_addr="local", get_json=lambda *args, **kwargs: {})
    fake_flask.send_from_directory = lambda *args, **kwargs: None
    fake_flask.session = {}
    sys.modules["flask"] = fake_flask

from server import app as appmod
from server import notifications as notifymod
from server import sudo_ops

BOOT_STATE_SPEC = importlib.util.spec_from_file_location(
    "pi4_boot_state",
    Path(__file__).resolve().parent.parent / "scripts" / "pi4-boot-state.py",
)
bootstate = importlib.util.module_from_spec(BOOT_STATE_SPEC)
assert BOOT_STATE_SPEC.loader is not None
BOOT_STATE_SPEC.loader.exec_module(bootstate)

ALERT_SUBSCRIBER_SPEC = importlib.util.spec_from_file_location(
    "cabrera_alerts_subscriber",
    Path(__file__).resolve().parent.parent / "scripts" / "cabrera-alerts-subscriber.py",
)
alert_subscriber = importlib.util.module_from_spec(ALERT_SUBSCRIBER_SPEC)
assert ALERT_SUBSCRIBER_SPEC.loader is not None
ALERT_SUBSCRIBER_SPEC.loader.exec_module(alert_subscriber)


def datetime_from_parts(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute).timestamp()


def ts_parts(value):
    d = datetime.fromtimestamp(value)
    return d.year, d.month, d.day, d.hour, d.minute


class FakeResponse:
    def __init__(self, body: str, status: int = 200):
        self.body = body.encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return self.body

    def getcode(self):
        return self.status


class RecordingNotificationManager(appmod.NotificationManager):
    def __init__(self, state_path: Path):
        super().__init__({"PI4_NOC_NOTIFY_WEBHOOK_URL": "http://notify.test", "PI4_NOC_NOTIFY_STATE": str(state_path)})
        self.sent = []

    def send(self, subject: str, body: str, *, severity: str = "info", fields: dict | None = None) -> bool:
        self.sent.append({"subject": subject, "body": body, "severity": severity, "fields": fields})
        return True


def ops_snapshot(*checks):
    cadences = [{"id": "five-minute", "label": "Every 5 minutes", "checks": list(checks)}]
    return {
        "schemaVersion": 1,
        "updatedAt": datetime.now().isoformat(),
        "summary": appmod.summarize_operations(cadences),
        "cadences": cadences,
        "events": [],
    }


def ops_check(id_, status, message="detail", label=None, host="Pi4"):
    # Synthetic values in legacy handler tests below are not current monitor
    # targets; the live contract is asserted from OPS_CHECK_CONFIG above.
    return {
        "id": id_,
        "label": label or id_.replace("-", " ").title(),
        "host": host,
        "kind": "fake",
        "status": status,
        "message": message,
        "latencyMs": 1,
    }


def ssh_portfolio_check():
    return {
        "id": "portfolio-api-remote-fixture",
        "label": "Portfolio API remote fixture",
        "host": "fixture",
        "kind": "ssh-http",
        "sshTarget": "fixture@example.invalid",
        "healthUrl": "http://127.0.0.1:8099/api/health",
        "jsonField": "ok",
        "jsonEquals": True,
        "attempts": 2,
        "requestTimeout": 1,
        "attemptTimeout": 2,
        "retryDelay": 0.1,
        "probeUnavailableStatus": "warn",
        "failureStatus": "fail",
    }


class OpsCenterTests(unittest.TestCase):
    def test_frontend_systemd_allowlist_is_covered_by_sudo_helper(self):
        expected = {"cabrera-portfolio.service", "cabrera-programs.service"}

        self.assertTrue(expected.issubset(appmod.ALLOWED_UNITS))
        self.assertTrue(appmod.ALLOWED_UNITS.issubset(sudo_ops.ALLOWED_UNITS))

    def test_sudo_helper_allows_cabrera_units_for_logs_and_restart(self):
        for unit in ("cabrera-portfolio.service", "cabrera-programs.service"):
            with self.subTest(unit=unit):
                with patch.object(sudo_ops, "run", return_value="output") as mocked:
                    sudo_ops.main(["sudo_ops.py", "journal", unit, "180"])
                self.assertEqual(
                    mocked.call_args.args[0],
                    ["/usr/bin/journalctl", "-u", unit, "-n", "180", "--no-pager", "-o", "short-iso"],
                )

                with patch.object(sudo_ops, "run", return_value="") as mocked:
                    sudo_ops.main(["sudo_ops.py", "systemd_restart", unit])
                self.assertEqual(mocked.call_args.args[0], ["/usr/bin/systemctl", "restart", unit])

    def test_dashboard_logs_and_restart_route_use_the_narrow_helper(self):
        for unit in ("cabrera-portfolio.service", "cabrera-programs.service"):
            with self.subTest(unit=unit):
                fake_request = types.SimpleNamespace(
                    args={"sourceType": "systemd", "id": unit, "lines": "180"},
                )
                with patch.object(appmod, "request", fake_request), patch.object(
                    appmod, "run_privileged_text", return_value="journal output"
                ) as logs:
                    response = appmod.api_logs()
                self.assertEqual(response["text"], "journal output")
                logs.assert_called_once_with(["journal", unit, "180"], timeout=20)

                jobs = appmod.ActionJobs(appmod.DashboardCache())
                payload = {"type": "systemd", "action": "restart", "unit": unit}
                jobs.validate(payload)
                proc = types.SimpleNamespace(returncode=0, stdout="restarted\n")
                with patch.object(appmod, "run_privileged", return_value=proc) as restart:
                    self.assertEqual(jobs.execute(payload), "restarted\n")
                restart.assert_called_once_with(["systemd_restart", unit], timeout=45)

    def test_login_throttle_ignores_forwarded_for_without_trusted_proxy(self):
        fake_request = types.SimpleNamespace(
            remote_addr="192.0.2.20",
            headers={"X-Forwarded-For": "198.51.100.8"},
        )
        with patch.object(appmod, "request", fake_request), patch.object(appmod, "TRUSTED_PROXY_NETWORKS", ()):
            self.assertEqual(appmod.login_client_key(), "192.0.2.20")

    def test_login_throttle_accepts_only_validated_forwarded_chain(self):
        fake_request = types.SimpleNamespace(
            remote_addr="127.0.0.1",
            headers={"X-Forwarded-For": "198.51.100.8"},
        )
        with patch.object(
            appmod,
            "request",
            fake_request,
        ), patch.object(appmod, "TRUSTED_PROXY_NETWORKS", (appmod.ipaddress.ip_network("127.0.0.1/32"),)), patch.object(
            appmod, "TRUSTED_PROXY_HOPS", 1
        ):
            self.assertEqual(appmod.login_client_key(), "198.51.100.8")
            fake_request.headers["X-Forwarded-For"] = "not-an-ip"
            self.assertEqual(appmod.login_client_key(), "127.0.0.1")

    def test_trusted_proxy_configuration_fails_closed_on_malformed_entry(self):
        self.assertEqual(appmod.parse_trusted_proxy_networks("127.0.0.1,not-an-ip"), ())
        self.assertEqual(
            appmod.parse_trusted_proxy_networks("127.0.0.1"),
            (appmod.ipaddress.ip_network("127.0.0.1/32"),),
        )

    def test_login_rejects_plaintext_request_before_pam(self):
        fake_request = types.SimpleNamespace(
            remote_addr="192.0.2.20",
            headers={},
            is_secure=False,
            get_json=lambda **kwargs: {"password": "secret"},
        )
        with patch.object(appmod, "request", fake_request), patch.object(appmod, "verify_login_password") as verify:
            response, status = appmod.api_login()

        self.assertEqual(status, 400)
        self.assertIn("HTTPS", response["error"])
        verify.assert_not_called()

    def test_login_allows_direct_https_request(self):
        fake_request = types.SimpleNamespace(
            remote_addr="192.0.2.20",
            headers={},
            is_secure=True,
            get_json=lambda **kwargs: {"password": "secret"},
        )
        fake_session = {}
        with patch.object(appmod, "request", fake_request), patch.object(appmod, "session", fake_session), patch.object(
            appmod, "verify_login_password", return_value=True
        ):
            response = appmod.api_login()

        self.assertTrue(response["authenticated"])
        self.assertEqual(fake_session["user"], appmod.AUTH_USER)

    def test_login_allows_https_forwarded_by_configured_proxy(self):
        fake_request = types.SimpleNamespace(
            remote_addr="127.0.0.1",
            headers={"X-Forwarded-For": "198.51.100.8", "X-Forwarded-Proto": "https"},
            is_secure=False,
            get_json=lambda **kwargs: {"password": "secret"},
        )
        fake_session = {}
        with patch.object(appmod, "request", fake_request), patch.object(appmod, "session", fake_session), patch.object(
            appmod, "TRUSTED_PROXY_NETWORKS", (appmod.ipaddress.ip_network("127.0.0.1/32"),)
        ), patch.object(appmod, "TRUSTED_PROXY_HOPS", 1), patch.object(
            appmod, "verify_login_password", return_value=True
        ):
            response = appmod.api_login()

        self.assertTrue(response["authenticated"])
        self.assertEqual(fake_session["user"], appmod.AUTH_USER)

    def test_non_loopback_bind_without_tls_fails_closed(self):
        args = types.SimpleNamespace(host="0.0.0.0", port=80, debug=False)
        with patch.object(appmod, "parse_args", return_value=args), patch.object(appmod, "TLS_CERT_FILE", ""), patch.object(
            appmod, "TLS_KEY_FILE", ""
        ), patch.object(appmod.cache, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "without TLS"):
                appmod.main()
        start.assert_not_called()

    def test_service_template_defaults_to_loopback_boundary(self):
        unit = (Path(__file__).resolve().parent.parent / "scripts" / "pi4-noc.service").read_text(encoding="utf-8")
        self.assertIn("Environment=PI4_NOC_HOST=127.0.0.1", unit)
        self.assertIn("Environment=PI4_NOC_PORT=8080", unit)
        self.assertNotIn("--host 0.0.0.0 --port 80", unit)

    def test_empty_dashboard_snapshot_is_loading_until_initial_refresh(self):
        cache = appmod.DashboardCache()
        self.assertEqual(cache.snapshot()["META"]["state"], "loading")

        with patch.object(cache, "_run_refreshes", return_value=True):
            cache._initial_refresh()

        self.assertEqual(cache.snapshot()["META"]["state"], "ready")

    def test_macmini_monitor_contract_uses_current_topology(self):
        checks = {check["id"]: check for check in appmod.OPS_CHECK_CONFIG["five-minute"]}
        self.assertEqual(
            set(checks),
            {
                "gateway",
                "dns-resolver",
                "wan-http",
                "wan-latency",
                "dns-latency",
                "macminiops",
                "grid-web",
                "grid-mcp",
                "wedding-public",
                "work",
                "portfolio",
            },
        )
        self.assertEqual(checks["macminiops"]["url"], appmod.MACMINIOPS_HEALTH_URL)
        self.assertEqual(checks["grid-web"]["url"], appmod.GRID_WEB_HEALTH_URL)
        self.assertEqual(checks["grid-mcp"]["url"], appmod.GRID_MCP_HEALTH_URL)
        self.assertEqual(checks["grid-mcp"]["okStatuses"], [401])
        self.assertEqual(checks["work"]["url"], appmod.WORK_HEALTH_URL)
        self.assertEqual(checks["portfolio"]["url"], appmod.PORTFOLIO_HEALTH_URL)
        self.assertEqual(checks["portfolio"]["attempts"], 2)

        serialized = repr(appmod.OPS_CHECK_CONFIG)
        for stale in ("192.168.0.94", "192.168.0.101", "coinbot", "eagleeye"):
            self.assertNotIn(stale, serialized)

    def test_ops_config_covers_required_cadence_work(self):
        five_minute_ids = {check["id"] for check in appmod.OPS_CHECK_CONFIG["five-minute"]}
        hourly_ids = {check["id"] for check in appmod.OPS_CHECK_CONFIG["hourly"]}
        nightly_ids = {check["id"] for check in appmod.OPS_CHECK_CONFIG["nightly"]}

        self.assertEqual(
            five_minute_ids,
            {
                "gateway",
                "dns-resolver",
                "wan-http",
                "wan-latency",
                "dns-latency",
                "macminiops",
                "grid-web",
                "grid-mcp",
                "wedding-public",
                "work",
                "portfolio",
            },
        )
        self.assertEqual(hourly_ids, {"wan-speed", "k3s-release"})
        self.assertEqual({check["id"] for check in appmod.OPS_CHECK_CONFIG["morning"]}, {"brief"})
        self.assertEqual(nightly_ids, set())
        self.assertEqual(appmod.OPS_CADENCE_CONFIG[2]["scheduleTime"], "07:00")
        self.assertEqual(appmod.OPS_CADENCE_CONFIG[3]["scheduleTime"], "23:55")

    def test_retired_non_wedding_targets_are_not_current_monitors(self):
        all_checks = [check for cadence in appmod.OPS_CHECK_CONFIG.values() for check in cadence]
        serialized = repr(all_checks)
        for stale in (
            "pi4 k3s",
            "pi4-noc",
            "pi5",
            "192.168.0.94",
            "192.168.0.101",
            "coinbot",
            "eagleeye",
            "cabrera-programs",
            "portfolio-api",
            "portfolio-tailnet",
            "work-website",
        ):
            self.assertNotIn(stale, serialized.lower())

    def test_daily_schedule_helpers_keep_morning_wall_clock(self):
        base = datetime_from_parts(2026, 7, 5, 2, 30)
        next_run = appmod.next_operation_run_ts({"scheduleTime": "07:00"}, base)
        previous_run = appmod.scheduled_daily_ts("07:00", base, previous=True)

        self.assertEqual(ts_parts(next_run), (2026, 7, 5, 7, 0))
        self.assertEqual(ts_parts(previous_run), (2026, 7, 4, 7, 0))
        self.assertFalse(appmod.operation_cadence_due({"scheduleTime": "07:00"}, base, base))
        self.assertTrue(appmod.operation_cadence_due({"scheduleTime": "07:00"}, None, base))

        after_morning = datetime_from_parts(2026, 7, 5, 7, 5)
        self.assertTrue(appmod.operation_cadence_due({"scheduleTime": "07:00"}, base, after_morning))

    def test_summarize_operations_rolls_up_check_counts(self):
        summary = appmod.summarize_operations(
            [
                {"checks": [{"status": "ok"}, {"status": "warn"}]},
                {"checks": [{"status": "fail"}]},
            ]
        )

        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["warn"], 1)
        self.assertEqual(summary["fail"], 1)
        self.assertEqual(summary["total"], 3)

    def test_update_operations_runs_due_cadence_and_keeps_snapshot_shape(self):
        cache = appmod.DashboardCache()
        calls = []

        def fake_check(check):
            calls.append(check["id"])
            return {
                "id": check["id"],
                "label": check["label"],
                "host": "Pi4",
                "kind": "fake",
                "status": "ok",
                "message": "healthy",
                "latencyMs": 1,
            }

        cadence_config = [{"id": "fast", "label": "Fast", "intervalSeconds": 300, "glyph": "activity"}]
        check_config = {"fast": [{"id": "probe", "label": "Probe", "kind": "fake"}]}
        with patch.object(appmod, "OPS_CADENCE_CONFIG", cadence_config), patch.object(appmod, "OPS_CHECK_CONFIG", check_config):
            cache.run_operation_check = fake_check
            cache.update_operations(force=True)
            first = cache.snapshot()["OPS_CENTER"]
            cache.update_operations(force=False)
            second = cache.snapshot()["OPS_CENTER"]

        self.assertEqual(calls, ["probe"])
        self.assertEqual(first["schemaVersion"], 1)
        self.assertEqual(first["summary"]["status"], "ok")
        self.assertEqual(first["cadences"][0]["checks"][0]["message"], "healthy")
        self.assertEqual(second["cadences"][0]["lastRunAt"], first["cadences"][0]["lastRunAt"])

    def test_morning_notification_sends_once_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            notifier = RecordingNotificationManager(Path(tmp) / "notify-state.json")
            ops = ops_snapshot(ops_check("brief", "ok", "brief generated"))
            due = [{"id": "morning", "label": "Every morning"}]
            now = datetime_from_parts(2026, 7, 6, 7, 1)

            first = notifier.dispatch_operation_notifications(ops, due, now=now)
            second = notifier.dispatch_operation_notifications(ops, due, now=now + 60)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0]["severity"], "morning")
        self.assertIn("all green", notifier.sent[0]["subject"])

    def test_failed_morning_notification_retries_with_bounded_backoff(self):
        class FlakyNotificationManager(RecordingNotificationManager):
            def __init__(self, state_path):
                super().__init__(state_path)
                self.outcomes = [False, True]
                self.env["PI4_NOC_NOTIFY_MORNING_RETRY_SECONDS"] = "900"

            def send(self, subject: str, body: str, *, severity: str = "info", fields: dict | None = None) -> bool:
                self.sent.append({"subject": subject, "body": body, "severity": severity, "fields": fields})
                return self.outcomes.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "notify-state.json"
            notifier = FlakyNotificationManager(state_path)
            ops = ops_snapshot(ops_check("brief", "ok", "brief generated"))
            morning = [{"id": "morning", "label": "Every morning"}]
            five_minute = [{"id": "five-minute", "label": "Every 5 minutes"}]
            start = datetime_from_parts(2026, 7, 6, 7, 1)

            first = notifier.dispatch_operation_notifications(ops, morning, now=start)
            too_soon = notifier.dispatch_operation_notifications(ops, morning, now=start + 899)
            retried = notifier.dispatch_operation_notifications(ops, five_minute, now=start + 900)
            after_success = notifier.dispatch_operation_notifications(ops, five_minute, now=start + 1800)
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(first, [])
        self.assertEqual(too_soon, [])
        self.assertEqual(len(retried), 1)
        self.assertEqual(after_success, [])
        self.assertEqual(len(notifier.sent), 2)
        self.assertEqual(state["morning"]["lastDate"], "2026-07-06")
        self.assertNotIn("pendingDate", state["morning"])
        self.assertIn("lastFailureAt", state["delivery"])
        self.assertIn("lastSuccessAt", state["delivery"])

    def test_critical_notification_keeps_cooldown_through_brief_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "notify-state.json"
            notifier = RecordingNotificationManager(state_path)
            due = [{"id": "five-minute", "label": "Every 5 minutes"}]
            start = datetime_from_parts(2026, 7, 6, 8, 0)
            failing = ops_snapshot(ops_check("grid-web", "fail", "health check failed", "GRID web/API"))
            recovered = ops_snapshot(ops_check("grid-web", "ok", "healthy", "GRID web/API"))

            notifier.dispatch_operation_notifications(failing, due, now=start)
            notifier.dispatch_operation_notifications(recovered, due, now=start + 300)
            recovery_state = json.loads(state_path.read_text(encoding="utf-8"))
            notifier.dispatch_operation_notifications(failing, due, now=start + 600)
            notifier.dispatch_operation_notifications(failing, due, now=start + 1801)

        self.assertEqual([item["severity"] for item in notifier.sent], ["critical", "critical"])
        self.assertIn("GRID web/API", notifier.sent[0]["subject"])
        self.assertEqual(recovery_state["critical"]["active"], {})
        self.assertIn("five-minute:grid-web", recovery_state["critical"]["recent"])

    def test_critical_notification_resends_after_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            notifier = RecordingNotificationManager(Path(tmp) / "notify-state.json")
            notifier.env["PI4_NOC_NOTIFY_CRITICAL_COOLDOWN_SECONDS"] = "60"
            due = [{"id": "five-minute", "label": "Every 5 minutes"}]
            start = datetime_from_parts(2026, 7, 6, 8, 0)
            failing = ops_snapshot(ops_check("portfolio-api", "fail", "HTTP 500", "Portfolio API"))

            notifier.dispatch_operation_notifications(failing, due, now=start)
            notifier.dispatch_operation_notifications(failing, due, now=start + 30)
            notifier.dispatch_operation_notifications(failing, due, now=start + 61)

        self.assertEqual(len(notifier.sent), 2)

    def test_failed_critical_notification_uses_bounded_retry_cooldown(self):
        class FailingNotificationManager(RecordingNotificationManager):
            def send(self, subject: str, body: str, *, severity: str = "info", fields: dict | None = None) -> bool:
                self.sent.append({"subject": subject, "body": body, "severity": severity, "fields": fields})
                return False

        with tempfile.TemporaryDirectory() as tmp:
            notifier = FailingNotificationManager(Path(tmp) / "notify-state.json")
            notifier.env["PI4_NOC_NOTIFY_CRITICAL_COOLDOWN_SECONDS"] = "600"
            due = [{"id": "five-minute", "label": "Every 5 minutes"}]
            start = datetime_from_parts(2026, 7, 6, 8, 0)
            failing = ops_snapshot(ops_check("grid-web", "fail", "health check failed", "GRID web/API"))

            notifier.dispatch_operation_notifications(failing, due, now=start)
            notifier.dispatch_operation_notifications(failing, due, now=start + 300)
            notifier.dispatch_operation_notifications(failing, due, now=start + 601)

        self.assertEqual(len(notifier.sent), 2)

    def test_warning_checks_do_not_send_critical_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            notifier = RecordingNotificationManager(Path(tmp) / "notify-state.json")
            ops = ops_snapshot(ops_check("wan-speed", "warn", "slow sample", "WAN speed sample"))

            notifier.dispatch_operation_notifications(ops, [{"id": "five-minute"}], now=datetime_from_parts(2026, 7, 6, 8, 0))

        self.assertEqual(notifier.sent, [])

    def test_force_refresh_does_not_send_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            notifier = RecordingNotificationManager(Path(tmp) / "notify-state.json")
            failing = ops_snapshot(ops_check("grid-mcp", "fail", "down", "GRID MCP"))

            result = notifier.dispatch_operation_notifications(
                failing,
                [{"id": "morning", "label": "Every morning"}, {"id": "five-minute", "label": "Every 5 minutes"}],
                force=True,
                now=datetime_from_parts(2026, 7, 6, 7, 1),
            )

        self.assertEqual(result, [])
        self.assertEqual(notifier.sent, [])

    def test_notification_body_redacts_secret_shaped_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            notifier = RecordingNotificationManager(Path(tmp) / "notify-state.json")
            failing = ops_snapshot(ops_check("grid-mcp", "fail", "token=abc123 leaked", "GRID MCP"))

            notifier.dispatch_operation_notifications(failing, [{"id": "five-minute"}], now=datetime_from_parts(2026, 7, 6, 8, 0))

        self.assertEqual(len(notifier.sent), 1)
        self.assertNotIn("abc123", notifier.sent[0]["body"])
        self.assertIn("<redacted>", notifier.sent[0]["body"])

    def test_notification_dispatch_failure_does_not_break_operations(self):
        cache = appmod.DashboardCache()

        class FailingNotifier:
            def dispatch_operation_notifications(self, *_args, **_kwargs):
                raise RuntimeError("password=supersecret")

        cache.ops_notifier = FailingNotifier()
        cache.dispatch_operation_notifications({}, [], now=datetime_from_parts(2026, 7, 6, 8, 0))

    def test_form_webhook_sends_provider_compatible_payload(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(req.header_items())
            captured["data"] = req.data.decode()
            return FakeResponse('{"success":"true"}')

        notifier = appmod.NotificationManager(
            {
                "PI4_NOC_NOTIFY_WEBHOOK_URL": "https://formsubmit.co/ajax/zoneofsavi@gmail.com",
                "PI4_NOC_NOTIFY_WEBHOOK_FORMAT": "form",
                "PI4_NOC_NOTIFY_WEBHOOK_REFERER": "http://192.168.0.101/",
            }
        )

        with patch.object(notifymod.urlrequest, "urlopen", side_effect=fake_urlopen):
            notifier.send_webhook("Ops subject", "Ops body", severity="morning")

        self.assertEqual(captured["url"], "https://formsubmit.co/ajax/zoneofsavi@gmail.com")
        self.assertEqual(captured["headers"]["Content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(captured["headers"]["Referer"], "http://192.168.0.101/")
        self.assertEqual(captured["timeout"], 30.0)
        self.assertIn("_subject=Ops+subject", captured["data"])
        self.assertIn("message=Ops+body", captured["data"])
        self.assertIn("_captcha=false", captured["data"])

    def test_form_webhook_renders_structured_fields_as_email_rows(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["data"] = req.data.decode()
            return FakeResponse('{"success":"true"}')

        notifier = appmod.NotificationManager(
            {
                "PI4_NOC_NOTIFY_WEBHOOK_URL": "https://formsubmit.co/ajax/zoneofsavi@gmail.com",
                "PI4_NOC_NOTIFY_WEBHOOK_FORMAT": "form",
            }
        )
        fields = {
            notifymod.field_label("📊 Scorecard"): "🟢 All 3 checks passing",
            notifymod.field_label("🕐 Snapshot"): "Friday, July 10 · 7:00 AM",
        }

        with patch.object(notifymod.urlrequest, "urlopen", side_effect=fake_urlopen):
            notifier.send_webhook("🟢 Morning report", "fallback body", severity="morning", fields=fields)

        from urllib.parse import parse_qs

        parsed = parse_qs(captured["data"], keep_blank_values=True)
        nbsp = notifymod.NBSP
        self.assertIn(f"📊{nbsp}Scorecard", parsed)
        self.assertIn(f"🕐{nbsp}Snapshot", parsed)
        self.assertEqual(parsed[f"📊{nbsp}Scorecard"], ["🟢 All 3 checks passing"])
        self.assertEqual(parsed["_subject"], ["🟢 Morning report"])
        self.assertEqual(parsed["_template"], ["table"])
        # Plumbing fields stay out of the visible email when structured rows exist.
        for hidden in ("message", "name", "email", "source", "severity", "sentAt", "subject"):
            self.assertNotIn(hidden, parsed)

    def test_morning_email_fields_read_like_a_status_page(self):
        ops = ops_snapshot(
            ops_check("grid-web", "fail", "health check failed", "GRID web/API"),
            ops_check("wan-speed", "warn", "slow sample", "WAN speed sample"),
            ops_check("gateway", "ok", "reachable", "Gateway"),
        )
        now = datetime_from_parts(2026, 7, 10, 7, 0)

        fields = notifymod.morning_email_fields(ops, [{"id": "morning", "label": "Every morning"}], now, link="http://192.168.0.101/")
        labels = list(fields)
        nbsp = notifymod.NBSP

        self.assertEqual(labels[0], f"🛰️{nbsp}Report")
        self.assertIn(f"📊{nbsp}Scorecard", fields)
        self.assertIn("🔴 1 failing", fields[f"📊{nbsp}Scorecard"])
        self.assertIn("🟡 1 warning", fields[f"📊{nbsp}Scorecard"])
        self.assertIn("🔴 Pi4 · GRID web/API — health check failed", fields[f"🔴{nbsp}Failing{nbsp}now"])
        self.assertIn("🟡 Pi4 · WAN speed sample — slow sample", fields[f"🟡{nbsp}Warnings"])
        self.assertEqual(fields[f"🔗{nbsp}Dashboard"], "http://192.168.0.101/")
        self.assertNotIn(f"✅{nbsp}All{nbsp}clear", fields)
        # No plain spaces or dots allowed in labels: PHP-style form backends
        # would rewrite them to underscores in the rendered email.
        for label in labels:
            self.assertNotIn(" ", label)
            self.assertNotIn(".", label)

    def test_morning_email_fields_all_clear_is_friendly(self):
        ops = ops_snapshot(ops_check("gateway", "ok", "reachable", "Gateway"))
        now = datetime_from_parts(2026, 7, 10, 7, 0)

        fields = notifymod.morning_email_fields(ops, [{"id": "morning", "label": "Every morning"}], now)
        nbsp = notifymod.NBSP

        self.assertIn(f"✅{nbsp}All{nbsp}clear", fields)
        self.assertIn("Nothing needs your attention", fields[f"✅{nbsp}All{nbsp}clear"])
        self.assertNotIn(f"🔴{nbsp}Failing{nbsp}now", fields)
        self.assertNotIn(f"🔗{nbsp}Dashboard", fields)

    def test_critical_dispatch_sends_structured_fields_through_form_webhook(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["data"] = req.data.decode()
            return FakeResponse('{"success":"true"}')

        with tempfile.TemporaryDirectory() as tmp:
            notifier = appmod.NotificationManager(
                {
                    "PI4_NOC_NOTIFY_WEBHOOK_URL": "https://formsubmit.co/ajax/zoneofsavi@gmail.com",
                    "PI4_NOC_NOTIFY_WEBHOOK_FORMAT": "form",
                    "PI4_NOC_NOTIFY_LINK_URL": "http://192.168.0.101/",
                    "PI4_NOC_NOTIFY_STATE": str(Path(tmp) / "notify-state.json"),
                }
            )
            failing = ops_snapshot(ops_check("grid-mcp", "fail", "token=abc123 leaked", "GRID MCP"))
            with patch.object(notifymod.urlrequest, "urlopen", side_effect=fake_urlopen):
                sent = notifier.dispatch_operation_notifications(
                    failing, [{"id": "five-minute"}], now=datetime_from_parts(2026, 7, 10, 8, 0)
                )

        from urllib.parse import parse_qs

        parsed = parse_qs(captured["data"], keep_blank_values=True)
        nbsp = notifymod.NBSP

        self.assertEqual(len(sent), 1)
        self.assertIn("🚨", parsed["_subject"][0])
        self.assertIn("GRID MCP", parsed["_subject"][0])
        self.assertIn(f"🚨{nbsp}What{nbsp}broke", parsed)
        self.assertEqual(parsed[f"🔗{nbsp}Dashboard"], ["http://192.168.0.101/"])
        self.assertNotIn("abc123", captured["data"])
        self.assertIn("%3Credacted%3E", captured["data"])

    def test_ntfy_is_primary_and_uses_private_topic_json(self):
        captured = []

        def fake_urlopen(req, timeout=0):
            captured.append({"url": req.full_url, "timeout": timeout, "payload": json.loads(req.data.decode())})
            return FakeResponse('{"id":"ntfy-test","time":1,"event":"message","topic":"private_topic_1234567890"}')

        notifier = notifymod.NotificationManager(
            {
                "PI4_NOC_NOTIFY_NTFY_URL": "https://ntfy.sh/private_topic_1234567890",
                "PI4_NOC_NOTIFY_NTFY_CLICK_URL": "http://192.168.0.101/",
                "PI4_NOC_NOTIFY_WEBHOOK_URL": "https://fallback.example.test/hook",
            }
        )
        with patch.object(notifymod.urlrequest, "urlopen", side_effect=fake_urlopen):
            delivered = notifier.send("Critical test", "Host is down", severity="critical")

        self.assertTrue(delivered)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["url"], "https://ntfy.sh/")
        self.assertEqual(captured[0]["payload"]["topic"], "private_topic_1234567890")
        self.assertEqual(captured[0]["payload"]["priority"], 5)
        self.assertEqual(captured[0]["payload"]["click"], "http://192.168.0.101/")

    def test_ntfy_failure_uses_existing_webhook_as_fallback(self):
        responses = [TimeoutError("ntfy unavailable"), FakeResponse('{"success":true}')]
        calls = []

        def fake_urlopen(req, timeout=0):
            calls.append(req.full_url)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        notifier = notifymod.NotificationManager(
            {
                "PI4_NOC_NOTIFY_NTFY_URL": "https://ntfy.sh/private_topic_1234567890",
                "PI4_NOC_NOTIFY_WEBHOOK_URL": "https://fallback.example.test/ajax/recipient",
                "PI4_NOC_NOTIFY_WEBHOOK_FORMAT": "form",
            }
        )
        with patch.object(notifymod.urlrequest, "urlopen", side_effect=fake_urlopen):
            delivered = notifier.send("Fallback test", "ntfy failed", severity="critical")

        self.assertTrue(delivered)
        self.assertEqual(calls, ["https://ntfy.sh/", "https://fallback.example.test/ajax/recipient"])

    def test_macos_alert_subscriber_dedupes_and_omits_message_from_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subscriber = alert_subscriber.Subscriber(root / "state.json", root / "receipts.jsonl")
            event = {
                "event": "message",
                "id": "message-1",
                "time": 1234,
                "title": "RP4 alert",
                "message": "protected operational detail",
                "priority": 5,
            }
            with patch.object(alert_subscriber, "display_notification", return_value=True) as display:
                first = subscriber.handle(event)
                second = subscriber.handle(event)

            receipt_text = (root / "receipts.jsonl").read_text(encoding="utf-8")
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))

        self.assertTrue(first)
        self.assertFalse(second)
        display.assert_called_once_with("RP4 alert", "protected operational detail")
        self.assertNotIn("protected operational detail", receipt_text)
        self.assertEqual(state["recentIds"], ["message-1"])

    def test_macos_alert_subscriber_requires_private_https_topic(self):
        self.assertEqual(
            alert_subscriber.validate_topic_url("https://ntfy.sh/private_topic_1234567890/"),
            "https://ntfy.sh/private_topic_1234567890",
        )
        with self.assertRaises(ValueError):
            alert_subscriber.validate_topic_url("http://ntfy.sh/short")

    def test_webhook_failure_response_is_not_counted_as_delivered(self):
        notifier = appmod.NotificationManager({"PI4_NOC_NOTIFY_WEBHOOK_URL": "https://notify.test"})

        with patch.object(notifymod.urlrequest, "urlopen", return_value=FakeResponse('{"success":"false","message":"needs activation"}')):
            delivered = notifier.send("Ops subject", "Ops body", severity="morning")

        self.assertFalse(delivered)

    def test_http_operation_check_warns_on_degraded_json(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "fixture-http",
            "label": "Fixture HTTP",
            "host": "fixture",
            "kind": "http",
            "url": "https://monitor.test/health",
            "jsonField": "status",
            "jsonEquals": "ok",
            "warnJsonField": "degraded",
        }

        with patch.object(appmod, "open_ops_url", return_value=FakeResponse('{"status":"ok","degraded":true}')):
            result = cache.http_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["httpStatus"], 200)
        self.assertIn("degraded", result["message"])

    def test_http_operation_check_accepts_configured_http_error_status(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "grid-mcp",
            "label": "GRID MCP auth boundary",
            "host": "Mac mini",
            "kind": "http",
            "url": appmod.GRID_MCP_HEALTH_URL,
            "okStatuses": [401],
        }

        with patch.object(
            appmod,
            "open_ops_url",
            side_effect=appmod.urlerror.HTTPError(check["url"], 401, "Unauthorized", {}, None),
        ):
            result = cache.http_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["httpStatus"], 401)

    def test_http_operation_check_retries_transient_failure(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "fixture-http",
            "label": "Fixture HTTP",
            "host": "fixture",
            "kind": "http",
            "url": "https://monitor.test/api/health",
            "jsonField": "ok",
            "jsonEquals": True,
            "attempts": 2,
            "retryDelay": 0,
            "failureStatus": "warn",
        }

        with patch.object(
            appmod,
            "open_ops_url",
            side_effect=[TimeoutError("timed out"), FakeResponse('{"ok":true}')],
        ) as opened:
            result = cache.http_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(opened.call_count, 2)

    def test_http_operation_check_exhausts_retries_as_warning(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "fixture-http",
            "label": "Fixture HTTP",
            "host": "fixture",
            "kind": "http",
            "url": "https://monitor.test/api/health",
            "attempts": 2,
            "retryDelay": 0,
            "failureStatus": "warn",
        }

        with patch.object(appmod, "open_ops_url", side_effect=TimeoutError("timed out")) as opened:
            result = cache.http_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["attempts"], 2)
        self.assertIn("timed out", result["message"])
        self.assertEqual(opened.call_count, 2)

    def test_operations_requests_share_one_verified_tls_opener(self):
        self.assertEqual(appmod.OPS_SSL_CONTEXT.verify_mode, appmod.ssl.CERT_REQUIRED)
        self.assertTrue(appmod.OPS_SSL_CONTEXT.check_hostname)
        request = appmod.urlrequest.Request("https://example.test/health")
        results = []

        def open_request():
            with appmod.open_ops_url(request, timeout=2) as response:
                results.append(response.read().decode())

        with patch.object(
            appmod.OPS_URL_OPENER,
            "open",
            side_effect=lambda *_args, **_kwargs: FakeResponse("ok"),
        ) as opened:
            workers = [threading.Thread(target=open_request) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(1)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(results, ["ok"] * 8)
        self.assertEqual(opened.call_count, 8)

    def test_multi_http_operation_check_reports_average_latency(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "wan-latency",
            "label": "WAN endpoint latency",
            "host": "Internet",
            "kind": "multi-http",
            "urls": ["https://one.test", "https://two.test"],
            "maxAvgMs": 1000,
        }

        with patch.object(appmod, "open_ops_url", return_value=FakeResponse("ok")):
            result = cache.multi_http_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["failures"], 0)
        self.assertIn("2/2 endpoints", result["message"])

    def test_multi_dns_operation_check_reports_average_latency(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "dns-latency",
            "label": "DNS latency",
            "host": "Pi4",
            "kind": "multi-dns",
            "targets": ["one.test", "two.test"],
            "maxAvgMs": 1000,
        }

        with patch.object(appmod.socket, "getaddrinfo", return_value=[("family", "socktype")]):
            result = cache.multi_dns_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["failures"], 0)
        self.assertIn("2/2 names", result["message"])

    def test_brief_operation_check_does_not_replay_stale_warnings(self):
        cache = appmod.DashboardCache()
        cache.snapshot_data["OPS_CENTER"] = {
            "cadences": [{"checks": [{"status": "warn", "message": "old warning"}]}],
        }
        check = {"id": "brief", "label": "Brief", "kind": "operations-brief"}

        result = cache.brief_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertIn("brief generated", result["message"])
        self.assertIn("warning", result["message"])

    def test_timer_operation_check_reports_active_timer(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "logrotate-timer",
            "label": "Log rotation timer",
            "host": "Pi4",
            "kind": "systemd-timer",
            "unit": "logrotate.timer",
            "maxLastHours": 36,
        }
        now_text = datetime.fromtimestamp(time.time()).strftime("%a %Y-%m-%d %H:%M:%S MDT")
        proc = types.SimpleNamespace(returncode=0, stdout=f"ActiveState=active\nLastTriggerUSec={now_text}\nNextElapseUSecRealtime=\n", stderr="")

        with patch.object(appmod, "run_cmd", return_value=proc):
            result = cache.timer_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["unit"], "logrotate.timer")
        self.assertIn("active", result["message"])
        self.assertIn("lastAgeHours", result)

    def test_ssh_k3s_operation_check_reports_remote_workloads(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "pi5-k3s-apps",
            "label": "Pi5 k3s apps",
            "host": "Pi5 k3s",
            "kind": "ssh-k3s",
            "sshTarget": "pi5@192.168.0.94",
            "scope": "workloads",
            "workloads": ["coinbot", "coinbot-website", "eagleeye"],
        }
        body = {
            "items": [
                {"kind": "Deployment", "metadata": {"name": "coinbot"}, "spec": {"replicas": 1}, "status": {"readyReplicas": 1}},
                {"kind": "Deployment", "metadata": {"name": "coinbot-website"}, "spec": {"replicas": 1}, "status": {"readyReplicas": 1}},
                {"kind": "Deployment", "metadata": {"name": "eagleeye"}, "spec": {"replicas": 1}, "status": {"readyReplicas": 1}},
            ]
        }
        proc = types.SimpleNamespace(returncode=0, stdout=appmod.json.dumps(body), stderr="")

        with patch.object(appmod, "run_cmd", return_value=proc):
            result = cache.ssh_k3s_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["message"], "3/3 workloads ready")

    def test_ssh_http_operation_check_accepts_healthy_loopback_response(self):
        cache = appmod.DashboardCache()
        check = ssh_portfolio_check()
        healthy = types.SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

        with patch.object(cache, "ssh_run", return_value=healthy) as ssh_run:
            result = cache.ssh_http_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["attempts"], 1)
        self.assertIn("127.0.0.1:8099/api/health", ssh_run.call_args.args[1])

    def test_ssh_http_operation_check_retries_confirmed_api_failure(self):
        cache = appmod.DashboardCache()
        check = ssh_portfolio_check()
        refused = types.SimpleNamespace(returncode=7, stdout="curl: (7) connection refused", stderr="")
        healthy = types.SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

        with (
            patch.object(cache, "ssh_run", side_effect=[refused, healthy]) as ssh_run,
            patch.object(appmod.time, "sleep"),
        ):
            result = cache.ssh_http_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(ssh_run.call_count, 2)

    def test_ssh_http_operation_check_fails_after_two_confirmed_api_failures(self):
        cache = appmod.DashboardCache()
        check = ssh_portfolio_check()
        refused = types.SimpleNamespace(returncode=7, stdout="curl: (7) connection refused", stderr="")

        with (
            patch.object(cache, "ssh_run", return_value=refused) as ssh_run,
            patch.object(appmod.time, "sleep"),
        ):
            result = cache.ssh_http_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["attempts"], 2)
        self.assertIn("connection refused", result["message"])
        self.assertEqual(ssh_run.call_count, 2)

    def test_ssh_http_operation_check_warns_when_transport_is_unavailable(self):
        cache = appmod.DashboardCache()
        check = ssh_portfolio_check()
        unavailable = types.SimpleNamespace(returncode=255, stdout="ssh: connect timed out", stderr="")

        with (
            patch.object(cache, "ssh_run", return_value=unavailable) as ssh_run,
            patch.object(appmod.time, "sleep"),
        ):
            result = cache.ssh_http_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["attempts"], 2)
        self.assertIn("probe unavailable", result["message"])
        self.assertEqual(ssh_run.call_count, 2)

    def test_ssh_k3s_operation_check_flags_missing_configured_workload(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "pi5-k3s-apps",
            "label": "Pi5 k3s apps",
            "host": "Pi5 k3s",
            "kind": "ssh-k3s",
            "sshTarget": "pi5@192.168.0.94",
            "scope": "workloads",
            "workloads": ["coinbot", "eagleeye"],
        }
        body = {
            "items": [
                {"kind": "Deployment", "metadata": {"name": "coinbot"}, "spec": {"replicas": 1}, "status": {"readyReplicas": 1}},
            ]
        }
        proc = types.SimpleNamespace(returncode=0, stdout=appmod.json.dumps(body), stderr="")

        with patch.object(appmod, "run_cmd", return_value=proc):
            result = cache.ssh_k3s_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["missingWorkloads"], ["eagleeye"])
        self.assertEqual(result["message"], "1/2 workloads ready; missing eagleeye")

    def test_ssh_systemd_failed_operation_check_reports_clean_host(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "pi5-systemd-failures",
            "label": "Pi5 failed units",
            "host": "Pi5",
            "kind": "ssh-systemd-failed",
            "sshTarget": "pi5@192.168.0.94",
        }
        proc = types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(cache, "ssh_run", return_value=proc):
            result = cache.ssh_systemd_failed_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["failedUnits"], [])

    def test_ssh_systemd_failed_operation_check_flags_failed_units(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "pi5-systemd-failures",
            "label": "Pi5 failed units",
            "host": "Pi5",
            "kind": "ssh-systemd-failed",
            "sshTarget": "pi5@192.168.0.94",
        }
        proc = types.SimpleNamespace(
            returncode=0,
            stdout="cabrera-portfolio-manual-sync.service loaded failed failed CabreraPortfolio manual portal sync\n",
            stderr="",
        )

        with patch.object(cache, "ssh_run", return_value=proc):
            result = cache.ssh_systemd_failed_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["failedUnits"], ["cabrera-portfolio-manual-sync.service"])

    def test_ssh_systemd_unit_operation_check_reports_active_unit(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "pi5-tailscale",
            "label": "Pi5 Tailscale",
            "host": "Pi5",
            "kind": "ssh-systemd-unit",
            "sshTarget": "pi5@192.168.0.94",
            "unit": "tailscaled.service",
        }
        proc = types.SimpleNamespace(returncode=0, stdout="active\n", stderr="")

        with patch.object(cache, "ssh_run", return_value=proc):
            result = cache.ssh_systemd_unit_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["activeState"], "active")

    def test_ssh_pi_health_operation_check_reports_headroom(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "pi5-system-headroom",
            "label": "Pi5 system headroom",
            "host": "Pi5",
            "kind": "ssh-pi-health",
            "sshTarget": "pi5@192.168.0.94",
            "maxDiskPct": 85,
            "maxTempC": 70,
        }
        proc = types.SimpleNamespace(
            returncode=0,
            stdout="disk_pct=11\ndisk_avail=197G\nmem_avail_mb=5900\ntemp_c=45.0\nthrottle=0x0\n",
            stderr="",
        )

        with patch.object(cache, "ssh_run", return_value=proc):
            result = cache.ssh_pi_health_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["diskPct"], 11.0)
        self.assertEqual(result["throttleHex"], "0x0")

    def test_ssh_apt_upgrades_operation_check_warns_on_backlog(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "pi5-package-upgrades",
            "label": "Pi5 package upgrades",
            "host": "Pi5",
            "kind": "ssh-apt-upgrades",
            "sshTarget": "pi5@192.168.0.94",
            "warnCount": 25,
        }
        proc = types.SimpleNamespace(returncode=0, stdout="106\n", stderr="")

        with patch.object(cache, "ssh_run", return_value=proc):
            result = cache.ssh_apt_upgrades_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["upgradeCount"], 106)

    def test_k3s_resources_operation_check_reports_guardrails(self):
        cache = appmod.DashboardCache()
        body = {
            "items": [
                {
                    "metadata": {"name": "grid"},
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "grid",
                                        "resources": {
                                            "requests": {"cpu": "100m", "memory": "256Mi"},
                                            "limits": {"cpu": "500m", "memory": "512Mi"},
                                        },
                                    }
                                ]
                            }
                        }
                    },
                }
            ]
        }
        proc = types.SimpleNamespace(returncode=0, stdout=appmod.json.dumps(body), stderr="")
        check = {"id": "pi4-k3s-resources", "label": "Pi4 k3s resource guardrails", "host": "Pi4", "kind": "k3s-resources", "namespace": "homelab", "workloads": ["grid"]}

        with patch.object(appmod, "run_cmd", return_value=proc):
            result = cache.k3s_resources_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["inspected"], 1)
        self.assertEqual(result["missingCount"], 0)

    def test_k3s_resources_operation_check_flags_missing_limits(self):
        cache = appmod.DashboardCache()
        body = {
            "items": [
                {
                    "metadata": {"name": "unbounded-worker"},
                    "spec": {"template": {"spec": {"containers": [{"name": "registry", "resources": {"requests": {"memory": "128Mi"}}}]}}},
                }
            ]
        }
        proc = types.SimpleNamespace(returncode=0, stdout=appmod.json.dumps(body), stderr="")
        check = {"id": "pi4-k3s-resources", "label": "Pi4 k3s resource guardrails", "host": "Pi4", "kind": "k3s-resources", "namespace": "homelab", "workloads": ["unbounded-worker"]}

        with patch.object(appmod, "run_cmd", return_value=proc):
            result = cache.k3s_resources_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "warn")
        self.assertGreater(result["missingCount"], 0)

    def test_k3s_workload_and_resource_checks_flag_missing_targets(self):
        cache = appmod.DashboardCache()
        cache.snapshot_data["K3S"] = {"workloads": [{"name": "grid", "ready": 1, "desired": 1}]}
        workload_check = {
            "id": "pi4-k3s-apps",
            "label": "Pi4 k3s apps",
            "kind": "k3s-local",
            "scope": "workloads",
            "workloads": ["grid", "missing-app"],
        }
        workload_result = cache.k3s_operation_check(workload_check, time.monotonic())

        body = {
            "items": [
                {
                    "metadata": {"name": "grid"},
                    "spec": {"template": {"spec": {"containers": [{"name": "grid", "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "768Mi"}}}]}}},
                }
            ]
        }
        resource_check = {
            "id": "pi4-k3s-resources",
            "label": "Pi4 k3s resources",
            "kind": "k3s-resources",
            "namespace": "homelab",
            "workloads": ["grid", "missing-app"],
        }
        proc = types.SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
        with patch.object(appmod, "run_cmd", return_value=proc):
            resource_result = cache.k3s_resources_operation_check(resource_check, time.monotonic())

        self.assertEqual(workload_result["status"], "warn")
        self.assertEqual(workload_result["missingWorkloads"], ["missing-app"])
        self.assertEqual(resource_result["status"], "warn")
        self.assertEqual(resource_result["missingWorkloads"], ["missing-app"])

    def test_raspi_throttle_operation_check_reports_sticky_voltage(self):
        cache = appmod.DashboardCache()
        check = {"id": "pi4-power-throttle", "label": "Pi4 power/throttle", "host": "Pi4", "kind": "raspi-throttle"}
        proc = types.SimpleNamespace(returncode=0, stdout="throttled=0x50000\n", stderr="")

        with patch.object(appmod, "run_cmd", return_value=proc):
            result = cache.raspi_throttle_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "warn")
        self.assertIn("under-voltage occurred", result["message"])

    def test_port_drift_operation_check_flags_unexpected_listener(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "pi4-port-drift",
            "label": "Pi4 open-port drift",
            "host": "Pi4",
            "kind": "port-drift",
            "allow": ["tcp/22", "udp/53"],
            "ignoreUdpAbove": 20000,
        }
        stdout = "\n".join(
            [
                "tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:*",
                "tcp LISTEN 0 128 127.0.0.1:9999 0.0.0.0:*",
                "tcp LISTEN 0 128 0.0.0.0:9999 0.0.0.0:*",
                "udp UNCONN 0 0 0.0.0.0:64210 0.0.0.0:*",
                "udp UNCONN 0 0 *:53 *:*",
            ]
        )
        proc = types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        with patch.object(appmod, "run_cmd", return_value=proc):
            result = cache.port_drift_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["unexpected"], ["tcp/9999"])

    def test_backup_operation_check_verifies_newest_backup_contents(self):
        cache = appmod.DashboardCache()
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "grid-current"
            backup.mkdir()
            (backup / "note.md").write_text("current", encoding="utf-8")
            check = {
                "id": "hourly-backups",
                "label": "Backup verification",
                "host": "Pi4",
                "kind": "backup-recent",
                "path": tmp,
                "maxAgeHours": 72,
                "verifyContents": True,
            }

            result = cache.backup_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fileCount"], 1)
        self.assertGreater(result["bytes"], 0)

    def test_directory_retention_operation_check_reports_pressure(self):
        cache = appmod.DashboardCache()
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(3):
                (Path(tmp) / f"backup-{index}").mkdir()
            check = {
                "id": "backup-retention",
                "label": "Backup retention pressure",
                "host": "Pi4",
                "kind": "directory-retention",
                "path": tmp,
                "maxEntries": 5,
                "maxOldestDays": 180,
            }

            result = cache.directory_retention_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["entryCount"], 3)

    def test_file_freshness_operation_check_reads_json_status(self):
        cache = appmod.DashboardCache()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "last-restore-drill.json"
            state.write_text('{"status":"ok"}', encoding="utf-8")
            check = {
                "id": "restore-drill-state",
                "label": "Restore drill freshness",
                "host": "Pi4/Pi5",
                "kind": "file-freshness",
                "path": str(state),
                "maxAgeHours": 192,
                "jsonField": "status",
                "jsonEquals": "ok",
            }

            result = cache.file_freshness_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["jsonValue"], "ok")

    def test_backup_artifacts_operation_check_reads_archives_and_checksums(self):
        cache = appmod.DashboardCache()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / "payload.txt"
            payload.write_text("backup data", encoding="utf-8")
            archive = root / "payload.tgz"
            with appmod.tarfile.open(archive, "w:gz") as tf:
                tf.add(payload, arcname="payload.txt")
            digest = appmod.hashlib.sha256(payload.read_bytes()).hexdigest()
            (root / "payload.txt.sha256").write_text(f"{digest}  payload.txt\n", encoding="utf-8")
            (root / "external.sha256").write_text("0" * 64 + "  /etc/hosts\n", encoding="utf-8")
            check = {
                "id": "backup-artifacts",
                "label": "Backup artifact integrity",
                "host": "Pi4",
                "kind": "backup-artifacts",
                "path": tmp,
            }

            result = cache.backup_artifacts_operation_check(check, time.monotonic())

            with (
                patch.object(appmod.tarfile, "open", side_effect=AssertionError("unchanged archive should be cached")),
                patch.object(appmod.Path, "read_bytes", side_effect=AssertionError("unchanged checksum target should be cached")),
            ):
                cached_result = cache.backup_artifacts_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(cached_result["status"], "ok")
        self.assertEqual(result["archiveCount"], 1)
        self.assertEqual(result["checksumVerified"], 1)
        self.assertEqual(result["checksumFailures"], 0)
        self.assertEqual(result["checksumSkipped"], 1)

    def test_grid_sync_operation_check_reports_recent_scan(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "grid-vault-sync",
            "label": "GRID vault/index sync",
            "host": "Pi4 k3s",
            "kind": "grid-sync",
            "url": "http://example.test/api/stats",
            "minNotes": 1,
            "maxScanAgeMinutes": 30,
        }
        body = appmod.json.dumps({"notes": 42, "last_scan_at": datetime.now().isoformat()})

        with patch.object(appmod, "open_ops_url", return_value=FakeResponse(body)):
            result = cache.grid_sync_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["notes"], 42)
        self.assertLessEqual(result["scanAgeMinutes"], 1)

    def test_path_parity_operation_check_compares_files(self):
        cache = appmod.DashboardCache()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            target = Path(tmp) / "target"
            source.mkdir()
            target.mkdir()
            (source / "note.md").write_text("same", encoding="utf-8")
            (target / "note.md").write_text("same", encoding="utf-8")
            check = {
                "id": "brain-vault-parity",
                "label": "GRID vault path parity",
                "host": "Pi4",
                "kind": "path-parity",
                "source": str(source),
                "target": str(target),
                "pattern": "*.md",
            }

            result = cache.path_parity_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sourceCount"], 1)
        self.assertEqual(result["targetCount"], 1)
        self.assertEqual(result["mismatchCount"], 0)

    def test_path_parity_operation_check_allows_unreadable_hashes(self):
        cache = appmod.DashboardCache()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            target = Path(tmp) / "target"
            source.mkdir()
            target.mkdir()
            (source / "note.md").write_text("same", encoding="utf-8")
            (target / "note.md").write_text("same", encoding="utf-8")
            check = {
                "id": "brain-vault-parity",
                "label": "GRID vault path parity",
                "host": "Pi4",
                "kind": "path-parity",
                "source": str(source),
                "target": str(target),
                "pattern": "*.md",
            }

            with patch.object(appmod.Path, "read_bytes", side_effect=OSError("permission denied")):
                result = cache.path_parity_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["unreadableCount"], 2)
        self.assertEqual(result["mismatchCount"], 0)

    def test_journal_pattern_operation_check_flags_matching_errors(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "kernel-io-health",
            "label": "Kernel storage errors",
            "host": "Pi4",
            "kind": "journal-pattern",
            "since": "24 hours ago",
            "patterns": ["I/O error"],
        }
        proc = types.SimpleNamespace(returncode=0, stdout="Jul 05 host kernel: Buffer I/O error on dev sda1\n", stderr="")

        with patch.object(appmod, "run_cmd", return_value=proc):
            result = cache.journal_pattern_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["matchCount"], 1)

    def test_disk_operation_check_includes_inode_and_mount_mode(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "root-disk",
            "label": "Root disk headroom",
            "host": "Pi4",
            "kind": "disk",
            "path": "/",
            "maxPct": 85,
            "maxInodePct": 85,
        }
        usage = types.SimpleNamespace(percent=22.5)
        df_proc = types.SimpleNamespace(returncode=0, stdout="Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/root 100 4 96 4% /\n", stderr="")
        mount_proc = types.SimpleNamespace(returncode=0, stdout="rw,noatime\n", stderr="")

        with patch.object(appmod.psutil, "disk_usage", return_value=usage), patch.object(appmod, "run_cmd", side_effect=[df_proc, mount_proc]):
            result = cache.disk_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["inodePct"], 4.0)
        self.assertFalse(result["readOnly"])
        self.assertIn("rw", result["message"])

    def test_boot_state_lifecycle_distinguishes_unknown_clean_and_unclean(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            boot_id_path = Path(tmp) / "boot-id"
            boot_id_path.write_text("boot-a\n", encoding="utf-8")

            first = bootstate.mark_boot_started(state_path, boot_id_path, "2026-07-09T10:00:00-06:00")
            self.assertIsNone(first["previousBootClean"])
            self.assertFalse(first["currentBootClean"])
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o644)

            clean = bootstate.mark_boot_clean(state_path, boot_id_path, "2026-07-09T11:00:00-06:00")
            self.assertTrue(clean["currentBootClean"])

            boot_id_path.write_text("boot-b\n", encoding="utf-8")
            second = bootstate.mark_boot_started(state_path, boot_id_path, "2026-07-09T12:00:00-06:00")
            self.assertTrue(second["previousBootClean"])
            self.assertEqual(second["uncleanBootCount"], 0)

            boot_id_path.write_text("boot-c\n", encoding="utf-8")
            third = bootstate.mark_boot_started(state_path, boot_id_path, "2026-07-09T13:00:00-06:00")
            self.assertFalse(third["previousBootClean"])
            self.assertEqual(third["lastUncleanBootId"], "boot-b")
            self.assertEqual(third["uncleanBootCount"], 1)

    def test_boot_state_check_reports_initial_baseline_and_unclean_boot(self):
        cache = appmod.DashboardCache()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            boot_id_path = Path(tmp) / "boot-id"
            boot_id_path.write_text("boot-current\n", encoding="utf-8")
            check = {
                "id": "pi4-boot-state",
                "label": "Pi4 clean-shutdown state",
                "kind": "boot-state",
                "path": str(state_path),
                "bootIdPath": str(boot_id_path),
                "failureStatus": "fail",
            }

            state_path.write_text(json.dumps({"currentBootId": "boot-current", "currentBootClean": False, "previousBootClean": None}), encoding="utf-8")
            baseline = cache.boot_state_operation_check(check, time.monotonic())
            self.assertEqual(baseline["status"], "warn")
            self.assertEqual(baseline["trackingState"], "baseline")

            state_path.write_text(json.dumps({"currentBootId": "boot-current", "currentBootClean": False, "previousBootId": "boot-old", "previousBootClean": False, "uncleanBootCount": 1}), encoding="utf-8")
            unclean = cache.boot_state_operation_check(check, time.monotonic())
            self.assertEqual(unclean["status"], "fail")
            self.assertEqual(unclean["trackingState"], "unclean")

            state_path.write_text(json.dumps({"currentBootId": "boot-current", "currentBootClean": False, "previousBootId": "boot-old", "previousBootClean": True}), encoding="utf-8")
            clean = cache.boot_state_operation_check(check, time.monotonic())
            self.assertEqual(clean["status"], "ok")

    def test_mount_operation_check_validates_source_filesystem_and_mode(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "data-hdd-mount",
            "label": "Data HDD mount integrity",
            "kind": "mount",
            "path": "/mnt/ssd",
            "expectedUuid": "b0a1a356-3c0e-4f68-9c80-3379f662b4bc",
            "expectedFstype": "ext4",
            "requireReadWrite": True,
            "failureStatus": "fail",
        }
        healthy_proc = types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"filesystems": [{"target": "/mnt/ssd", "source": "/dev/sda1", "fstype": "ext4", "options": "rw,noatime", "uuid": "b0a1a356-3c0e-4f68-9c80-3379f662b4bc"}]}),
            stderr="",
        )
        wrong_proc = types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"filesystems": [{"target": "/mnt/ssd", "source": "/dev/mmcblk0p2", "fstype": "ext4", "options": "ro", "uuid": "wrong"}]}),
            stderr="",
        )

        with patch.object(appmod, "run_cmd", return_value=healthy_proc):
            healthy = cache.mount_operation_check(check, time.monotonic())
        with patch.object(appmod, "run_cmd", return_value=wrong_proc):
            wrong = cache.mount_operation_check(check, time.monotonic())

        self.assertEqual(healthy["status"], "ok")
        self.assertEqual(healthy["source"], "/dev/sda1")
        self.assertEqual(wrong["status"], "fail")
        self.assertTrue(wrong["readOnly"])

    def test_systemd_service_result_check_catches_failed_oneshot(self):
        cache = appmod.DashboardCache()
        check = {"id": "backup-service-result", "label": "Pi4 backup result", "kind": "systemd-service-result", "unit": "pi4-backup.service", "failureStatus": "fail"}
        success_proc = types.SimpleNamespace(returncode=0, stdout="ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nExecMainStartTimestamp=Thu 2026-07-09 02:20:00 MDT\nExecMainExitTimestamp=Thu 2026-07-09 02:21:00 MDT\n", stderr="")
        failed_proc = types.SimpleNamespace(returncode=0, stdout="ActiveState=failed\nSubState=failed\nResult=exit-code\nExecMainStatus=1\nExecMainStartTimestamp=Thu 2026-07-09 02:20:00 MDT\nExecMainExitTimestamp=Thu 2026-07-09 02:20:02 MDT\n", stderr="")

        with patch.object(appmod, "run_cmd", return_value=success_proc):
            success = cache.systemd_service_result_operation_check(check, time.monotonic())
        with patch.object(appmod, "run_cmd", return_value=failed_proc):
            failed = cache.systemd_service_result_operation_check(check, time.monotonic())

        self.assertEqual(success["status"], "ok")
        self.assertEqual(failed["status"], "fail")
        self.assertEqual(failed["exitStatus"], 1)

    def test_smart_operation_check_handles_exit_bit_four_health_and_failures(self):
        cache = appmod.DashboardCache()
        check = {"id": "data-hdd-smart", "label": "Data HDD SMART health", "kind": "smart", "maxTempC": 50, "failureStatus": "fail", "unavailableStatus": "warn"}
        healthy_data = {
            "_pi4_noc": {"available": True, "exitStatus": 4},
            "smart_status": {"passed": True},
            "temperature": {"current": 38},
            "ata_smart_attributes": {"table": [
                {"id": 5, "raw": {"value": 0}},
                {"id": 197, "raw": {"value": 0}},
                {"id": 198, "raw": {"value": 0}},
                {"id": 199, "raw": {"value": 0}},
            ]},
            "ata_smart_self_test_log": {"standard": {"table": [{"status": {"passed": True, "string": "Completed without error"}}]}},
        }
        failed_data = json.loads(json.dumps(healthy_data))
        failed_data["ata_smart_attributes"]["table"][1]["raw"]["value"] = 2

        with patch.object(appmod, "run_privileged_json", return_value=healthy_data):
            healthy = cache.smart_operation_check(check, time.monotonic())
        with patch.object(appmod, "run_privileged_json", return_value=failed_data):
            failed = cache.smart_operation_check(check, time.monotonic())
        with patch.object(appmod, "run_privileged_json", return_value={"_pi4_noc": {"available": False, "exitStatus": 2, "reason": "SMART unavailable"}}):
            unavailable = cache.smart_operation_check(check, time.monotonic())

        self.assertEqual(healthy["status"], "ok")
        self.assertEqual(failed["status"], "fail")
        self.assertEqual(failed["pendingSectors"], 2)
        self.assertEqual(unavailable["status"], "warn")

    def test_smart_helper_is_fixed_to_persistent_wwn_and_accepts_partial_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            smartctl = Path(tmp) / "smartctl"
            smartctl.write_text("", encoding="utf-8")
            proc = types.SimpleNamespace(returncode=4, stdout=json.dumps({"smart_status": {"passed": True}, "ata_smart_attributes": {"table": []}}))
            with patch.object(sudo_ops, "SMARTCTL", smartctl), patch.object(sudo_ops.subprocess, "run", return_value=proc) as mocked:
                result = sudo_ops.smart_health()

        argv = mocked.call_args.args[0]
        self.assertEqual(argv[-1], "/dev/disk/by-id/wwn-0x50014ee2bebee4fe")
        self.assertIn("sat", argv)
        self.assertTrue(result["_pi4_noc"]["available"])

    def test_smart_operation_check_preserves_drive_standby(self):
        cache = appmod.DashboardCache()
        check = {"id": "data-hdd-smart", "label": "Data HDD SMART health", "kind": "smart"}
        data = {"_pi4_noc": {"available": True, "exitStatus": 0}, "power_mode": "STANDBY"}

        with patch.object(appmod, "run_privileged_json", return_value=data):
            result = cache.smart_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["powerMode"], "standby")

    def test_boot_and_smart_systemd_units_use_safe_fixed_commands(self):
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        boot_unit = (scripts / "pi4-boot-state.service").read_text(encoding="utf-8")
        short_unit = (scripts / "pi4-smart-short.service").read_text(encoding="utf-8")
        short_timer = (scripts / "pi4-smart-short.timer").read_text(encoding="utf-8")
        long_unit = (scripts / "pi4-smart-long.service").read_text(encoding="utf-8")
        long_timer = (scripts / "pi4-smart-long.timer").read_text(encoding="utf-8")

        self.assertIn("RefuseManualStop=yes", boot_unit)
        self.assertIn("ExecStop=/usr/local/sbin/pi4-boot-state stop", boot_unit)
        self.assertIn("ConditionFileIsExecutable=/usr/sbin/smartctl", short_unit)
        self.assertNotIn("ConditionPathIsExecutable", short_unit)
        self.assertIn("/usr/sbin/smartctl -d sat -t short /dev/disk/by-id/wwn-0x50014ee2bebee4fe", short_unit)
        self.assertIn("OnCalendar=Sun *-*-* 04:30:00", short_timer)
        self.assertIn("Persistent=false", short_timer)
        self.assertIn("/usr/sbin/smartctl -d sat -t long /dev/disk/by-id/wwn-0x50014ee2bebee4fe", long_unit)
        self.assertIn("ConditionFileIsExecutable=/usr/sbin/smartctl", long_unit)
        self.assertNotIn("ConditionPathIsExecutable", long_unit)
        self.assertIn("OnCalendar=*-*-01 05:30:00", long_timer)
        self.assertIn("Persistent=false", long_timer)

    def test_storage_snapshot_identifies_rotational_data_drive(self):
        cache = appmod.DashboardCache()
        cache.data_drive = {"label": "Data HDD", "media": "HDD", "model": "WDC", "device": "/dev/sda1", "rotational": True, "transport": "USB"}
        cache.nas_size_gb = 300
        root = types.SimpleNamespace(total=100 * 1024**3, used=20 * 1024**3)
        data = types.SimpleNamespace(total=2000 * 1024**3, used=400 * 1024**3)
        storage = cache.storage_snapshot(root, data)
        cache.snapshot_data["STORAGE"] = storage

        self.assertEqual(storage["ssd"]["media"], "HDD")
        self.assertTrue(storage["ssd"]["rotational"])
        self.assertEqual(next(row for row in cache.kpis() if row["id"] == "ssd")["label"], "Data HDD Used")

    def test_speed_operation_check_reports_sample_mbps(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "wan-speed",
            "label": "WAN speed sample",
            "host": "Internet",
            "kind": "speed-lite",
            "url": "https://speed.example.test",
            "minMbps": 0.1,
        }

        with patch.object(appmod, "open_ops_url", return_value=FakeResponse("x" * 100000)):
            result = cache.speed_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertGreater(result["mbps"], 0)
        self.assertIn("Mbps", result["message"])

    def test_github_release_operation_check_reports_latest_tag(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "k3s-release",
            "label": "k3s latest release",
            "host": "GitHub",
            "kind": "github-release",
            "repo": "k3s-io/k3s",
        }

        with patch.object(appmod, "open_ops_url", return_value=FakeResponse('{"tag_name":"v1.35.5+k3s1"}')):
            result = cache.github_release_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["release"], "v1.35.5+k3s1")
        self.assertIn("github.com/k3s-io/k3s", result["href"])


if __name__ == "__main__":
    unittest.main()
