#!/usr/bin/env python3
"""Durably relay ntfy messages to the configured email webhook."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import socket
import sqlite3
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


APP_NAME = "cabrera-alert-relay"
DEFAULT_DB = "/var/lib/cabrera-alert-relay/outbox.sqlite3"
PRIVATE_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
URL_RE = re.compile(r"https?://[^\s<>\"']+")
SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret|apikey|api_key|authorization)([=: ]+)(\S+)")
NO_EMAIL_RELAY_TAG = "no-email-relay"
SELF_HEALTH_SOURCE_TAGS = {"pi4-noc", "pi5-critical-alerts"}
SELF_HEALTH_TITLE = "durable email relay outbox"
PERMANENT_ROW_HTTP_STATUSES = {413}
MAX_RETRY_AFTER_SECONDS = 86400.0


NBSP = "\u00a0"  # non-breaking space


class PermanentDeliveryError(RuntimeError):
    """A provider rejection specific to one outbox row."""


def redact(value: str) -> str:
    return URL_RE.sub("<redacted-url>", SECRET_RE.sub(r"\1\2<redacted>", value or ""))


def iso_now() -> str:
    return datetime.now().astimezone().isoformat()


def field_label(text: str) -> str:
    # Form relay backends (PHP-style) rewrite spaces and dots in field names
    # to underscores; non-breaking spaces survive and render as plain spaces.
    return text.replace(" ", NBSP).replace(".", NBSP)


STATUS_PREFIXES = ("🟢", "🟡", "🔴", "🚨", "⚠️", "🔔", "✅", "🛰️")


def severity_style(priority: int) -> tuple[str, str]:
    if priority >= 5:
        return "🚨", "Critical"
    if priority == 4:
        return "⚠️", "High"
    return "🔔", "Normal"


def pretty_subject(icon: str, title: str) -> str:
    # New-style publishers already lead with a status icon; don't stack two.
    if title.startswith(STATUS_PREFIXES):
        return title
    return f"{icon} {title}"


def friendly_time(moment: datetime) -> str:
    clock = moment.strftime("%I:%M %p").lstrip("0")
    return f"{moment.strftime('%A, %B')} {moment.day} · {clock}"


def friendly_received(row) -> str:
    published = int(row["published_at"] or 0)
    if published:
        return friendly_time(datetime.fromtimestamp(published).astimezone())
    try:
        return friendly_time(datetime.fromisoformat(str(row["received_at"])))
    except (TypeError, ValueError):
        return str(row["received_at"] or "unknown")


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def env_value(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def env_float_value(names: tuple[str, ...], default: float, minimum: float, maximum: float) -> float:
    for name in names:
        if os.environ.get(name, "").strip():
            return env_float(name, default, minimum, maximum)
    return max(minimum, min(default, maximum))


def canonical_tags(value) -> tuple[list[str], str]:
    if not isinstance(value, (list, tuple, set)):
        value = []
    tags = sorted({str(tag).strip().lower() for tag in (value or []) if str(tag).strip()})
    return tags, json.dumps(tags, ensure_ascii=False, separators=(",", ":"))


def suppress_from_email_relay(title: str, tags: list[str]) -> bool:
    tag_set = set(tags)
    if NO_EMAIL_RELAY_TAG in tag_set:
        return True
    return bool(SELF_HEALTH_SOURCE_TAGS & tag_set) and SELF_HEALTH_TITLE in title.casefold()


def retry_after_seconds(exc: Exception, now: float | None = None) -> float | None:
    if not isinstance(exc, urlerror.HTTPError) or not exc.headers:
        return None
    value = exc.headers.get("Retry-After")
    if value is None:
        return None
    value = str(value).strip()
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            return None
        return min(MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    seconds = retry_at.timestamp() - (time.time() if now is None else now)
    if not math.isfinite(seconds):
        return None
    return min(MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))


def permanent_delivery_error(exc: Exception) -> bool:
    if isinstance(exc, PermanentDeliveryError):
        return True
    return isinstance(exc, urlerror.HTTPError) and exc.code in PERMANENT_ROW_HTTP_STATUSES


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

    @contextlib.contextmanager
    def exclusive_run(self):
        lock_path = Path(f"{self.db_path}.lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            try:
                lock_path.chmod(0o600)
            except OSError:
                pass
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another alert relay run is already active") from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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
                last_error text,
                coalesced_count integer not null default 0
            );
            create index if not exists outbox_due
                on outbox(delivered_at, next_attempt_at);
            """
        )
        columns = {str(row[1]) for row in self.conn.execute("pragma table_info(outbox)")}
        if "coalesced_count" not in columns:
            self.conn.execute("alter table outbox add column coalesced_count integer not null default 0")
        self.conn.commit()

    def meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("select value from meta where key = ?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "insert into meta(key, value) values(?, ?) on conflict(key) do update set value=excluded.value",
            (key, value),
        )

    def meta_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.meta(key, str(default)))
        except ValueError:
            return default

    def meta_float(self, key: str, default: float = 0.0) -> float:
        try:
            value = float(self.meta(key, str(default)))
        except ValueError:
            return default
        return value if math.isfinite(value) else default

    @staticmethod
    def topic_url() -> tuple[str, str]:
        configured = env_value(
            "CABRERA_ALERT_RELAY_NTFY_URL",
            "PI4_NOC_NOTIFY_NTFY_URL",
            "PI5_ALERTS_NOTIFY_NTFY_URL",
        )
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
            raise ValueError("CABRERA_ALERT_RELAY_NTFY_URL must be a private HTTPS topic URL")
        base = urlparse.urlunsplit((parts.scheme, parts.netloc, f"/{topic}/json", "", ""))
        return base, topic

    def poll(self, since: str) -> list[dict]:
        base, _topic = self.topic_url()
        query = urlparse.urlencode({"poll": "1", "since": since})
        request = urlrequest.Request(
            f"{base}?{query}",
            headers={"Accept": "application/x-ndjson", "User-Agent": f"{APP_NAME}/1"},
        )
        timeout = env_float_value(
            ("CABRERA_ALERT_RELAY_NTFY_TIMEOUT_SECONDS", "PI4_NOC_NOTIFY_NTFY_TIMEOUT_SECONDS"),
            15,
            2,
            30,
        )
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
        with self.exclusive_run():
            return self._initialize()

    def _initialize(self) -> dict:
        messages = self.poll("all")
        with self.conn:
            if messages:
                self.set_meta("cursor", str(messages[-1]["id"]))
            self.set_meta("initialized", "1")
            self.set_meta("last_poll_at", iso_now())
        return self.status()

    def collect(self) -> tuple[int, int, int]:
        cursor = self.meta("cursor")
        since = cursor or ("2m" if self.meta("initialized") == "1" else "all")
        messages = self.poll(since)
        inserted = 0
        coalesced = 0
        suppressed = 0
        with self.conn:
            for item in messages:
                message_id = str(item["id"])
                if self.conn.execute("select 1 from outbox where id = ?", (message_id,)).fetchone():
                    self.set_meta("cursor", message_id)
                    continue
                title = str(item.get("title") or "Cabrera Network alert")[:500]
                tags, tags_json = canonical_tags(item.get("tags"))
                published_at = int(item.get("time") or 0)
                message = str(item.get("message") or "")[:12000]
                priority = int(item.get("priority") or 3)
                if suppress_from_email_relay(title, tags):
                    suppressed_at = iso_now()
                    self.conn.execute(
                        """
                        insert into outbox(
                            id, published_at, title, message, priority, tags, received_at,
                            delivered_at, last_error
                        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message_id,
                            published_at,
                            title,
                            message,
                            priority,
                            tags_json,
                            suppressed_at,
                            suppressed_at,
                            "suppressed: relay self-health remains on ntfy",
                        ),
                    )
                    suppressed += 1
                    self.set_meta("cursor", message_id)
                    continue
                existing = self.conn.execute(
                    """
                    select id, received_at, priority, attempts, next_attempt_at, last_error, coalesced_count
                    from outbox
                    where delivered_at is null and title = ? and tags = ?
                    order by published_at desc, received_at desc
                    limit 1
                    """,
                    (title, tags_json),
                ).fetchone()
                if existing:
                    self.conn.execute(
                        """
                        insert into outbox(
                            id, published_at, title, message, priority, tags, received_at,
                            attempts, next_attempt_at, last_error, coalesced_count
                        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message_id,
                            published_at,
                            title,
                            message,
                            max(priority, int(existing["priority"] or 0)),
                            tags_json,
                            existing["received_at"],
                            int(existing["attempts"] or 0),
                            float(existing["next_attempt_at"] or 0),
                            existing["last_error"],
                            int(existing["coalesced_count"] or 0) + 1,
                        ),
                    )
                    self.conn.execute(
                        """
                        update outbox
                        set delivered_at=?, last_error=?, coalesced_count=0
                        where id=?
                        """,
                        (
                            iso_now(),
                            f"coalesced: superseded by {message_id}",
                            existing["id"],
                        ),
                    )
                    coalesced += 1
                    self.set_meta("cursor", message_id)
                    continue
                result = self.conn.execute(
                    """
                    insert or ignore into outbox(
                        id, published_at, title, message, priority, tags, received_at
                    ) values(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        published_at,
                        title,
                        message,
                        priority,
                        tags_json,
                        iso_now(),
                    ),
                )
                inserted += max(0, result.rowcount)
                self.set_meta("cursor", message_id)
            self.set_meta("initialized", "1")
            self.set_meta("last_poll_at", iso_now())
            self.set_meta("last_collect_count", str(inserted))
            self.set_meta("last_collect_error", "")
        return inserted, coalesced, suppressed

    def compact_pending(self) -> tuple[int, int]:
        rows = self.conn.execute(
            """
            select * from outbox
            where delivered_at is null
            order by published_at, received_at, id
            """
        ).fetchall()
        suppressed_ids = set()
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        normalized_tags = {}
        for row in rows:
            try:
                tags, tags_json = canonical_tags(json.loads(row["tags"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                tags, tags_json = [], "[]"
            normalized_tags[str(row["id"])] = tags_json
            if suppress_from_email_relay(str(row["title"]), tags):
                suppressed_ids.add(str(row["id"]))
                continue
            groups.setdefault((str(row["title"]), tags_json), []).append(row)

        now = iso_now()
        coalesced = 0
        with self.conn:
            for message_id, tags_json in normalized_tags.items():
                self.conn.execute("update outbox set tags=? where id=?", (tags_json, message_id))
            for message_id in suppressed_ids:
                self.conn.execute(
                    """
                    update outbox
                    set delivered_at=?, last_error='suppressed: relay self-health remains on ntfy'
                    where id=? and delivered_at is null
                    """,
                    (now, message_id),
                )
            for duplicates in groups.values():
                if len(duplicates) < 2:
                    continue
                survivor = max(
                    duplicates,
                    key=lambda row: (int(row["published_at"] or 0), str(row["received_at"]), str(row["id"])),
                )
                superseded = [row for row in duplicates if row["id"] != survivor["id"]]
                attempts = max(int(row["attempts"] or 0) for row in duplicates)
                priority = max(int(row["priority"] or 0) for row in duplicates)
                next_attempt = max(float(row["next_attempt_at"] or 0) for row in duplicates)
                received_at = min(str(row["received_at"]) for row in duplicates)
                repeated = sum(int(row["coalesced_count"] or 0) for row in duplicates) + len(superseded)
                error_row = max(duplicates, key=lambda row: int(row["attempts"] or 0))
                self.conn.execute(
                    """
                    update outbox
                    set priority=?, attempts=?, next_attempt_at=?, received_at=?, tags=?, last_error=?, coalesced_count=?
                    where id=?
                    """,
                    (
                        priority,
                        attempts,
                        next_attempt,
                        received_at,
                        normalized_tags[str(survivor["id"])],
                        error_row["last_error"],
                        repeated,
                        survivor["id"],
                    ),
                )
                for row in superseded:
                    self.conn.execute(
                        """
                        update outbox
                        set delivered_at=?, last_error=?, coalesced_count=0
                        where id=? and delivered_at is null
                        """,
                        (now, f"coalesced: superseded by {survivor['id']}", row["id"]),
                    )
                coalesced += len(superseded)
        return coalesced, len(suppressed_ids)

    @staticmethod
    def webhook_config() -> tuple[str, str]:
        url = env_value(
            "CABRERA_ALERT_RELAY_WEBHOOK_URL",
            "PI4_NOC_NOTIFY_WEBHOOK_URL",
            "PI5_ALERTS_NOTIFY_WEBHOOK_URL",
        )
        parts = urlparse.urlsplit(url)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("CABRERA_ALERT_RELAY_WEBHOOK_URL must be HTTPS")
        mode = env_value(
            "CABRERA_ALERT_RELAY_WEBHOOK_FORMAT",
            "PI4_NOC_NOTIFY_WEBHOOK_FORMAT",
            "PI5_ALERTS_NOTIFY_WEBHOOK_FORMAT",
            default="json",
        ).lower()
        return url, mode

    def deliver(self, row: sqlite3.Row) -> None:
        url, mode = self.webhook_config()
        sent_at = iso_now()
        priority = int(row["priority"] or 0)
        icon, severity_word = severity_style(priority)
        payload = {
            "source": APP_NAME,
            "severity": "critical" if priority >= 5 else "info",
            "subject": row["title"],
            "body": row["message"],
            "sentAt": sent_at,
            "messageId": row["id"],
            "occurrenceCount": int(row["coalesced_count"] or 0) + 1,
        }
        headers = {"Accept": "application/json", "User-Agent": f"{APP_NAME}/1"}
        referer = env_value(
            "CABRERA_ALERT_RELAY_WEBHOOK_REFERER",
            "PI4_NOC_NOTIFY_WEBHOOK_REFERER",
            "PI5_ALERTS_NOTIFY_WEBHOOK_REFERER",
        )
        if referer:
            headers["Referer"] = referer
        if mode in {"form", "form-urlencoded", "x-www-form-urlencoded"}:
            # Each field renders as its own labeled row in the provider's
            # email template; keep the visible rows short and scannable.
            form = {
                field_label(f"{icon} Alert"): row["title"],
                field_label("📝 Details"): row["message"] or "(no detail provided)",
                field_label("📟 Severity"): f"{icon} {severity_word} (priority {priority})",
                field_label("🕐 Received"): friendly_received(row),
                field_label("📨 Trace"): f"{APP_NAME} · {socket.gethostname()} · message {row['id']}",
                "_subject": pretty_subject(icon, row["title"]),
                "_captcha": env_value(
                    "CABRERA_ALERT_RELAY_WEBHOOK_CAPTCHA",
                    "PI4_NOC_NOTIFY_WEBHOOK_CAPTCHA",
                    "PI5_ALERTS_NOTIFY_WEBHOOK_CAPTCHA",
                    default="false",
                ),
                "_template": env_value(
                    "CABRERA_ALERT_RELAY_WEBHOOK_TEMPLATE",
                    "PI4_NOC_NOTIFY_WEBHOOK_TEMPLATE",
                    "PI5_ALERTS_NOTIFY_WEBHOOK_TEMPLATE",
                    default="table",
                ),
            }
            if int(row["coalesced_count"] or 0):
                form[field_label("♻️ Repeated")] = f"{int(row['coalesced_count']) + 1} similar alerts combined"
            data = urlparse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urlrequest.Request(url, data=data, headers=headers, method="POST")
        timeout = env_float_value(
            ("CABRERA_ALERT_RELAY_WEBHOOK_TIMEOUT_SECONDS", "PI4_NOC_NOTIFY_WEBHOOK_TIMEOUT_SECONDS"),
            25,
            2,
            40,
        )
        with urlrequest.urlopen(request, timeout=timeout) as response:
            raw = response.read(65536)
            status = response.getcode() if hasattr(response, "getcode") else 200
            if status >= 400:
                if status in PERMANENT_ROW_HTTP_STATUSES:
                    raise PermanentDeliveryError(f"webhook rejected message with HTTP {status}")
                raise RuntimeError(f"webhook returned HTTP {status}")
            try:
                result = json.loads(raw.decode("utf-8", "replace") or "{}")
            except json.JSONDecodeError:
                result = {}
            if isinstance(result, dict):
                success = str(result.get("success", "")).lower()
                if success in {"false", "0", "no"} or result.get("error"):
                    raise RuntimeError(str(result.get("message") or result.get("error") or "webhook rejected message"))

    def provider_backoff_seconds(self, exc: Exception, failures: int, now: float) -> float:
        base = env_float("CABRERA_ALERT_RELAY_PROVIDER_RETRY_BASE_SECONDS", 1800, 300, 21600)
        maximum = env_float("CABRERA_ALERT_RELAY_PROVIDER_RETRY_MAX_SECONDS", 21600, base, 86400)
        exponential = min(maximum, base * (2 ** min(max(0, failures - 1), 8)))
        requested = retry_after_seconds(exc, now)
        return max(exponential, requested or 0.0)

    def drain(self) -> tuple[int, int]:
        now = time.time()
        if self.meta_float("provider_retry_at") > now:
            return 0, 0
        limit = int(env_float("CABRERA_ALERT_RELAY_MAX_ATTEMPTS_PER_RUN", 1, 1, 10))
        due = self.conn.execute(
            """
            select * from outbox
            where delivered_at is null
            order by published_at, received_at
            limit ?
            """,
            (limit,),
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
                if permanent_delivery_error(exc):
                    discarded_error = f"discarded permanent delivery error: {error}"[:500]
                    with self.conn:
                        self.conn.execute(
                            "update outbox set attempts=?, delivered_at=?, last_error=? where id=?",
                            (attempts, iso_now(), discarded_error, row["id"]),
                        )
                        self.set_meta("last_delivery_error", discarded_error)
                        self.set_meta("last_delivery_attempt_at", iso_now())
                        self.set_meta("provider_consecutive_failures", "0")
                        self.set_meta("provider_retry_at", "0")
                    continue
                failures = self.meta_int("provider_consecutive_failures") + 1
                attempted_at = time.time()
                retry_at = attempted_at + self.provider_backoff_seconds(exc, failures, attempted_at)
                with self.conn:
                    self.conn.execute(
                        "update outbox set attempts=?, next_attempt_at=?, last_error=? where id=?",
                        (attempts, retry_at, error, row["id"]),
                    )
                    self.set_meta("provider_consecutive_failures", str(failures))
                    self.set_meta("provider_retry_at", str(retry_at))
                    self.set_meta("last_delivery_error", error)
                    self.set_meta("last_delivery_attempt_at", iso_now())
                break
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
                    self.set_meta("provider_consecutive_failures", "0")
                    self.set_meta("provider_retry_at", "0")
        return delivered, failed

    def run_once(self) -> dict:
        with self.exclusive_run():
            return self._run_once()

    def _run_once(self) -> dict:
        coalesced_existing, suppressed_existing = self.compact_pending()
        inserted = 0
        coalesced = coalesced_existing
        suppressed = suppressed_existing
        collect_failure = None
        try:
            inserted, coalesced_new, suppressed_new = self.collect()
            coalesced += coalesced_new
            suppressed += suppressed_new
        except Exception as exc:
            collect_failure = exc
            error = redact(f"{type(exc).__name__}: {exc}")[:500]
            with self.conn:
                self.set_meta("last_collect_error", error)
        delivered, failed = self.drain()
        result = self.status()
        result.update(
            {
                "collected": inserted,
                "coalescedThisRun": coalesced,
                "suppressedThisRun": suppressed,
                "deliveredThisRun": delivered,
                "failedThisRun": failed,
            }
        )
        if collect_failure is not None:
            raise collect_failure
        return result

    def status(self) -> dict:
        pending = self.conn.execute("select count(*) from outbox where delivered_at is null").fetchone()[0]
        delivered = self.conn.execute(
            "select count(*) from outbox where delivered_at is not null and coalesce(last_error, '') = ''"
        ).fetchone()[0]
        discarded = self.conn.execute(
            "select count(*) from outbox where delivered_at is not null and coalesce(last_error, '') != ''"
        ).fetchone()[0]
        coalesced = self.conn.execute("select coalesce(sum(coalesced_count), 0) from outbox").fetchone()[0]
        oldest = self.conn.execute(
            "select received_at from outbox where delivered_at is null order by received_at limit 1"
        ).fetchone()
        pending_error = self.conn.execute(
            """
            select last_error from outbox
            where delivered_at is null and coalesce(last_error, '') != ''
            order by received_at
            limit 1
            """
        ).fetchone()
        retry_at = self.meta_float("provider_retry_at")
        circuit_open = retry_at > time.time()
        return {
            "initialized": self.meta("initialized") == "1",
            "pending": int(pending),
            "delivered": int(delivered),
            "discarded": int(discarded),
            "coalesced": int(coalesced),
            "oldestPendingAt": oldest[0] if oldest else None,
            "lastPollAt": self.meta("last_poll_at") or None,
            "lastCollectError": self.meta("last_collect_error") or None,
            "lastDeliveryAttemptAt": self.meta("last_delivery_attempt_at") or None,
            "lastDeliveryAt": self.meta("last_delivery_at") or None,
            "lastError": self.meta("last_delivery_error") or None,
            "oldestPendingError": pending_error[0] if pending_error else None,
            "providerCircuitOpen": circuit_open,
            "providerRetryAt": datetime.fromtimestamp(retry_at).astimezone().isoformat() if retry_at else None,
            "providerConsecutiveFailures": self.meta_int("provider_consecutive_failures"),
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
