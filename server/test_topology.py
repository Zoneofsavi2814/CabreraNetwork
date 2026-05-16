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
            pass

        def after_request(self, fn):
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
    fake_flask.request = types.SimpleNamespace(args={}, get_json=lambda *args, **kwargs: {})
    fake_flask.send_from_directory = lambda *args, **kwargs: None
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
