import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "pi5-balanced-efficiency" / "pi5_storage_growth.py"


class StorageGrowthWrapperTests(unittest.TestCase):
    def load_wrapper(self, deliveries):
        fake = types.ModuleType("pi5_alerts")
        fake.STATE_DIR = Path("/var/lib/pi5-critical-alerts")
        fake.datetime = datetime
        fake.check_config = lambda: [{"id": "k3s-storage-growth"}]
        fake.run_check = lambda check: {
            "id": check["id"],
            "status": "fail",
            "message": "test storage failure",
        }
        fake.summarize_operations = lambda cadences: {"fail": 1, "warn": 0, "ok": 0}

        class NotificationManager:
            last_env = None

            def __init__(self, env=None):
                type(self).last_env = env

            def dispatch_operation_notifications(self, snapshot):
                self.snapshot = snapshot
                return deliveries

        fake.NotificationManager = NotificationManager
        original = sys.modules.get("pi5_alerts")
        sys.modules["pi5_alerts"] = fake
        try:
            spec = importlib.util.spec_from_file_location("rp5_storage_growth_test_target", SCRIPT)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
        finally:
            if original is None:
                sys.modules.pop("pi5_alerts", None)
            else:
                sys.modules["pi5_alerts"] = original
        return module, NotificationManager

    def test_observed_failure_uses_isolated_state_without_failing_unit(self):
        module, notifier = self.load_wrapper([])
        self.assertEqual(module.main(), 0)
        self.assertEqual(notifier.last_env["PI5_ALERTS_NOTIFY_MORNING_ENABLED"], "0")
        self.assertTrue(notifier.last_env["PI5_ALERTS_NOTIFY_STATE"].endswith("storage-notification-state.json"))

    def test_delivery_failure_is_a_unit_failure(self):
        module, _ = self.load_wrapper([{"delivered": False}])
        self.assertEqual(module.main(), 3)


if __name__ == "__main__":
    unittest.main()
