#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys

ALLOWED_UNITS = {
    "AdGuardHome.service",
    "k3s.service",
    "container-uptime-kuma.service",
    "smbd.service",
    "nmbd.service",
    "ssh.service",
}


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


def require_unit(unit: str) -> str:
    if unit not in ALLOWED_UNITS:
        raise Denied(f"unit is not allowlisted: {unit}")
    return unit


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
