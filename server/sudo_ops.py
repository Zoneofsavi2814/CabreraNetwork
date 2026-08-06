#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SMARTCTL = Path("/usr/sbin/smartctl")
SMART_DEVICE = "/dev/disk/by-id/wwn-0x50014ee2bebee4fe"

ALLOWED_UNITS = {
    "AdGuardHome.service",
    "k3s.service",
    "cabrera-portfolio.service",
    "cabrera-programs.service",
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


def smart_health() -> dict:
    if not SMARTCTL.is_file():
        return {
            "_pi4_noc": {
                "available": False,
                "exitStatus": None,
                "reason": "smartctl is not installed",
            }
        }

    proc = subprocess.run(
        [
            str(SMARTCTL),
            "--json",
            "-d",
            "sat",
            "-H",
            "-A",
            "-l",
            "error",
            "-l",
            "selftest",
            "-n",
            "standby,0",
            SMART_DEVICE,
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
    )
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    has_health_data = (
        isinstance(data.get("smart_status"), dict)
        or isinstance(data.get("ata_smart_attributes"), dict)
        or bool(data.get("power_mode"))
    )
    available = not bool(proc.returncode & 0x03) and has_health_data
    data["_pi4_noc"] = {
        "available": available,
        "exitStatus": proc.returncode,
        "reason": None if available else "SMART command or device passthrough is unavailable",
    }
    return data


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

    if op == "smart_health":
        if len(argv) != 2:
            raise Denied("usage: smart_health")
        print(json.dumps(smart_health(), separators=(",", ":")))
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
