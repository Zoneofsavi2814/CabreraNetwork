#!/usr/bin/env python3
"""Durably relay ntfy messages to the configured email webhook."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


APP_NAME = "cabrera-alert-relay"
DEFAULT_DB = "/var/lib/cabrera-alert-relay/outbox.sqlite3"
PRIVATE_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
URL_RE = re.compile(r"https?://[^\s<>\"']+")
SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret|apikey|api_key|authorization)([=: ]+)(\S+)")


def redact(value: str) -> str:
    return URL_RE.sub("<redacted-url>", SECRET_RE.sub(r"\1\2<redacted>", value or ""))


def iso_now() -> str:
    return datetime.now().astimezone().isoformat()


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


class AlertRelay:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or os.environ.get("CABRERA_ALERT_RELAY_DB", DEFAULT_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=WAL")
        self.conn.execute("pragma synchronous=FULL")
        self._migrate()
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            create table if not exists meta (
                key text primary key,
                value text not null
            );
            create table if not exists outbox (
                id text primary key,
                published_at integer,
                title text not null,
                message text not null,
                priority integer,
                tags text not null default '[]',
                received_at text not null,
                attempts integer not null default 0,
                next_attempt_at real not null default 0,
                delivered_at text,
                last_error text
            );
            create index if not exists outbox_due
                on outbox(delivered_at, next_attempt_at);
            """
        )
        self.conn.commit()

    def meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("select value from meta where key = ?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "insert into meta(key, value) values(?, ?) on conflict(key) do update set value=excluded.value",
            (key, value),
        )

    @staticmethod
    def topic_url() -> tuple[str, str]:
        configured = os.environ.get("PI5_ALERTS_NOTIFY_NTFY_URL", "").strip()
        parts = urlparse.urlsplit(configured)
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
            raise ValueError("PI5_ALERTS_NOTIFY_NTFY_URL must be a private HTTPS topic URL")
        base = urlparse.urlunsplit((parts.scheme, parts.netloc, f"/{topic}/json", "", ""))
        return base, topic

    def poll(self, since: str) -> list[dict]:
        base, _topic = self.topic_url()
        query = urlparse.urlencode({"poll": "1", "since": since})
        request = urlrequest.Request(
            f"{base}?{query}",
            headers={"Accept": "application/x-ndjson", "User-Agent": f"{APP_NAME}/1"},
        )
        timeout = env_float("CABRERA_ALERT_RELAY_NTFY_TIMEOUT_SECONDS", 15, 2, 30)
        with urlrequest.urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024).decode("utf-8", "replace")
        messages = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict) and item.get("event") == "message" and item.get("id"):
                messages.append(item)
        return messages

    def initialize(self) -> dict:
        messages = self.poll("all")
        with self.conn:
            if messages:
                self.set_meta("cursor", str(messages[-1]["id"]))
            self.set_meta("initialized", "1")
            self.set_meta("last_poll_at", iso_now())
        return self.status()

    def collect(self) -> int:
        cursor = self.meta("cursor")
        since = cursor or ("2m" if self.meta("initialized") == "1" else "all")
        messages = self.poll(since)
        inserted = 0
        with self.conn:
            for item in messages:
                result = self.conn.execute(
                    """
                    insert or ignore into outbox(
                        id, published_at, title, message, priority, tags, received_at
                    ) values(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(item["id"]),
                        int(item.get("time") or 0),
                        str(item.get("title") or "Cabrera Network alert")[:500],
                        str(item.get("message") or "")[:12000],
                        int(item.get("priority") or 3),
                        json.dumps(item.get("tags") or []),
                        iso_now(),
                    ),
                )
                inserted += max(0, result.rowcount)
                self.set_meta("cursor", str(item["id"]))
            self.set_meta("initialized", "1")
            self.set_meta("last_poll_at", iso_now())
            self.set_meta("last_collect_count", str(inserted))
        return inserted

    @staticmethod
    def webhook_config() -> tuple[str, str]:
        url = os.environ.get("PI5_ALERTS_NOTIFY_WEBHOOK_URL", "").strip()
        parts = urlparse.urlsplit(url)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("PI5_ALERTS_NOTIFY_WEBHOOK_URL must be HTTPS")
        mode = os.environ.get("PI5_ALERTS_NOTIFY_WEBHOOK_FORMAT", "json").strip().lower()
        return url, mode

    def deliver(self, row: sqlite3.Row) -> None:
        url, mode = self.webhook_config()
        sent_at = iso_now()
        payload = {
            "source": APP_NAME,
            "severity": "critical" if int(row["priority"] or 0) >= 5 else "info",
            "subject": row["title"],
            "body": row["message"],
            "sentAt": sent_at,
            "messageId": row["id"],
        }
        headers = {"Accept": "application/json", "User-Agent": f"{APP_NAME}/1"}
        referer = os.environ.get("PI5_ALERTS_NOTIFY_WEBHOOK_REFERER", "").strip()
        if referer:
            headers["Referer"] = referer
        if mode in {"form", "form-urlencoded", "x-www-form-urlencoded"}:
            form = {
                "name": os.environ.get("PI5_ALERTS_NOTIFY_WEBHOOK_NAME", "Cabrera Network Alerts"),
                "email": os.environ.get(
                    "PI5_ALERTS_NOTIFY_WEBHOOK_FROM",
                    f"{APP_NAME}@{socket.gethostname()}.local",
                ),
                "subject": row["title"],
                "_subject": row["title"],
                "message": row["message"],
                "source": APP_NAME,
                "severity": payload["severity"],
                "sentAt": sent_at,
                "messageId": row["id"],
                "_captcha": os.environ.get("PI5_ALERTS_NOTIFY_WEBHOOK_CAPTCHA", "false"),
                "_template": os.environ.get("PI5_ALERTS_NOTIFY_WEBHOOK_TEMPLATE", "table"),
            }
            data = urlparse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urlrequest.Request(url, data=data, headers=headers, method="POST")
        timeout = env_float("CABRERA_ALERT_RELAY_WEBHOOK_TIMEOUT_SECONDS", 25, 2, 40)
        with urlrequest.urlopen(request, timeout=timeout) as response:
            raw = response.read(65536)
            status = response.getcode() if hasattr(response, "getcode") else 200
            if status >= 400:
                raise RuntimeError(f"webhook returned HTTP {status}")
            try:
                result = json.loads(raw.decode("utf-8", "replace") or "{}")
            except json.JSONDecodeError:
                result = {}
            if isinstance(result, dict):
                success = str(result.get("success", "")).lower()
                if success in {"false", "0", "no"} or result.get("error"):
                    raise RuntimeError(str(result.get("message") or result.get("error") or "webhook rejected message"))

    def drain(self) -> tuple[int, int]:
        limit = int(env_float("CABRERA_ALERT_RELAY_MAX_ATTEMPTS_PER_RUN", 1, 1, 10))
        due = self.conn.execute(
            """
            select * from outbox
            where delivered_at is null and next_attempt_at <= ?
            order by published_at, received_at
            limit ?
            """,
            (time.time(), limit),
        ).fetchall()
        delivered = 0
        failed = 0
        for row in due:
            attempts = int(row["attempts"] or 0) + 1
            try:
                self.deliver(row)
            except Exception as exc:
                failed += 1
                error = redact(f"{type(exc).__name__}: {exc}")[:500]
                backoff = min(21600, 300 * (2 ** min(attempts - 1, 6)))
                with self.conn:
                    self.conn.execute(
                        "update outbox set attempts=?, next_attempt_at=?, last_error=? where id=?",
                        (attempts, time.time() + backoff, error, row["id"]),
                    )
                    self.set_meta("last_delivery_error", error)
                    self.set_meta("last_delivery_attempt_at", iso_now())
            else:
                delivered += 1
                with self.conn:
                    self.conn.execute(
                        "update outbox set attempts=?, delivered_at=?, last_error=null where id=?",
                        (attempts, iso_now(), row["id"]),
                    )
                    self.set_meta("last_delivery_at", iso_now())
                    self.set_meta("last_delivery_attempt_at", iso_now())
                    self.set_meta("last_delivery_error", "")
        return delivered, failed

    def run_once(self) -> dict:
        inserted = self.collect()
        delivered, failed = self.drain()
        result = self.status()
        result.update({"collected": inserted, "deliveredThisRun": delivered, "failedThisRun": failed})
        return result

    def status(self) -> dict:
        pending = self.conn.execute("select count(*) from outbox where delivered_at is null").fetchone()[0]
        delivered = self.conn.execute("select count(*) from outbox where delivered_at is not null").fetchone()[0]
        oldest = self.conn.execute(
            "select received_at from outbox where delivered_at is null order by received_at limit 1"
        ).fetchone()
        return {
            "initialized": self.meta("initialized") == "1",
            "pending": int(pending),
            "delivered": int(delivered),
            "oldestPendingAt": oldest[0] if oldest else None,
            "lastPollAt": self.meta("last_poll_at") or None,
            "lastDeliveryAttemptAt": self.meta("last_delivery_attempt_at") or None,
            "lastDeliveryAt": self.meta("last_delivery_at") or None,
            "lastError": self.meta("last_delivery_error") or None,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Durable Cabrera Network alert relay")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--initialize", action="store_true", help="record the current ntfy cursor without replaying old alerts")
    mode.add_argument("--once", action="store_true", help="collect new alerts and attempt due deliveries")
    mode.add_argument("--status", action="store_true", help="print a secret-free outbox summary")
    args = parser.parse_args()
    relay = AlertRelay()
    try:
        if args.initialize:
            result = relay.initialize()
        elif args.once:
            result = relay.run_once()
        else:
            result = relay.status()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"{APP_NAME} failed: {redact(f'{type(exc).__name__}: {exc}')}", flush=True)
        return 1
    finally:
        relay.close()


if __name__ == "__main__":
    raise SystemExit(main())
