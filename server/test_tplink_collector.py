import os
import tempfile
import unittest
from pathlib import Path

from server import tplink_collector as tplink


APS = [
    {
        "id": "basement",
        "name": "ArcherAX3000Pro_Basement",
        "ip": "192.168.0.117",
        "mac": "98:03:8e:65:a4:ec",
        "location": "Basement",
    },
    {
        "id": "loft",
        "name": "ArcherAX3000Pro_Loft",
        "ip": "192.168.0.176",
        "mac": "98:03:8e:44:f7:e4",
        "location": "Loft",
    },
]


class FakeTplinkClient:
    calls = []

    def __init__(self, url, username, password, timeout=4.0):
        self.url = url
        self.username = username
        self.password = password
        self.timeout = timeout

    def login(self):
        self.calls.append(("login", self.url, self.username))

    def request(self, path, payload):
        self.calls.append((path, dict(payload)))
        if payload.get("operation") not in {"read", "loadDevice", "loadSpeed"}:
            raise AssertionError(f"unexpected router operation {payload}")
        if self.url != "http://192.168.0.1":
            return {}
        if path == "/admin/smart_network?form=game_accelerator" and payload["operation"] == "loadDevice":
            return {
                "clients": [
                    {
                        "deviceName": "raspberrypi5",
                        "mac": "2C-CF-67-28-72-9D",
                        "ip": "192.168.0.94",
                        "deviceTag": "wired",
                        "downloadSpeed": 30,
                        "uploadSpeed": 12,
                    },
                    {
                        "deviceName": "Anns-MacBook",
                        "mac": "0E-5C-E5-70-C3-5A",
                        "ip": "192.168.0.42",
                        "deviceTag": "5G",
                        "agent_mac": "98-03-8E-44-F7-E4",
                    },
                    {
                        "deviceName": "ArcherAX3000Pro_Loft",
                        "mac": "98-03-8E-44-F7-E4",
                        "ip": "192.168.0.176",
                        "deviceTag": "wired",
                    },
                ]
            }
        if path == "/admin/smart_network?form=game_accelerator" and payload["operation"] == "loadSpeed":
            return {
                "clients": [
                    {"mac": "0E-5C-E5-70-C3-5A", "downloadSpeed": 55, "uploadSpeed": 8},
                ]
            }
        return {}


