import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "cabrera_notify.py"
SPEC = importlib.util.spec_from_file_location("cabrera_notify", MODULE_PATH)
cabrera_notify = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cabrera_notify)


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return json.dumps({"id": "message-id"}).encode()

    def getcode(self):
        return 200


class CabreraNotifyTests(unittest.TestCase):
    def test_ntfy_publish_uses_root_endpoint_and_keeps_topic_in_payload(self):
        env = {
            "TEST_NOTIFY_NTFY_URL": "https://ntfy.sh/abcdefghijklmnopqrstuvwxyz012345",
            "TEST_NOTIFY_NTFY_CLICK_URL": "http://192.168.0.94/",
        }
        with mock.patch.object(cabrera_notify.urlrequest, "urlopen", return_value=FakeResponse()) as opened:
            cabrera_notify.send_ntfy(
                env,
                "TEST_NOTIFY",
                source="host-monitor",
                subject="Critical alert",
                body="A service failed",
                severity="critical",
            )

        request = opened.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://ntfy.sh/")
        self.assertEqual(payload["topic"], "abcdefghijklmnopqrstuvwxyz012345")
        self.assertEqual(payload["priority"], 5)
        self.assertEqual(payload["click"], "http://192.168.0.94/")

    def test_ntfy_rejects_short_or_non_https_topic_urls(self):
        for value in ("http://ntfy.sh/abcdefghijklmnopqrstuvwxyz", "https://ntfy.sh/short"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cabrera_notify.send_ntfy(
                    {"TEST_NOTIFY_NTFY_URL": value},
                    "TEST_NOTIFY",
                    source="host-monitor",
                    subject="Alert",
                    body="Body",
                    severity="critical",
                )


if __name__ == "__main__":
    unittest.main()
