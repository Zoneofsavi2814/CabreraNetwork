#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys

ALLOWED_UNITS = {
    "AdGuardHome.service",
    "k3s.service",
    "container-uptime-kuma.service",
    "container-esty.service",
    "smbd.service",
    "nmbd.service",
    "ssh.service",
    "podman.socket",
}

NAME_RE = re.compile(r"^[A-Za-z0-9_.@:/-]{1,128}$")


class Denied(Exception):
    pass


def run(argv: list[str], timeout: int = 20) -> str:
    proc = subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout.strip() or f"{argv[0]} exited {proc.returncode}")
    return proc.stdout


def podman_names() -> set[str]:
    raw = run(["/usr/bin/podman", "ps", "-a", "--format", "json"], timeout=10)
    try:
        rows = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return set()
    names: set[str] = set()
    for row in rows:
        for name in row.get("Names") or []:
            names.add(str(name))
        if row.get("Name"):
            names.add(str(row["Name"]))
    return names


def require_unit(unit: str) -> str:
    if unit not in ALLOWED_UNITS:
        raise Denied(f"unit is not allowlisted: {unit}")
    return unit


def require_container(name: str) -> str:
    if not NAME_RE.match(name):
        raise Denied("invalid container name")
    if name not in podman_names():
        raise Denied(f"container is not discovered: {name}")
    return name


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise Denied("missing operation")
    op = argv[1]

    if op == "journal":
        if len(argv) != 4:
            raise Denied("usage: journal <unit> <lines>")
        unit = require_unit(argv[2])
        lines = max(1, min(int(argv[3]), 600))
        print(run(["/usr/bin/journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=15), end="")
        return 0

    if op == "systemd_restart":
        if len(argv) != 3:
            raise Denied("usage: systemd_restart <unit>")
        unit = require_unit(argv[2])
        print(run(["/usr/bin/systemctl", "restart", unit], timeout=30), end="")
        return 0

    if op == "podman_ps":
        print(run(["/usr/bin/podman", "ps", "-a", "--format", "json"], timeout=10), end="")
        return 0

    if op == "podman_stats":
        print(run(["/usr/bin/podman", "stats", "--no-stream", "--format", "json"], timeout=10), end="")
        return 0

    if op == "podman_logs":
        if len(argv) != 4:
            raise Denied("usage: podman_logs <container> <lines>")
        name = require_container(argv[2])
        lines = max(1, min(int(argv[3]), 600))
        print(run(["/usr/bin/podman", "logs", "--tail", str(lines), name], timeout=15), end="")
        return 0

    if op == "podman_action":
        if len(argv) != 4:
            raise Denied("usage: podman_action <start|stop|restart> <container>")
        action = argv[2]
        if action not in {"start", "stop", "restart"}:
            raise Denied("podman action is not allowlisted")
        name = require_container(argv[3])
        print(run(["/usr/bin/podman", action, name], timeout=40), end="")
        return 0

    if op == "podman_start_all":
        raw = run(["/usr/bin/podman", "ps", "-a", "--format", "json"], timeout=10)
        rows = json.loads(raw or "[]")
        names = []
        for row in rows:
            state = str(row.get("State") or row.get("Status") or "").lower()
            row_names = row.get("Names") or []
            if row_names and "running" not in state:
                names.append(str(row_names[0]))
        if not names:
            print("No stopped rootful containers discovered.")
            return 0
        print(run(["/usr/bin/podman", "start", *names], timeout=45), end="")
        return 0

    raise Denied(f"operation is not allowlisted: {op}")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except Denied as exc:
        print(f"DENIED: {exc}", file=sys.stderr)
        raise SystemExit(64)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
