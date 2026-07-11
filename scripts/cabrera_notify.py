#!/usr/bin/env python3
"""Shared notification channels for Cabrera Network host monitors."""

from __future__ import annotations

import json
import re
from datetime import datetime
from urllib import parse as urlparse
from urllib import request as urlrequest


PRIVATE_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")


def ntfy_configured(env: dict[str, str], prefix: str) -> bool:
    return bool(env.get(f"{prefix}_NTFY_URL", "").strip())


def _ntfy_target(env: dict[str, str], prefix: str) -> tuple[str, str]:
    configured_url = env.get(f"{prefix}_NTFY_URL", "").strip()
    parts = urlparse.urlsplit(configured_url)
    topic = parts.path.strip("/")
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or "/" in topic
        or not PRIVATE_TOPIC_RE.fullmatch(topic)
    ):
        raise ValueError(f"{prefix}_NTFY_URL must be a private HTTPS topic URL")
    return urlparse.urlunsplit((parts.scheme, parts.netloc, "/", "", "")), topic


def send_ntfy(
    env: dict[str, str],
    prefix: str,
    *,
    source: str,
    subject: str,
    body: str,
    severity: str,
) -> None:
    publish_url, topic = _ntfy_target(env, prefix)
    priority = 5 if severity == "critical" else 4 if severity in {"warning", "warn"} else 3
    payload: dict[str, object] = {
        "topic": topic,
        "title": subject[:200],
        "message": body[:3000],
        "priority": priority,
        "tags": [source, severity],
    }
    click_url = env.get(f"{prefix}_NTFY_CLICK_URL", "").strip()
    if click_url:
        click_parts = urlparse.urlsplit(click_url)
        if click_parts.scheme not in {"http", "https"} or not click_parts.netloc:
            raise ValueError(f"{prefix}_NTFY_CLICK_URL must be HTTP or HTTPS")
        payload["click"] = click_url

    try:
        timeout = float(env.get(f"{prefix}_NTFY_TIMEOUT_SECONDS", "10"))
    except ValueError:
        timeout = 10.0
    timeout = max(1.0, min(timeout, 30.0))
    request = urlrequest.Request(
        publish_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"{source}/notifications",
        },
        method="POST",
    )
    with urlrequest.urlopen(request, timeout=timeout) as response:
        raw = response.read(65536)
        status = response.getcode() if hasattr(response, "getcode") else 200
        if status >= 400:
            raise RuntimeError(f"ntfy returned HTTP {status}")
        try:
            result = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("ntfy returned invalid JSON") from exc
        if not isinstance(result, dict) or not result.get("id"):
            raise RuntimeError("ntfy did not acknowledge the message")


def delivery_result(*, delivered: bool, severity: str, started: float, attempts: int, **extra: object) -> dict[str, object]:
    import time

    result: dict[str, object] = {
        "delivered": delivered,
        "attempts": attempts,
        "latencyMs": max(0, round((time.monotonic() - started) * 1000)),
        "completedAt": datetime.now().isoformat(),
        "severity": severity,
    }
    result.update(extra)
    return result
