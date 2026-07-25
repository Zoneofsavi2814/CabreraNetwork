#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

sys.path.insert(0, "/opt/pi5-critical-alerts")

import pi5_alerts  # noqa: E402


CHECK_ID = "k3s-storage-growth"


def main() -> int:
    skipped = {
        token.strip()
        for token in os.environ.get("PI5_ALERTS_SKIP_CHECKS", "").split(",")
        if token.strip() and token.strip() != CHECK_ID
    }
    os.environ["PI5_ALERTS_SKIP_CHECKS"] = ",".join(sorted(skipped))

    check = next((item for item in pi5_alerts.check_config() if item.get("id") == CHECK_ID), None)
    if check is None:
        print(f"{CHECK_ID}: check configuration unavailable", file=sys.stderr)
        return 2

    result = pi5_alerts.run_check(check)
    cadences = [{"id": "pi5-storage", "label": "Pi5 hourly storage monitoring", "checks": [result]}]
    snapshot = {
        "updatedAt": pi5_alerts.datetime.now().isoformat(),
        "cadences": cadences,
        "summary": pi5_alerts.summarize_operations(cadences),
    }
    notify_env = dict(os.environ)
    notify_env["PI5_ALERTS_NOTIFY_STATE"] = str(
        pi5_alerts.STATE_DIR / "storage-notification-state.json"
    )
    notify_env["PI5_ALERTS_NOTIFY_MORNING_ENABLED"] = "0"
    deliveries = pi5_alerts.NotificationManager(env=notify_env).dispatch_operation_notifications(snapshot)
    failed_delivery = any(not item.get("delivered") for item in deliveries)
    print(
        f"{CHECK_ID}: {result.get('status', 'unknown')} - {result.get('message', 'no result')}; "
        f"delivery_failures={int(failed_delivery)}",
        flush=True,
    )
    if failed_delivery:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
