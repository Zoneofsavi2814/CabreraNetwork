import unittest
from datetime import datetime, timedelta, timezone

from scripts.rp5_acceptance_report import evaluate


LIMITS = {
    "average_cpu_pct": 2.0,
    "median_temperature_c": 56.0,
    "p95_temperature_c": 60.0,
    "peak_temperature_c": 65.0,
    "endpoint_p95_ms": 28.0,
}


class Rp5AcceptanceReportTests(unittest.TestCase):
    def setUp(self):
        self.since = datetime(2026, 7, 14, 20, tzinfo=timezone.utc)

    def test_complete_passing_window(self):
        samples = [self.sample(index, cpu="2%", temperature=55.0) for index in range(12)]

        report = evaluate(
            samples,
            since=self.since,
            hours=1,
            interval_minutes=5,
            minimum_coverage=0.9,
            limits=LIMITS,
        )

        self.assertTrue(report["complete"])
        self.assertTrue(report["passed"])
        self.assertEqual(report["metrics"]["average_cpu_pct"], 2.0)
        self.assertEqual(report["metrics"]["endpoints"]["coinbot"]["p95_latency_ms"], 5.0)

    def test_target_failure_is_reported(self):
        samples = [self.sample(index, temperature=66.0, status=500) for index in range(12)]

        report = evaluate(
            samples,
            since=self.since,
            hours=1,
            interval_minutes=5,
            minimum_coverage=0.9,
            limits=LIMITS,
        )

        self.assertTrue(report["complete"])
        self.assertFalse(report["passed"])
        self.assertFalse(report["checks"]["peak_temperature"])
        self.assertFalse(report["checks"]["no_endpoint_failures"])

    def test_incomplete_window_does_not_pass(self):
        samples = [self.sample(index) for index in range(2)]

        report = evaluate(
            samples,
            since=self.since,
            hours=1,
            interval_minutes=5,
            minimum_coverage=0.9,
            limits=LIMITS,
        )

        self.assertFalse(report["complete"])
        self.assertFalse(report["passed"])

    def sample(
        self,
        index: int,
        *,
        cpu: str = "1%",
        temperature: float = 55.0,
        status: int = 200,
    ):
        return {
            "timestamp": (self.since + timedelta(minutes=5 * index)).isoformat(),
            "hardware": {"temperature": f"temp={temperature}'C", "throttled": "throttled=0x0"},
            "k3sNode": {"cpuPct": cpu},
            "endpoints": {"coinbot": {"status": status, "latencyMs": 5}},
        }


if __name__ == "__main__":
    unittest.main()
