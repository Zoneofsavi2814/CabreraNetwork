import unittest
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

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


class TopologyCollectorTests(unittest.TestCase):
    def make_cache(self):
        cache = appmod.DashboardCache()
        cache.snapshot_data["HOST"] = {"name": "pi4", "ip": "192.168.0.101"}
        cache.snapshot_data["HISTORY"] = {"netIn": [0], "netOut": [0]}
        return cache

    def collect(self, cache, router_doc, neighbors, aliases=None):
        aliases = aliases or {"devices": {}, "updatedAt": None}
        with (
            patch.object(appmod, "default_gateway", return_value="192.168.0.1"),
            patch.object(appmod, "load_device_aliases", return_value=aliases),
            patch.object(cache, "read_router_topology_file", return_value=router_doc),
            patch.object(cache, "collect_neighbors", return_value=neighbors),
            patch.object(cache, "resolve_client_name", return_value={}),
        ):
            return cache.collect_topology()

    def test_router_link_and_ap_beat_arp_observation(self):
        cache = self.make_cache()
        topology = self.collect(
            cache,
            {
                "clients": [
                    {
                        "name": "Anns-MacBook",
                        "ip": "192.168.0.42",
                        "mac": "0e:5c:e5:70:c3:5a",
                        "interface": "5G",
                        "apId": "loft",
                    }
                ]
            },
            [{"ip": "192.168.0.42", "mac": "0e:5c:e5:70:c3:5a", "interface": "LAN", "online": True}],
        )

        row = next(client for client in topology["clients"] if client["ip"] == "192.168.0.42")
        self.assertEqual(row["displayName"], "Anns-MacBook")
        self.assertEqual(row["linkType"], "5g")
        self.assertEqual(row["groupId"], "loft")
        self.assertTrue(row["online"])

    def test_pure_topology_helper_caches_are_bounded(self):
        helpers = (
            appmod.is_ipv4,
            appmod.normalize_mac,
            appmod.normalize_ap_id,
            appmod.normalize_link_type,
            appmod.looks_like_default_ap,
            appmod.device_key,
        )
        for helper in helpers:
            helper.cache_clear()
            self.assertEqual(helper.cache_info().maxsize, 1024)

        self.assertTrue(appmod.is_ipv4("192.168.0.1"))
        self.assertEqual(appmod.normalize_mac("AA-BB-CC-DD-EE-FF"), "aa:bb:cc:dd:ee:ff")
        self.assertEqual(appmod.normalize_link_type("5G"), "5g")
        self.assertEqual(appmod.device_key("192.168.0.1", ""), "ip:192.168.0.1")
        for index in range(1100):
            appmod.is_ipv4(f"10.{index // 256}.{index % 256}.1")
        self.assertLessEqual(appmod.is_ipv4.cache_info().currsize, 1024)

    def test_arp_only_devices_are_lan_observed_not_wired(self):
        cache = self.make_cache()
        topology = self.collect(
            cache,
            {"clients": []},
            [{"ip": "192.168.0.88", "mac": "aa:bb:cc:dd:ee:ff", "interface": "LAN", "online": True}],
        )

        row = next(client for client in topology["clients"] if client["ip"] == "192.168.0.88")
        self.assertEqual(row["linkType"], "lan-observed")
        self.assertEqual(row["interface"], "LAN observed")
        self.assertEqual(topology["counts"]["wired"], 0)
        self.assertEqual(topology["counts"]["wifi"], 0)
        self.assertEqual(topology["counts"]["unknown"], 1)

    def test_unnamed_clients_bound_resolution_attempts(self):
        cache = self.make_cache()
        ip_neighbors = [
            {
                "ip": f"192.168.0.{100 + index}",
                "mac": f"aa:bb:cc:dd:ee:{index:02x}",
                "interface": "LAN",
                "online": True,
            }
            for index in range(10)
        ]
        mac_only_neighbors = [
            {
                "ip": "",
                "mac": f"aa:bb:cc:ff:ee:{index:02x}",
                "interface": "LAN",
                "online": True,
            }
            for index in range(4)
        ]
        neighbors = mac_only_neighbors + ip_neighbors
        with (
            patch.object(appmod, "default_gateway", return_value="192.168.0.1"),
            patch.object(appmod, "load_device_aliases", return_value={"devices": {}, "updatedAt": None}),
            patch.object(cache, "read_router_topology_file", return_value={"clients": []}),
            patch.object(cache, "collect_neighbors", return_value=neighbors),
            patch.object(cache, "resolve_client_name", return_value={}) as resolver,
        ):
            cache.collect_topology()

        self.assertEqual(resolver.call_count, 2)
        self.assertEqual([call.args[0] for call in resolver.call_args_list], ["192.168.0.100", "192.168.0.101"])

    def test_cached_misses_allow_fair_resolution_across_cycles(self):
        cache = self.make_cache()
        neighbors = [
            {
                "ip": f"192.168.0.{100 + index}",
                "mac": f"aa:bb:cc:dd:ee:{index:02x}",
                "interface": "LAN",
                "online": True,
            }
            for index in range(10)
        ]
        with (
            patch.object(appmod, "default_gateway", return_value="192.168.0.1"),
            patch.object(appmod, "load_device_aliases", return_value={"devices": {}, "updatedAt": None}),
            patch.object(cache, "read_router_topology_file", return_value={"clients": []}),
            patch.object(cache, "collect_neighbors", return_value=neighbors),
            patch.object(appmod, "run_text", return_value="") as lookup,
            patch.object(appmod.shutil, "which", return_value=None),
        ):
            for _ in range(5):
                cache.collect_topology()

        self.assertEqual(lookup.call_count, 10)
        self.assertEqual(len(cache.name_cache), 10)

    def test_aliases_override_display_name_and_hints(self):
        cache = self.make_cache()
        aliases = {
            "devices": {
                "mac:aa:bb:cc:dd:ee:ff": {
                    "alias": "Living Room Console",
                    "location": "Media shelf",
                    "linkType": "wired",
                    "apId": "basement",
                }
            }
        }
        topology = self.collect(
            cache,
            {
                "clients": [
                    {
                        "name": "android",
                        "ip": "192.168.0.88",
                        "mac": "aa:bb:cc:dd:ee:ff",
                        "interface": "2.4G",
                    }
                ]
            },
            [],
            aliases=aliases,
        )

        row = next(client for client in topology["clients"] if client["ip"] == "192.168.0.88")
        self.assertEqual(row["displayName"], "Living Room Console")
        self.assertEqual(row["sourceName"], "android")
        self.assertEqual(row["location"], "Media shelf")
        self.assertEqual(row["linkType"], "wired")
        self.assertEqual(row["apId"], "basement")
        self.assertEqual(row["confidence"], "manual")

    def test_alias_save_can_clear_manual_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            alias_file = Path(tmp) / "aliases.json"
            with patch.object(appmod, "DEVICE_ALIASES_FILE", alias_file):
                appmod.save_device_alias(
                    {
                        "key": "ip:192.0.2.254",
                        "alias": "smoke-test",
                        "location": "lab",
                        "linkType": "wired",
                        "apId": "main",
                    }
                )
                cleared = appmod.save_device_alias(
                    {
                        "key": "ip:192.0.2.254",
                        "alias": "",
                        "location": "",
                        "linkType": "",
                        "apId": "",
                    }
                )
        self.assertNotIn("ip:192.0.2.254", cleared["devices"])


if __name__ == "__main__":
    unittest.main()