class TplinkCollectorTests(unittest.TestCase):
    def test_rejects_group_or_world_readable_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.ini"
            path.write_text("url=http://192.168.0.1\nusername=admin\npassword=secret\n")
            os.chmod(path, 0o644)

            with self.assertRaises(tplink.RouterCredentialsError):
                tplink.read_router_credentials(path)

    def test_collector_uses_read_only_endpoints_and_normalizes_topology(self):
        FakeTplinkClient.calls = []
        with tempfile.TemporaryDirectory() as tmp:
            creds = Path(tmp) / "router.ini"
            cache = Path(tmp) / "router-cache.json"
            creds.write_text("url=http://192.168.0.1\nusername=admin\npassword=secret\n")
            os.chmod(creds, 0o600)
            collector = tplink.TplinkTopologyCollector(
                creds,
                cache,
                router_name="Archer BE400",
                router_ip="192.168.0.1",
                aps=APS,
                client_factory=FakeTplinkClient,
            )

            topology, status = collector.poll()
            self.assertTrue(cache.exists())

        self.assertEqual(status["status"], "ok")
        self.assertIn("gameDevices", status["endpoints"])
        operations = [call[1]["operation"] for call in FakeTplinkClient.calls if isinstance(call[0], str) and call[0].startswith("/")]
        self.assertEqual(set(operations), {"read", "loadDevice", "loadSpeed"})

        macbook = next(row for row in topology["clients"] if row["mac"] == "0e:5c:e5:70:c3:5a")
        self.assertEqual(macbook["interface"], "5g")
        self.assertEqual(macbook["apId"], "loft")
        self.assertEqual(macbook["rxKbps"], 55)
        self.assertEqual(macbook["txKbps"], 8)

        pi5 = next(row for row in topology["clients"] if row["mac"] == "2c:cf:67:28:72:9d")
        self.assertEqual(pi5["interface"], "wired")

        loft = next(row for row in topology["aps"] if row["apId"] == "loft")
        self.assertTrue(loft["online"])
        self.assertEqual(loft["location"], "Loft")

    def test_direct_ap_poll_assigns_clients_to_actual_ap(self):
        class FakeApAwareClient(FakeTplinkClient):
            calls = []

            def login(self):
                self.calls.append(("login", self.url, self.username))

            def request(self, path, payload):
                self.calls.append((path, dict(payload), self.url))
                if path != "/admin/smart_network?form=game_accelerator":
                    return {}
                if payload["operation"] == "loadDevice" and self.url == "http://192.168.0.1":
                    return {
                        "clients": [
                            {
                                "deviceName": "ChristohersMini",
                                "mac": "D0-11-E5-F2-0E-5C",
                                "ip": "192.168.0.175",
                                "deviceTag": "wired",
                            }
                        ]
                    }
                if payload["operation"] == "loadDevice" and self.url == "http://192.168.0.176":
                    return {
                        "clients": [
                            {
                                "deviceName": "ChristohersMini",
                                "key": "D011E5F20E5C",
                                "ip": "192.168.0.175",
                                "deviceTag": "UNKNOW",
                                "signal": -32,
                            }
                        ]
                    }
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            creds = Path(tmp) / "router.ini"
            cache = Path(tmp) / "router-cache.json"
            creds.write_text("url=http://192.168.0.1\nusername=admin\npassword=secret\n")
            os.chmod(creds, 0o600)
            collector = tplink.TplinkTopologyCollector(
                creds,
                cache,
                router_name="Archer BE400",
                router_ip="192.168.0.1",
                aps=APS,
                client_factory=FakeApAwareClient,
            )

            topology, status = collector.poll()

        mini = next(row for row in topology["clients"] if row["mac"] == "d0:11:e5:f2:0e:5c")
        self.assertEqual(mini["apId"], "loft")
        self.assertEqual(mini["interface"], "wifi")
        self.assertEqual(status["apClientCounts"]["loft"], 1)
        self.assertIn("loft:gameDevices", status["endpoints"])

    def test_router_docs_normalize_wifi_without_inventing_ap(self):
        topology = tplink.normalize_router_topology(
            {
                "gameDevices": {
                    "clients": [
                        {
                            "deviceName": "AmazonPlug164L",
                            "mac": "A0-D2-B1-14-C4-F6",
                            "ip": "192.168.0.225",
                            "deviceTag": "2.4G",
                        }
                    ]
                }
            },
            router_name="Archer BE400",
            router_ip="192.168.0.1",
            aps=APS,
        )

        row = topology["clients"][0]
        self.assertEqual(row["interface"], "2.4g")
        self.assertEqual(row["apId"], "")

    def test_compact_ap_mac_key_and_signal_normalize_as_wifi(self):
        topology = tplink.normalize_router_topology(
            {
                "gameDevices": {
                    "clients": [
                        {
                            "deviceName": "2DA-US-SGB0161A",
                            "key": "446FF813AE2F",
                            "ip": "192.168.0.177",
                            "deviceTag": "UNKNOW",
                            "signal": -47,
                        }
                    ]
                }
            },
            router_name="ArcherAX3000Pro_Loft",
            router_ip="192.168.0.176",
            aps=APS,
            default_ap_id="loft",
        )

        row = topology["clients"][0]
        self.assertEqual(row["mac"], "44:6f:f8:13:ae:2f")
        self.assertEqual(row["interface"], "wifi")
        self.assertEqual(row["apId"], "loft")

    def test_location_words_do_not_make_clients_into_ap_nodes(self):
        topology = tplink.normalize_router_topology(
            {
                "gameDevices": {
                    "clients": [
                        {
                            "deviceName": "Loft-TV",
                            "mac": "0C-91-60-A6-08-88",
                            "ip": "192.168.0.233",
                            "deviceTag": "wired",
                        }
                    ]
                }
            },
            router_name="Archer BE400",
            router_ip="192.168.0.1",
            aps=APS,
        )

        self.assertEqual(topology["aps"], [])
        self.assertEqual(topology["clients"][0]["name"], "Loft-TV")

    def test_encryptor_matches_router_static_sequence_behavior(self):
        encryptor = tplink.TplinkEncryptor(
            "hash",
            100,
            ["b" * 128, "10001"],
        )
        encryptor._aes_encrypt = lambda payload: "abcd"  # type: ignore[method-assign]

        first = encryptor.encrypt("one=1", with_aes_key=False, token_valid=True)
        second = encryptor.encrypt("two=2", with_aes_key=False, token_valid=True)

        self.assertEqual(encryptor.sequence, 100)
        self.assertEqual(first["sign"], second["sign"])

    def test_raw_be400_certification_strings_enable_sha256_and_oaep(self):
        certs = tplink.certification_set({"certification": ["US FCC", "SG CLS L1 STAGE2"]})

        self.assertTrue(certs & tplink.SHA256_CERTS)
        self.assertTrue(certs & tplink.REPLACE_HASH_CERTS)
        self.assertTrue(certs & tplink.RSA_OAEP_CERTS)


if __name__ == "__main__":
    unittest.main()
