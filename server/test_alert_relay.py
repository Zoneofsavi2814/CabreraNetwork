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

    def queue(
        self,
        message_id,
        *,
        published_at=1783639000,
        title="Subject",
        message="Body",
        tags=None,
        priority=5,
    ):
        with self.relay.conn:
            self.relay.conn.execute(
                """
                insert into outbox(id,published_at,title,message,priority,tags,received_at,next_attempt_at)
                values(?,?,?,?,?,?,?,0)
                """,
                (
                    message_id,
                    published_at,
                    title,
                    message,
                    priority,
                    json.dumps(tags or []),
                    "2026-07-09T17:00:00-06:00",
                ),
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
        timeout = urlerror.HTTPError("https://provider.invalid", 522, "timeout", {}, None)
        self.addCleanup(timeout.close)
        responses = [
            FakeResponse(self.message()),
            timeout,
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

    def test_429_opens_durable_provider_circuit_and_blocks_requests_until_retry_after(self):
        self.queue("oldest", published_at=1)
        self.queue("newer", published_at=2)
        throttled = urlerror.HTTPError(
            "https://provider.invalid",
            429,
            "Too Many Requests",
            {"Retry-After": "1200"},
            None,
        )
        self.addCleanup(throttled.close)

        with (
            mock.patch.dict(
                os.environ,
                {
                    "CABRERA_ALERT_RELAY_PROVIDER_RETRY_BASE_SECONDS": "300",
                    "CABRERA_ALERT_RELAY_PROVIDER_RETRY_MAX_SECONDS": "300",
                },
            ),
            mock.patch.object(relay_module.time, "time", return_value=1000),
            mock.patch.object(self.relay, "deliver", side_effect=throttled) as deliver,
        ):
            self.assertEqual(self.relay.drain(), (0, 1))
            status = self.relay.status()

        self.assertEqual(deliver.call_count, 1)
        self.assertTrue(status["providerCircuitOpen"])
        self.assertIsNotNone(status["providerRetryAt"])
        self.assertEqual(status["providerConsecutiveFailures"], 1)

        db_path = self.relay.db_path
        self.relay.close()
        self.relay = relay_module.AlertRelay(db_path)
        self.addCleanup(self.relay.close)
        with (
            mock.patch.object(relay_module.time, "time", return_value=2199),
            mock.patch.object(self.relay, "deliver") as deliver_before_expiry,
        ):
            self.assertEqual(self.relay.drain(), (0, 0))
            self.assertTrue(self.relay.status()["providerCircuitOpen"])

        deliver_before_expiry.assert_not_called()
        self.assertEqual(self.relay.status()["pending"], 2)

    def test_circuit_recovery_retries_oldest_and_clears_provider_state(self):
        self.queue("oldest", published_at=1)
        self.queue("newer", published_at=2)
        throttled = urlerror.HTTPError(
            "https://provider.invalid",
            429,
            "Too Many Requests",
            {"Retry-After": "1200"},
            None,
        )
        self.addCleanup(throttled.close)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "CABRERA_ALERT_RELAY_PROVIDER_RETRY_BASE_SECONDS": "300",
                    "CABRERA_ALERT_RELAY_PROVIDER_RETRY_MAX_SECONDS": "300",
                },
            ),
            mock.patch.object(relay_module.time, "time", return_value=1000),
            mock.patch.object(self.relay, "deliver", side_effect=throttled),
        ):
            self.assertEqual(self.relay.drain(), (0, 1))

        with (
            mock.patch.object(relay_module.time, "time", return_value=2201),
            mock.patch.object(self.relay, "deliver") as recovered_delivery,
        ):
            self.assertEqual(self.relay.drain(), (1, 0))
            status = self.relay.status()

        self.assertEqual(recovered_delivery.call_args.args[0]["id"], "oldest")
        self.assertFalse(status["providerCircuitOpen"])
        self.assertIsNone(status["providerRetryAt"])
        self.assertEqual(status["providerConsecutiveFailures"], 0)
        self.assertEqual(status["pending"], 1)
        self.assertEqual(status["delivered"], 1)

    def test_non_finite_retry_after_cannot_permanently_break_status(self):
        throttled = urlerror.HTTPError(
            "https://provider.invalid",
            429,
            "Too Many Requests",
            {"Retry-After": "1e309"},
            None,
        )
        self.addCleanup(throttled.close)
        self.assertIsNone(relay_module.retry_after_seconds(throttled, now=1000))

        with self.relay.conn:
            self.relay.set_meta("provider_retry_at", "inf")
        status = self.relay.status()
        self.assertFalse(status["providerCircuitOpen"])
        self.assertIsNone(status["providerRetryAt"])

    def test_compaction_keeps_latest_duplicate_and_separates_discarded_from_delivered(self):
        self.queue(
            "duplicate-old",
            published_at=1,
            title="Repeated critical alert",
            message="old detail",
            tags=["PI4-NOC", "critical"],
        )
        self.queue(
            "duplicate-latest",
            published_at=2,
            title="Repeated critical alert",
            message="latest detail",
            tags=["critical", "pi4-noc", "critical"],
            priority=3,
        )
        self.queue(
            "suppressed",
            published_at=3,
            title="Relay-only health detail",
            tags=["NO-EMAIL-RELAY"],
        )

        self.assertEqual(self.relay.compact_pending(), (1, 1))
        survivor = self.relay.conn.execute(
            "select id, message, priority, coalesced_count from outbox where delivered_at is null"
        ).fetchone()
        self.assertEqual(survivor["id"], "duplicate-latest")
        self.assertEqual(survivor["message"], "latest detail")
        self.assertEqual(survivor["priority"], 5)
        self.assertEqual(survivor["coalesced_count"], 1)
        status = self.relay.status()
        self.assertEqual(status["pending"], 1)
        self.assertEqual(status["delivered"], 0)
        self.assertEqual(status["discarded"], 2)

        with mock.patch.object(self.relay, "deliver") as delivery:
            self.assertEqual(self.relay.drain(), (1, 0))

        self.assertEqual(delivery.call_args.args[0]["id"], "duplicate-latest")
        status = self.relay.status()
        self.assertEqual(status["pending"], 0)
        self.assertEqual(status["delivered"], 1)
        self.assertEqual(status["discarded"], 2)

    def test_self_health_message_advances_cursor_without_queuing_email(self):
        item = json.loads(self.message("self-health"))
        item.update(
            {
                "title": "🚨 Pi5 · Durable email relay outbox is failing",
                "tags": ["pi5-critical-alerts", "critical"],
            }
        )

        with mock.patch.object(self.relay, "poll", return_value=[item]):
            self.assertEqual(self.relay.collect(), (0, 0, 1))

        self.assertEqual(self.relay.meta("cursor"), "self-health")
        self.assertEqual(self.relay.status()["pending"], 0)
        self.assertEqual(self.relay.status()["discarded"], 1)
        row = self.relay.conn.execute("select delivered_at, last_error from outbox").fetchone()
        self.assertIsNotNone(row["delivered_at"])
        self.assertIn("relay self-health", row["last_error"])

    def test_permanently_rejected_row_is_discarded_without_blocking_fifo(self):
        self.queue("poison", published_at=1)
        self.queue("valid", published_at=2)
        rejected = urlerror.HTTPError(
            "https://provider.invalid",
            413,
            "Content Too Large",
            {},
            None,
        )
        self.addCleanup(rejected.close)

        with mock.patch.object(self.relay, "deliver", side_effect=rejected):
            self.assertEqual(self.relay.drain(), (0, 1))

        status = self.relay.status()
        self.assertFalse(status["providerCircuitOpen"])
        self.assertEqual(status["providerConsecutiveFailures"], 0)
        self.assertEqual(status["pending"], 1)
        self.assertEqual(status["discarded"], 1)

        with mock.patch.object(self.relay, "deliver") as delivery:
            self.assertEqual(self.relay.drain(), (1, 0))

        self.assertEqual(delivery.call_args.args[0]["id"], "valid")

    def test_run_result_reports_coalesced_and_suppressed_counts(self):
        duplicate_old = json.loads(self.message("duplicate-old"))
        duplicate_old.update({"title": "Repeated alert", "message": "old", "time": 1})
        duplicate_latest = json.loads(self.message("duplicate-latest"))
        duplicate_latest.update({"title": "Repeated alert", "message": "latest", "time": 2})
        self_health = json.loads(self.message("self-health"))
        self_health.update(
            {
                "title": "Pi5 · Durable email relay outbox is failing",
                "tags": ["pi5-critical-alerts", "critical"],
                "time": 3,
            }
        )

        with (
            mock.patch.object(
                self.relay,
                "poll",
                return_value=[duplicate_old, duplicate_latest, self_health],
            ),
            mock.patch.object(self.relay, "deliver"),
        ):
            result = self.relay.run_once()

        self.assertEqual(result["collected"], 1)
        self.assertEqual(result["coalescedThisRun"], 1)
        self.assertEqual(result["suppressedThisRun"], 1)
        self.assertEqual(result["deliveredThisRun"], 1)
        self.assertEqual(result["discarded"], 2)
        self.assertEqual(self.relay.conn.execute("select count(*) from outbox").fetchone()[0], 3)

    def test_collect_failure_drains_existing_row_before_propagating(self):
        self.queue("already-durable", published_at=1)

        with (
            mock.patch.object(self.relay, "poll", side_effect=RuntimeError("ntfy unavailable")),
            mock.patch.object(self.relay, "deliver") as delivery,
        ):
            with self.assertRaisesRegex(RuntimeError, "ntfy unavailable"):
                self.relay.run_once()

        self.assertEqual(delivery.call_args.args[0]["id"], "already-durable")
        self.assertEqual(self.relay.status()["pending"], 0)
        self.assertEqual(self.relay.status()["delivered"], 1)

    def test_overlapping_run_is_rejected_before_poll_or_delivery(self):
        competing = relay_module.AlertRelay(self.relay.db_path)
        self.addCleanup(competing.close)

        with self.relay.exclusive_run():
            with mock.patch.object(competing, "poll") as poll:
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    competing.run_once()

        poll.assert_not_called()

    def test_form_delivery_renders_structured_email_rows(self):
        from urllib.parse import parse_qs

        self.relay.set_meta("initialized", "1")
        self.relay.conn.commit()
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured.setdefault("bodies", []).append(req.data.decode() if req.data else "")
            return FakeResponse(self.message() if req.data is None else '{"success":"true"}')

        with mock.patch.object(relay_module.urlrequest, "urlopen", side_effect=fake_urlopen):
            result = self.relay.run_once()

        self.assertEqual(result["deliveredThisRun"], 1)
        form_body = captured["bodies"][-1]
        parsed = parse_qs(form_body, keep_blank_values=True)
        nbsp = relay_module.NBSP

        self.assertEqual(parsed["_subject"], ["🚨 Pi4 critical alert"])
        self.assertEqual(parsed[f"🚨{nbsp}Alert"], ["Pi4 critical alert"])
        self.assertEqual(parsed[f"📝{nbsp}Details"], ["A service failed"])
        self.assertIn("Critical (priority 5)", parsed[f"📟{nbsp}Severity"][0])
        self.assertIn(f"🕐{nbsp}Received", parsed)
        self.assertIn("message new-message", parsed[f"📨{nbsp}Trace"][0])
        # Legacy plumbing fields must not appear as visible email rows.
        for hidden in ("message", "name", "email", "subject", "source", "severity", "sentAt", "messageId"):
            self.assertNotIn(hidden, parsed)

    def test_subjects_with_status_icons_are_not_double_prefixed(self):
        self.assertEqual(
            relay_module.pretty_subject("🔔", "🟡 Cabrera Network · Morning report — 5 warnings to review"),
            "🟡 Cabrera Network · Morning report — 5 warnings to review",
        )
        self.assertEqual(
            relay_module.pretty_subject("🚨", "🚨 Cabrera Network · GRID MCP on Pi4 is failing"),
            "🚨 Cabrera Network · GRID MCP on Pi4 is failing",
        )
        self.assertEqual(relay_module.pretty_subject("🚨", "Pi4 critical alert"), "🚨 Pi4 critical alert")


if __name__ == "__main__":
    unittest.main()
