import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib import error as urlerror


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "cabrera-alert-relay.py"
SPEC = importlib.util.spec_from_file_location("cabrera_alert_relay", MODULE_PATH)
relay_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(relay_module)


class FakeResponse:
    def __init__(self, body: str, status: int = 200):
        self.body = body.encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.body

    def getcode(self):
        return self.status


class AlertRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {
                "PI5_ALERTS_NOTIFY_NTFY_URL": "https://ntfy.sh/abcdefghijklmnopqrstuvwxyz012345",
                "PI5_ALERTS_NOTIFY_WEBHOOK_URL": "https://formsubmit.co/ajax/example@example.com",
                "PI5_ALERTS_NOTIFY_WEBHOOK_FORMAT": "form",
                "CABRERA_ALERT_RELAY_MAX_ATTEMPTS_PER_RUN": "1",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.relay = relay_module.AlertRelay(Path(self.temp.name) / "outbox.sqlite3")
        self.addCleanup(self.relay.close)

    @staticmethod
    def message(message_id="new-message"):
        return json.dumps(
            {
                "id": message_id,
                "event": "message",
                "time": 1783639000,
                "title": "Pi4 critical alert",
                "message": "A service failed",
                "priority": 5,
                "tags": ["pi4-noc", "critical"],
            }
        )

    def test_initialize_records_cursor_without_replaying_history(self):
        with mock.patch.object(relay_module.urlrequest, "urlopen", return_value=FakeResponse(self.message("historical"))):
            status = self.relay.initialize()

        self.assertTrue(status["initialized"])
        self.assertEqual(status["pending"], 0)
        self.assertEqual(self.relay.meta("cursor"), "historical")

    def test_failed_email_stays_queued_for_later_retry(self):
        self.relay.set_meta("initialized", "1")
        self.relay.conn.commit()
        responses = [
            FakeResponse(self.message()),
            urlerror.HTTPError("https://provider.invalid", 522, "timeout", {}, None),
        ]
        with mock.patch.object(relay_module.urlrequest, "urlopen", side_effect=responses):
            result = self.relay.run_once()

        self.assertEqual(result["collected"], 1)
        self.assertEqual(result["failedThisRun"], 1)
        self.assertEqual(result["pending"], 1)
        row = self.relay.conn.execute("select attempts, delivered_at, last_error from outbox").fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertIsNone(row["delivered_at"])
        self.assertNotIn("provider.invalid", row["last_error"])

    def test_successful_retry_marks_message_delivered(self):
        with self.relay.conn:
            self.relay.conn.execute(
                "insert into outbox(id,title,message,tags,received_at,next_attempt_at) values(?,?,?,?,?,0)",
                ("queued", "Subject", "Body", "[]", "2026-07-09T17:00:00-06:00"),
            )
        with mock.patch.object(
            relay_module.urlrequest,
            "urlopen",
            return_value=FakeResponse('{"success":"true"}'),
        ):
            delivered, failed = self.relay.drain()

        self.assertEqual((delivered, failed), (1, 0))
        self.assertEqual(self.relay.status()["pending"], 0)
        self.assertEqual(self.relay.status()["delivered"], 1)


if __name__ == "__main__":
    unittest.main()
