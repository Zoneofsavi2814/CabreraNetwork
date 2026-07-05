import time
import unittest
import sys
import types
from datetime import datetime
from unittest.mock import patch

try:
    import psutil  # noqa: F401
except ModuleNotFoundError:
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.net_io_counters = lambda: types.SimpleNamespace(bytes_recv=0, bytes_sent=0)
    fake_psutil.disk_io_counters = lambda: types.SimpleNamespace(read_bytes=0, write_bytes=0)
    fake_psutil.cpu_percent = lambda interval=None: 0
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


class OpsCenterTests(unittest.TestCase):
    def test_ops_config_covers_required_cadence_work(self):
        five_minute_ids = {check["id"] for check in appmod.OPS_CHECK_CONFIG["five-minute"]}
        hourly_ids = {check["id"] for check in appmod.OPS_CHECK_CONFIG["hourly"]}
        nightly_ids = {check["id"] for check in appmod.OPS_CHECK_CONFIG["nightly"]}

        self.assertTrue({"gateway", "dns-resolver", "wan-http"}.issubset(five_minute_ids))
        self.assertTrue({"wan-speed", "k3s-release", "hourly-backups"}.issubset(hourly_ids))
        self.assertTrue(
            {
                "logrotate-timer",
                "log2ram-flush",
                "tmpfiles-clean",
                "dpkg-backup",
                "ssd-trim",
                "root-disk",
                "ssd-disk",
                "brain-mount",
                "brain-freshness",
            }.issubset(nightly_ids)
        )
        self.assertEqual(appmod.OPS_CADENCE_CONFIG[2]["scheduleTime"], "07:00")
        self.assertEqual(appmod.OPS_CADENCE_CONFIG[3]["scheduleTime"], "23:55")

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

    def test_http_operation_check_warns_on_degraded_json(self):
        cache = appmod.DashboardCache()
        check = {
            "id": "coinbot",
            "label": "Coinbot",
            "host": "Pi5 k3s",
            "kind": "http",
            "url": "http://example.test/health",
            "jsonField": "status",
            "jsonEquals": "ok",
            "warnJsonField": "degraded",
        }

        with patch.object(appmod.urlrequest, "urlopen", return_value=FakeResponse('{"status":"ok","degraded":true}')):
            result = cache.http_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["httpStatus"], 200)
        self.assertIn("degraded", result["message"])

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
        }
        proc = types.SimpleNamespace(returncode=0, stdout="active\n", stderr="")

        with patch.object(appmod, "run_cmd", return_value=proc):
            result = cache.timer_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["unit"], "logrotate.timer")
        self.assertEqual(result["message"], "active")

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

        with patch.object(appmod.urlrequest, "urlopen", return_value=FakeResponse("x" * 100000)):
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

        with patch.object(appmod.urlrequest, "urlopen", return_value=FakeResponse('{"tag_name":"v1.35.5+k3s1"}')):
            result = cache.github_release_operation_check(check, time.monotonic())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["release"], "v1.35.5+k3s1")
        self.assertIn("github.com/k3s-io/k3s", result["href"])


if __name__ == "__main__":
    unittest.main()
