#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

DEFAULT_STATE_PATH = Path("/var/lib/pi4-boot-state/state.json")
DEFAULT_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def read_boot_id(boot_id_path: Path = DEFAULT_BOOT_ID_PATH) -> str:
    boot_id = boot_id_path.read_text(encoding="utf-8").strip()
    if not boot_id:
        raise RuntimeError(f"boot ID is empty: {boot_id_path}")
    return boot_id


def load_state(state_path: Path = DEFAULT_STATE_PATH) -> dict:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError(f"boot state must be a JSON object: {state_path}")
    return data


def write_state(state: dict, state_path: Path = DEFAULT_STATE_PATH) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{state_path.name}.", dir=state_path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(0o644)
        os.replace(tmp_path, state_path)
        directory_fd = os.open(state_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def mark_boot_started(
    state_path: Path = DEFAULT_STATE_PATH,
    boot_id_path: Path = DEFAULT_BOOT_ID_PATH,
    timestamp: str | None = None,
) -> dict:
    timestamp = timestamp or now_iso()
    boot_id = read_boot_id(boot_id_path)
    prior = load_state(state_path)

    if prior.get("currentBootId") == boot_id:
        state = dict(prior)
        state["schemaVersion"] = 1
        state["currentBootClean"] = False
        state.pop("cleanShutdownAt", None)
        state.setdefault("currentBootStartedAt", timestamp)
        state["updatedAt"] = timestamp
        write_state(state, state_path)
        return state

    previous_boot_id = prior.get("currentBootId")
    previous_boot_clean = prior.get("currentBootClean") if previous_boot_id else None
    if not isinstance(previous_boot_clean, bool):
        previous_boot_clean = None

    unclean_count = int(prior.get("uncleanBootCount") or 0)
    last_unclean_id = prior.get("lastUncleanBootId")
    last_unclean_at = prior.get("lastUncleanBootDetectedAt")
    if previous_boot_clean is False:
        unclean_count += 1
        last_unclean_id = previous_boot_id
        last_unclean_at = timestamp

    state = {
        "schemaVersion": 1,
        "currentBootId": boot_id,
        "currentBootStartedAt": timestamp,
        "currentBootClean": False,
        "previousBootId": previous_boot_id,
        "previousBootClean": previous_boot_clean,
        "previousCleanShutdownAt": prior.get("cleanShutdownAt"),
        "uncleanBootCount": unclean_count,
        "lastUncleanBootId": last_unclean_id,
        "lastUncleanBootDetectedAt": last_unclean_at,
        "updatedAt": timestamp,
    }
    write_state(state, state_path)
    return state


def mark_boot_clean(
    state_path: Path = DEFAULT_STATE_PATH,
    boot_id_path: Path = DEFAULT_BOOT_ID_PATH,
    timestamp: str | None = None,
) -> dict:
    timestamp = timestamp or now_iso()
    boot_id = read_boot_id(boot_id_path)
    state = load_state(state_path)
    if state.get("currentBootId") != boot_id:
        state = {
            "schemaVersion": 1,
            "currentBootId": boot_id,
            "currentBootStartedAt": None,
            "previousBootId": None,
            "previousBootClean": None,
            "uncleanBootCount": int(state.get("uncleanBootCount") or 0),
            "lastUncleanBootId": state.get("lastUncleanBootId"),
            "lastUncleanBootDetectedAt": state.get("lastUncleanBootDetectedAt"),
        }
    state["currentBootClean"] = True
    state["cleanShutdownAt"] = timestamp
    state["updatedAt"] = timestamp
    write_state(state, state_path)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist Pi4 boot and clean-shutdown state")
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--boot-id", type=Path, default=DEFAULT_BOOT_ID_PATH)
    args = parser.parse_args()

    if args.action == "start":
        mark_boot_started(args.state, args.boot_id)
    else:
        mark_boot_clean(args.state, args.boot_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
