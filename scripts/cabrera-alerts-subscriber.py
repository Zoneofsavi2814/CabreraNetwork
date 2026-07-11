#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib import parse as urlparse
from urllib import request as urlrequest

TOPIC_RE = re.compile(r"[-_A-Za-z0-9]{16,64}")
APPLESCRIPT = """
on run argv
    display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"
end run
"""


def parse_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def validate_topic_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse.urlsplit(normalized)
    topic = parsed.path.strip("/")
    if parsed.scheme != "https" or not parsed.netloc or not TOPIC_RE.fullmatch(topic):
        raise ValueError("NTFY_TOPIC_URL must be an HTTPS URL with a private 16-64 character topic")
    return normalized


def load_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def append_receipt(path: Path, receipt: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


def display_notification(title: str, message: str) -> bool:
    proc = subprocess.run(
        ["/usr/bin/osascript", "-e", APPLESCRIPT, title[:160], message[:600]],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        print(f"macOS notification failed with status {proc.returncode}", file=sys.stderr, flush=True)
    return proc.returncode == 0


class Subscriber:
    def __init__(self, state_path: Path, receipt_path: Path) -> None:
        self.state_path = state_path
        self.receipt_path = receipt_path
        self.state = load_state(state_path)
        recent = self.state.get("recentIds", [])
        self.recent_ids = [str(item) for item in recent[-100:]] if isinstance(recent, list) else []

    def handle(self, event: dict) -> bool:
        if event.get("event") != "message":
            return False
        event_id = str(event.get("id") or "")
        if not event_id or event_id in self.recent_ids:
            return False
        title = str(event.get("title") or "Cabrera Network Alert")
        message = str(event.get("message") or "New infrastructure alert")
        shown = display_notification(title, message)
        event_time = int(event.get("time") or time.time())
        self.recent_ids = (self.recent_ids + [event_id])[-100:]
        self.state = {"lastTime": event_time, "recentIds": self.recent_ids, "updatedAt": int(time.time())}
        save_state(self.state_path, self.state)
        append_receipt(
            self.receipt_path,
            {
                "id": event_id,
                "time": event_time,
                "title": title[:200],
                "priority": event.get("priority"),
                "displayed": shown,
            },
        )
        return True


def subscription_url(topic_url: str, last_time: int, *, poll: bool) -> str:
    query = {"since": str(max(0, last_time))}
    if poll:
        query["poll"] = "1"
    return f"{topic_url}/json?{urlparse.urlencode(query)}"


def consume(topic_url: str, subscriber: Subscriber, *, poll: bool) -> None:
    last_time = int(subscriber.state.get("lastTime") or (time.time() - 60))
    req = urlrequest.Request(
        subscription_url(topic_url, last_time, poll=poll),
        headers={"Accept": "application/x-ndjson", "User-Agent": "cabrera-alerts/macos"},
    )
    with urlrequest.urlopen(req, timeout=90 if not poll else 20) as response:
        for raw in response:
            try:
                event = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                subscriber.handle(event)


def main() -> int:
    home = Path.home()
    default_root = home / "Library" / "Application Support" / "Cabrera Alerts"
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=default_root / "ntfy.env")
    parser.add_argument("--state", type=Path, default=default_root / "state.json")
    parser.add_argument("--receipts", type=Path, default=default_root / "receipts.jsonl")
    parser.add_argument("--poll-once", action="store_true")
    parser.add_argument("--validate-url")
    args = parser.parse_args()

    if args.validate_url is not None:
        validate_topic_url(args.validate_url)
        return 0

    topic_url = validate_topic_url(parse_config(args.config)["NTFY_TOPIC_URL"])
    subscriber = Subscriber(args.state, args.receipts)
    if args.poll_once:
        consume(topic_url, subscriber, poll=True)
        return 0

    while True:
        try:
            consume(topic_url, subscriber, poll=False)
        except Exception as exc:
            print(f"ntfy subscription reconnecting after {type(exc).__name__}", file=sys.stderr, flush=True)
            time.sleep(15)


if __name__ == "__main__":
    raise SystemExit(main())
