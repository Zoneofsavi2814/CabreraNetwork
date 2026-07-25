import json
import os
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

try:
    import psutil  # noqa: F401
except ModuleNotFoundError:
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.net_io_counters = lambda **kwargs: types.SimpleNamespace(bytes_recv=0, bytes_sent=0)
    fake_psutil.disk_io_counters = lambda **kwargs: types.SimpleNamespace(read_bytes=0, write_bytes=0)
    fake_psutil.disk_usage = lambda path: types.SimpleNamespace(total=1, used=0, percent=0)
    fake_psutil.virtual_memory = lambda: types.SimpleNamespace(percent=0, used=0, total=1)
    fake_psutil.swap_memory = lambda: types.SimpleNamespace(percent=0)
    fake_psutil.sensors_temperatures = lambda: {}
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


class CollectorPerformanceTests(unittest.TestCase):
    def assert_snapshot_wire_schema(self, payload):
        required_sections = {
            "HISTORY",
            "KPIS",
            "SERVICES",
            "ADGUARD",
            "K3S",
            "STORAGE",
            "TOPOLOGY",
            "LOGS",
            "WEB_APPS",
            "OPS_CENTER",
            "HOST",
            "META",
        }
        self.assertTrue(required_sections.issubset(payload))
        self.assertEqual(
            set(payload["HISTORY"]),
            {
                "cpu",
                "ram",
                "temp",
                "ssdPct",
                "dnsPerMin",
                "loadAvg",
                "netIn",
                "netOut",
                "dnsTotal",
                "dnsBlocked",
                "diskRead",
                "diskWrite",
            },
        )
        for history in payload["HISTORY"].values():
            self.assertIsInstance(history, list)
        for section in ("KPIS", "SERVICES", "LOGS", "WEB_APPS"):
            self.assertIsInstance(payload[section], list)
        for section in ("ADGUARD", "K3S", "STORAGE", "TOPOLOGY", "OPS_CENTER", "HOST", "META"):
            self.assertIsInstance(payload[section], dict)
        self.assertTrue({"queries", "blocked", "blockRatio", "topDomains", "topClients", "status"}.issubset(payload["ADGUARD"]))
        self.assertTrue({"version", "nodes", "podsByNs", "events", "workloads"}.issubset(payload["K3S"]))
        self.assertTrue({"router", "host", "clients", "counts", "aggregateKbps", "source", "routerCollector", "updatedAt"}.issubset(payload["TOPOLOGY"]))
        self.assertTrue({"schemaVersion", "updatedAt", "summary", "cadences", "events"}.issubset(payload["OPS_CENTER"]))
        self.assertTrue({"updatedAt", "source"}.issubset(payload["META"]))

    def test_default_collector_cadences_match_balanced_profile(self):
        self.assertEqual(
            (
                appmod.HOT_REFRESH_SECONDS,
                appmod.SERVICE_REFRESH_SECONDS,
                appmod.HEAVY_REFRESH_SECONDS,
                appmod.ROUTER_REFRESH_SECONDS,
                appmod.STORAGE_REFRESH_SECONDS,
            ),
            (2, 15, 60, 120, 1800),
        )

    def test_refresh_settings_are_bounded(self):
        with patch.dict(os.environ, {"TEST_REFRESH": "0"}):
            self.assertEqual(appmod.bounded_refresh_seconds("TEST_REFRESH", 15, 5, 300), 15)
        with patch.dict(os.environ, {"TEST_REFRESH": "not-a-number"}):
            self.assertEqual(appmod.bounded_refresh_seconds("TEST_REFRESH", 15, 5, 300), 15)
        with patch.dict(os.environ, {"TEST_REFRESH": "1"}):
            self.assertEqual(appmod.bounded_refresh_seconds("TEST_REFRESH", 15, 5, 300), 5)
        with patch.dict(os.environ, {"TEST_REFRESH": "9999"}):
            self.assertEqual(appmod.bounded_refresh_seconds("TEST_REFRESH", 15, 5, 300), 300)

    def test_start_wires_the_five_collector_cadences(self):
        created = []

        class FakeThread:
            def __init__(self, *, target, args=(), daemon=False):
                self.target = target
                self.args = args
                self.daemon = daemon
                created.append(self)

            def start(self):
                return None

        cache = appmod.DashboardCache()
        with patch.object(appmod.threading, "Thread", FakeThread), patch.object(cache, "update_hot"):
            cache.start()

        loops = {thread.target.__name__: thread.args for thread in created if thread.args}
        self.assertEqual(loops["_hot_loop"], (appmod.HOT_REFRESH_SECONDS,))
        self.assertEqual(loops["_service_loop"], (appmod.SERVICE_REFRESH_SECONDS,))
        self.assertEqual(loops["_heavy_loop"], (appmod.HEAVY_REFRESH_SECONDS,))
        self.assertEqual(loops["_router_loop"], (appmod.ROUTER_REFRESH_SECONDS,))
        self.assertEqual(loops["_storage_loop"], (appmod.STORAGE_REFRESH_SECONDS,))

    def test_refresh_boundary_continues_after_transient_failure(self):
        cache = appmod.DashboardCache()
        calls = []

        def fail_once():
            calls.append("failed")
            raise RuntimeError("temporary collector failure")

        def succeed():
            calls.append("succeeded")

        with patch("builtins.print") as log:
            self.assertFalse(cache._run_refreshes("test", fail_once, succeed))
        self.assertEqual(calls, ["failed", "succeeded"])
        self.assertIn("temporary collector failure", log.call_args.args[0])

        calls.clear()
        self.assertTrue(cache._run_refreshes("test", succeed))
        self.assertEqual(calls, ["succeeded"])

    def test_hot_update_reuses_static_host_data_and_does_not_scan_nas(self):
        cache = appmod.DashboardCache()
        cached_host = dict(cache.host_static)
        cache.prev_net = types.SimpleNamespace(bytes_recv=100, bytes_sent=200)
        cache.prev_disk = types.SimpleNamespace(read_bytes=300, write_bytes=400)
        vm = types.SimpleNamespace(percent=25.0, used=2 * 1024**3, total=8 * 1024**3)
        swap = types.SimpleNamespace(percent=1.0)
        usage = types.SimpleNamespace(total=100 * 1024**3, used=20 * 1024**3, percent=20.0)

        with (
            patch.object(cache, "path_size_gb", side_effect=AssertionError("hot path must not run du")),
            patch.object(cache, "temperature_c", return_value=40.0),
            patch.object(appmod.psutil, "cpu_percent", return_value=3.0),
            patch.object(appmod.psutil, "virtual_memory", return_value=vm, create=True),
            patch.object(appmod.psutil, "swap_memory", return_value=swap, create=True),
            patch.object(appmod.psutil, "net_io_counters", return_value=types.SimpleNamespace(bytes_recv=110, bytes_sent=220)),
            patch.object(appmod.psutil, "disk_io_counters", return_value=types.SimpleNamespace(read_bytes=330, write_bytes=440)),
            patch.object(appmod.psutil, "disk_usage", return_value=usage),
            patch.object(appmod.os, "getloadavg", return_value=(0.1, 0.2, 0.3)),
            patch.object(appmod.socket, "gethostname", side_effect=AssertionError("hostname should be cached")),
            patch.object(appmod, "local_ip", side_effect=AssertionError("IP should be cached")),
            patch.object(appmod.platform, "platform", side_effect=AssertionError("platform should be cached")),
            patch.object(appmod.psutil, "boot_time", side_effect=AssertionError("boot time should be cached")),
        ):
            cache.update_hot()

        for key, value in cached_host.items():
            self.assertEqual(cache.snapshot_data["HOST"][key], value)

    def test_temperature_prefers_direct_thermal_zone(self):
        cache = appmod.DashboardCache()
        thermal = types.SimpleNamespace(read_text=lambda **kwargs: "41868\n")
        with (
            patch.object(appmod, "THERMAL_ZONE_TEMP", thermal),
            patch.object(appmod.psutil, "sensors_temperatures", side_effect=AssertionError("fallback should not run"), create=True),
        ):
            self.assertEqual(cache.temperature_c(), 41.868)

    def test_storage_scan_retains_last_successful_size(self):
        cache = appmod.DashboardCache()
        usage = types.SimpleNamespace(total=100 * 1024**3, used=20 * 1024**3, percent=20.0)
        cache.nas_size_gb = 41

        with patch.object(cache, "path_size_gb", return_value=None):
            self.assertFalse(cache.update_storage_size())
        self.assertEqual(cache.nas_size_gb, 41)

        with patch.object(cache, "path_size_gb", return_value=52), patch.object(appmod.psutil, "disk_usage", return_value=usage):
            self.assertTrue(cache.update_storage_size())
        self.assertEqual(cache.nas_size_gb, 52)
        self.assertEqual(cache.snapshot_data["STORAGE"]["ssd"]["segments"][0]["value"], 52)

    def test_path_size_rejects_failed_and_unparseable_du(self):
        failed = types.SimpleNamespace(returncode=1, stdout="permission denied")
        malformed = types.SimpleNamespace(returncode=0, stdout="unknown")
        valid = types.SimpleNamespace(returncode=0, stdout="123G\t/mnt/ssd/nas\n")
        with patch.object(appmod.Path, "exists", return_value=True):
            with patch.object(appmod, "run_cmd", return_value=failed):
                self.assertIsNone(appmod.DashboardCache().path_size_gb("/mnt/ssd/nas"))
            with patch.object(appmod, "run_cmd", return_value=malformed):
                self.assertIsNone(appmod.DashboardCache().path_size_gb("/mnt/ssd/nas"))
            with patch.object(appmod, "run_cmd", return_value=valid):
                self.assertEqual(appmod.DashboardCache().path_size_gb("/mnt/ssd/nas"), 123)

    def test_systemd_show_parser_and_service_refresh_are_batched(self):
        output = (
            "Id=alpha.service\nActiveState=active\nSubState=running\n\n"
            "Id=beta.service\nActiveState=inactive\nSubState=dead\n"
        )
        parsed = appmod.parse_systemd_show(output, ["alpha.service", "beta.service"])
        self.assertEqual(parsed["alpha.service"]["ActiveState"], "active")
        self.assertEqual(parsed["beta.service"]["SubState"], "dead")
        with_missing = appmod.parse_systemd_show(output, ["alpha.service", "missing.service", "beta.service"])
        self.assertEqual(with_missing["missing.service"], {})
        self.assertEqual(with_missing["beta.service"]["SubState"], "dead")

        cache = appmod.DashboardCache()
        systemd_units = [cfg["unit"] for cfg in appmod.UNIT_CONFIG if cfg.get("kind") != "k3s"]
        rows = {unit: {"ActiveState": "active", "SubState": "running", "NRestarts": "0", "MainPID": "1"} for unit in systemd_units}
        all_ports = {str(port) for cfg in appmod.UNIT_CONFIG for port in cfg["ports"]} | {str(cfg["port"]) for cfg in appmod.WEB_APP_CONFIG}
        with (
            patch.object(appmod, "listening_ports", return_value=all_ports),
            patch.object(appmod, "probe_ports", return_value={port: True for port in all_ports}),
            patch.object(appmod, "service_show_many", return_value=rows) as batched,
        ):
            cache.update_services()
        batched.assert_called_once_with(systemd_units)

    def test_run_json_keeps_stderr_out_of_json_and_accepts_client_environment(self):
        proc = types.SimpleNamespace(returncode=0, stdout='{"items":[]}', stderr="config warning")
        environment = {"K3S_CONFIG_FILE": "/dev/null"}
        with patch.object(appmod.subprocess, "run", return_value=proc) as run:
            result = appmod.run_json(["kubectl", "get", "pods"], env=environment)

        self.assertEqual(result, {"items": []})
        self.assertEqual(run.call_args.kwargs["stderr"], appmod.subprocess.PIPE)
        self.assertEqual(run.call_args.kwargs["env"], environment)

    def test_k3s_direct_inventory_kubectl_fallback_and_stale_retention(self):
        document = {
            "items": [
                {
                    "kind": "Node",
                    "metadata": {"name": "pi4", "creationTimestamp": "2026-01-01T00:00:00Z", "labels": {"node-role.kubernetes.io/control-plane": "true"}},
                    "status": {"nodeInfo": {"kubeletVersion": "v1.33.1+k3s1"}, "conditions": [{"type": "Ready", "status": "True"}]},
                },
                {"kind": "Pod", "metadata": {"namespace": "homelab"}, "status": {"phase": "Running"}},
                {"kind": "Event", "metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"}, "reason": "Older", "type": "Normal", "involvedObject": {"kind": "Pod", "name": "old"}},
                {"kind": "Event", "metadata": {"creationTimestamp": "2026-01-02T00:00:00Z"}, "reason": "Newer", "type": "Normal", "involvedObject": {"kind": "Pod", "name": "new"}},
                {"kind": "Deployment", "metadata": {"name": "grid", "namespace": "homelab"}, "spec": {"replicas": 1}, "status": {"replicas": 1, "readyReplicas": 1}},
                {"kind": "DaemonSet", "metadata": {"name": "agent", "namespace": "homelab"}, "status": {"desiredNumberScheduled": 2, "numberReady": 2}},
            ]
        }
        cache = appmod.DashboardCache()
        direct = Mock()
        direct.get_inventory.return_value = document
        cache.k3s_client = direct
        with patch.object(appmod, "run_json") as fallback:
            self.assertTrue(cache.update_k3s())
        direct.get_inventory.assert_called_once_with()
        fallback.assert_not_called()
        self.assertEqual(cache.snapshot_data["K3S"]["nodes"][0]["name"], "pi4")
        self.assertEqual(cache.snapshot_data["K3S"]["events"][0]["reason"], "Newer")
        daemon = next(item for item in cache.snapshot_data["K3S"]["workloads"] if item["kind"] == "daemonset")
        self.assertEqual((daemon["ready"], daemon["desired"]), (2, 2))

        direct.get_inventory.side_effect = appmod.K3sClientError("direct inventory unavailable")
        with patch.object(appmod, "run_json", return_value=document) as fallback:
            self.assertTrue(cache.update_k3s())
        self.assertEqual(fallback.call_args.kwargs["env"]["K3S_CONFIG_FILE"], "/dev/null")
        self.assertEqual(fallback.call_args.kwargs["env"]["KUBECONFIG"], "/etc/rancher/k3s/k3s.yaml")

        last_good = json.loads(json.dumps(cache.snapshot_data["K3S"]))
        with patch.object(appmod, "run_json", return_value=None):
            self.assertFalse(cache.update_k3s())
        self.assertEqual(cache.snapshot_data["K3S"], last_good)

    def test_serialized_snapshot_is_reused_by_revision(self):
        cache = appmod.DashboardCache()
        real_dumps = appmod.json.dumps
        with patch.object(appmod.json, "dumps", side_effect=real_dumps) as dumps:
            first = cache.serialized_snapshot()
            second = cache.serialized_snapshot()
            self.assertEqual(first, second)
            self.assertEqual(dumps.call_count, 1)
            with cache.lock:
                cache.snapshot_data["META"] = {**cache.snapshot_data["META"], "updatedAt": "changed"}
                cache._touch_locked()
            third = cache.serialized_snapshot()
            self.assertNotEqual(first[0], third[0])
            self.assertEqual(dumps.call_count, 2)

    def test_serialization_retries_after_concurrent_section_publication(self):
        cache = appmod.DashboardCache()
        entered_dump = threading.Event()
        release_dump = threading.Event()
        real_dumps = appmod.json.dumps
        calls = 0
        captured_meta = []

        def slow_first_dump(value, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered_dump.set()
                self.assertTrue(release_dump.wait(1))
            captured_meta.append(value["META"]["updatedAt"])
            return real_dumps(value, **kwargs)

        result = []
        with patch.object(appmod.json, "dumps", side_effect=slow_first_dump):
            worker = threading.Thread(target=lambda: result.append(cache.serialized_snapshot()))
            worker.start()
            self.assertTrue(entered_dump.wait(1))
            with cache.lock:
                cache.snapshot_data["META"] = {**cache.snapshot_data["META"], "updatedAt": "concurrent"}
                cache._touch_locked()
            release_dump.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(calls, 2)
        self.assertNotEqual(captured_meta[0], "concurrent")
        self.assertEqual(captured_meta[1], "concurrent")
        self.assertEqual(json.loads(result[0][1])["META"]["updatedAt"], "concurrent")

    def test_concurrent_serialization_releases_cache_lock_and_shares_payload(self):
        cache = appmod.DashboardCache()
        entered_dump = threading.Event()
        release_dump = threading.Event()
        results = []
        real_dumps = appmod.json.dumps

        def slow_dumps(value, **kwargs):
            entered_dump.set()
            self.assertTrue(release_dump.wait(1))
            return real_dumps(value, **kwargs)

        def serialize():
            results.append(cache.serialized_snapshot())

        with patch.object(appmod.json, "dumps", side_effect=slow_dumps) as dumps:
            first = threading.Thread(target=serialize)
            second = threading.Thread(target=serialize)
            first.start()
            self.assertTrue(entered_dump.wait(1))

            lock_available = cache.lock.acquire(timeout=0.5)
            self.assertTrue(lock_available)
            if lock_available:
                cache.lock.release()

            second.start()
            release_dump.set()
            first.join(1)
            second.join(1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(dumps.call_count, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])

    def test_snapshot_route_preserves_wire_schema_and_values(self):
        route_cache = appmod.DashboardCache()
        with route_cache.lock:
            route_cache.snapshot_data["HOST"] = {"name": "pi4", "cpuPct": 2.5}
            route_cache.snapshot_data["K3S"]["workloads"] = [
                {
                    "namespace": "homelab",
                    "kind": "deployment",
                    "name": "grid",
                    "ready": 1,
                    "desired": 1,
                    "rolloutAllowed": True,
                }
            ]
            route_cache._touch_locked()
        expected = route_cache.snapshot()

        with (
            patch.object(appmod, "cache", route_cache),
            patch.object(appmod, "jsonify", side_effect=lambda value: value),
        ):
            payload = appmod.api_snapshot()

        self.assertEqual(payload, expected)
        self.assert_snapshot_wire_schema(payload)
        payload["HOST"]["name"] = "client-side mutation"
        self.assertEqual(route_cache.snapshot_data["HOST"]["name"], "pi4")

    def test_events_route_preserves_sse_framing_schema_and_revision_reuse(self):
        route_cache = appmod.DashboardCache()
        with route_cache.lock:
            route_cache.snapshot_data["HOST"] = {"name": "pi4", "cpuPct": 1.0}
            route_cache._touch_locked()
        initial_snapshot = route_cache.snapshot()
        response_args = {}

        def capture_response(iterable, *, mimetype, headers):
            response_args.update(iterable=iterable, mimetype=mimetype, headers=headers)
            return response_args

        real_dumps = appmod.json.dumps
        with (
            patch.object(appmod, "cache", route_cache),
            patch.object(appmod, "Response", side_effect=capture_response),
            patch.object(appmod.time, "sleep", return_value=None),
            patch.object(appmod.json, "dumps", side_effect=real_dumps) as dumps,
        ):
            response = appmod.api_events()
            stream = response["iterable"]
            first_frame = next(stream)
            self.assertEqual(first_frame, f"data: {real_dumps(initial_snapshot, separators=(',', ':'))}\n\n")
            self.assertEqual(dumps.call_count, 1)
            self.assert_snapshot_wire_schema(json.loads(first_frame.removeprefix("data: ").removesuffix("\n\n")))

            self.assertEqual(next(stream), ": keepalive\n\n")
            self.assertEqual(dumps.call_count, 1)

            with route_cache.lock:
                route_cache.snapshot_data["META"]["updatedAt"] = "2026-07-14T18:00:00"
                route_cache._touch_locked()
            updated_snapshot = route_cache.snapshot()
            updated_frame = next(stream)
            self.assertEqual(updated_frame, f"data: {real_dumps(updated_snapshot, separators=(',', ':'))}\n\n")
            self.assertEqual(dumps.call_count, 2)
            stream.close()

        self.assertEqual(response["mimetype"], "text/event-stream")
        self.assertEqual(response["headers"], {"Cache-Control": "no-cache", "Connection": "keep-alive"})


if __name__ == "__main__":
    unittest.main()
