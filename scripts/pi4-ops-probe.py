#!/usr/bin/python3
"""Fixed-scope, read-only Pi4 health probe for the Pi3 operations scheduler.

The SSH boundary is intentionally tiny: callers may select one of four cadence
names, but cannot supply a path, unit, workload, URL, or command.  This module
uses only fixed constants and emits a sanitized, versioned JSON document.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request as urlrequest


SCHEMA_VERSION = 1
PROBE_VERSION = "1.0.0"
HOST_ID = "pi4-core"
ROOT_UID = 0
ALLOWED_CADENCES = ("five-minute", "hourly", "morning", "nightly")
STATUS_ORDER = {"ok": 0, "warn": 1, "fail": 2}

VCGENCMD = "/usr/bin/vcgencmd"
K3S = "/usr/local/bin/k3s"
SS = "/usr/bin/ss"
SYSTEMCTL = "/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
FINDMNT = "/usr/bin/findmnt"
DF = "/usr/bin/df"
SMARTCTL = "/usr/sbin/smartctl"

BOOT_STATE_FILE = Path("/var/lib/pi4-boot-state/state.json")
BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")
BACKUP_ROOT = Path("/mnt/ssd/backups/pi4")
RESTORE_STATE_FILE = Path("/var/lib/pi4-backup/last-restore-drill.json")
PI3_BACKUP_RESTORE_STATE_FILE = Path("/var/lib/pi3-backup/last-restore-check.json")
PI3_BACKUP_ROOT = Path("/mnt/ssd/backups/pi3")
DATA_MOUNT = Path("/mnt/ssd")
BRAIN_MOUNT = Path("/mnt/nas/brain")
BRAIN_STORAGE = Path("/mnt/ssd/nas/brain")
DATA_FS_UUID = "b0a1a356-3c0e-4f68-9c80-3379f662b4bc"
SMART_DEVICE = Path("/dev/disk/by-id/wwn-0x50014ee2bebee4fe")
PORTFOLIO_HEALTH_URL = "http://127.0.0.1:8099/api/health"
GRID_STATS_URL = "http://127.0.0.1:8090/api/stats"

EXPECTED_WORKLOADS = {
    ("homelab", "grid"): "GRID",
    ("eagleeye", "eagleeye"): "EagleEye",
}

ALLOWED_LISTENERS = {
    "tcp/22",
    "tcp/53",
    "tcp/139",
    "tcp/445",
    "tcp/6443",
    "tcp/7777",
    "tcp/8080",
    "tcp/8090",
    "tcp/8096",
    "tcp/8098",
    "tcp/8099",
    "tcp/10250",
    "udp/53",
    "udp/137",
    "udp/138",
    "udp/8472",
    "udp/41641",
    "udp/5353",
}

IGNORED_LISTENER_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.42.0.0/16"),
    ipaddress.ip_network("10.43.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
)

THROTTLE_CURRENT_FLAGS = {
    0: "under-voltage active",
    1: "frequency cap active",
    2: "throttling active",
    3: "soft temperature limit active",
}
THROTTLE_HISTORY_FLAGS = {
    16: "under-voltage occurred",
    17: "frequency cap occurred",
    18: "throttling occurred",
    19: "soft temperature limit occurred",
}

STORAGE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"I/O error",
        r"EXT4-fs error",
        r"Buffer I/O",
        r"blk_update_request",
        r"mmc.*error",
        r"sda.*error",
        r"filesystem.*error",
        r"read-only file system",
    )
)
KERNEL_PATTERNS = STORAGE_PATTERNS + (re.compile(r"Undervoltage detected", re.IGNORECASE),)
SECRET_RE = re.compile(
    r"(?i)(password|passwd|token|secret|api[_-]?key|authorization)(\s*[=:]\s*)([^\s,;]+)"
)
SAFE_METRIC_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sanitize_text(value: object, limit: int = 240) -> str:
    text = " ".join(str(value).replace("\x00", " ").split())
    text = SECRET_RE.sub(r"\1\2[redacted]", text)
    return text[:limit]


def sanitize_metrics(value: object, depth: int = 0) -> object:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, str):
        return sanitize_text(value, 160)
    if isinstance(value, (list, tuple)):
        return [sanitize_metrics(item, depth + 1) for item in value[:32]]
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in list(value.items())[:64]:
            key_text = str(key)
            if SAFE_METRIC_KEY_RE.fullmatch(key_text):
                cleaned[key_text] = sanitize_metrics(item, depth + 1)
        return cleaned
    return sanitize_text(type(value).__name__, 40)


def command_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }


@dataclass
class ProbeContext:
    command_cache: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = field(default_factory=dict)

    def run(
        self,
        command: Sequence[str],
        *,
        timeout: float = 8,
        cache: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        argv = tuple(str(part) for part in command)
        if cache and argv in self.command_cache:
            return self.command_cache[argv]
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=command_environment(),
        )
        if cache:
            self.command_cache[argv] = proc
        return proc

    def k3s_json(self, resource: str) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        # resource is selected only by code-owned call sites, never by input.
        command = (K3S, "kubectl", "get", resource, "-A", "-o", "json")
        proc = self.run(command, timeout=15, cache=True)
        if proc.returncode != 0:
            return proc, {}
        try:
            payload = json.loads(proc.stdout or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return proc, payload if isinstance(payload, dict) else {}


def check_result(
    check_id: str,
    label: str,
    status: str,
    message: str,
    started: float,
    **metrics: object,
) -> dict[str, object]:
    safe_status = status if status in STATUS_ORDER else "fail"
    result: dict[str, object] = {
        "id": check_id,
        "label": label,
        "status": safe_status,
        "message": sanitize_text(message),
        "durationMs": max(0, round((time.monotonic() - started) * 1000)),
    }
    if metrics:
        result["metrics"] = sanitize_metrics(metrics)
    return result


def command_failed(
    check_id: str,
    label: str,
    started: float,
    status: str = "warn",
) -> dict[str, object]:
    return check_result(check_id, label, status, "required local check is unavailable", started)


def http_json(url: str, timeout: float = 3) -> tuple[int, dict[str, Any]]:
    req = urlrequest.Request(url, headers={"User-Agent": "pi4-ops-probe/1.0", "Accept": "application/json"})
    with urlrequest.urlopen(req, timeout=timeout) as response:
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("response too large")
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        status = int(getattr(response, "status", response.getcode()))
    return status, payload if isinstance(payload, dict) else {}


def check_power(ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "pi4-power", "Pi4 power and throttle flags", time.monotonic()
    proc = ctx.run((VCGENCMD, "get_throttled"), timeout=3)
    match = re.search(r"0x[0-9a-fA-F]+", proc.stdout or "") if proc.returncode == 0 else None
    if not match:
        return command_failed(check_id, label, started)
    throttle_hex = match.group(0).lower()
    value = int(throttle_hex, 16)
    current = [name for bit, name in THROTTLE_CURRENT_FLAGS.items() if value & (1 << bit)]
    history = [name for bit, name in THROTTLE_HISTORY_FLAGS.items() if value & (1 << bit)]
    status = "fail" if current else "warn" if history else "ok"
    message = ", ".join(current or history) if current or history else "no throttle flags"
    return check_result(
        check_id,
        label,
        status,
        f"{throttle_hex}: {message}",
        started,
        throttleHex=throttle_hex,
        currentFlags=current,
        historicalFlags=history,
    )


def check_boot_state(_ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "pi4-boot-state", "Pi4 clean-shutdown state", time.monotonic()
    if not BOOT_STATE_FILE.is_file():
        return check_result(check_id, label, "warn", "boot tracking is not initialized", started, trackingState="missing")
    try:
        state = json.loads(BOOT_STATE_FILE.read_text(encoding="utf-8"))
        current_boot_id = BOOT_ID_FILE.read_text(encoding="utf-8").strip()
    except (OSError, json.JSONDecodeError):
        return check_result(check_id, label, "fail", "boot tracking state is unreadable", started, trackingState="invalid")
    if not isinstance(state, dict) or not current_boot_id:
        return check_result(check_id, label, "fail", "boot tracking state is invalid", started, trackingState="invalid")
    unclean_count = int(state.get("uncleanBootCount") or 0)
    if str(state.get("currentBootId") or "") != current_boot_id:
        return check_result(check_id, label, "fail", "boot tracker does not match the running boot", started, trackingState="stale", uncleanBootCount=unclean_count)
    if state.get("currentBootClean") is True:
        return check_result(check_id, label, "fail", "running boot is already marked clean", started, trackingState="invalid", uncleanBootCount=unclean_count)
    previous_clean = state.get("previousBootClean")
    if previous_clean is False:
        return check_result(check_id, label, "fail", "previous boot ended uncleanly", started, trackingState="unclean", previousBootClean=False, uncleanBootCount=unclean_count)
    if previous_clean is None:
        return check_result(check_id, label, "warn", "previous boot state is an initial baseline", started, trackingState="baseline", previousBootClean=None, uncleanBootCount=unclean_count)
    return check_result(check_id, label, "ok", "previous boot shut down cleanly", started, trackingState="clean", previousBootClean=True, uncleanBootCount=unclean_count)


def check_portfolio_loopback(_ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "portfolio-loopback", "Portfolio loopback health", time.monotonic()
    try:
        status_code, payload = http_json(PORTFOLIO_HEALTH_URL, timeout=3)
    except Exception:
        return check_result(check_id, label, "fail", "Portfolio loopback health is unavailable", started)
    healthy = status_code == 200 and payload.get("ok") is True
    return check_result(
        check_id,
        label,
        "ok" if healthy else "fail",
        "Portfolio loopback health is healthy" if healthy else "Portfolio loopback health returned an unhealthy response",
        started,
        httpStatus=status_code,
        healthy=healthy,
    )


def check_k3s_nodes(ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "k3s-node", "Pi4 k3s node readiness", time.monotonic()
    proc, payload = ctx.k3s_json("nodes")
    if proc.returncode != 0 or not isinstance(payload.get("items"), list):
        return command_failed(check_id, label, started, "fail")
    nodes = payload["items"]
    ready = 0
    for node in nodes:
        conditions = node.get("status", {}).get("conditions", []) if isinstance(node, dict) else []
        if any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions if isinstance(item, dict)):
            ready += 1
    healthy = bool(nodes) and ready == len(nodes)
    return check_result(check_id, label, "ok" if healthy else "fail", f"{ready}/{len(nodes)} nodes ready", started, readyNodes=ready, totalNodes=len(nodes))


def workload_readiness(item: dict[str, Any]) -> tuple[int, int]:
    kind = str(item.get("kind") or "")
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    if kind == "DaemonSet":
        return int(status.get("numberReady") or 0), int(status.get("desiredNumberScheduled") or 0)
    return int(status.get("readyReplicas") or 0), int(spec.get("replicas") if spec.get("replicas") is not None else 1)


def retained_workload_items(ctx: ProbeContext) -> tuple[subprocess.CompletedProcess[str], dict[tuple[str, str], dict[str, Any]]]:
    proc, payload = ctx.k3s_json("deployments,statefulsets,daemonsets")
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for item in payload.get("items", []) if isinstance(payload.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        key = (str(metadata.get("namespace") or ""), str(metadata.get("name") or ""))
        if key in EXPECTED_WORKLOADS:
            found[key] = item
    return proc, found


def check_k3s_workloads(ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "k3s-workloads", "Retained Pi4 k3s workloads", time.monotonic()
    proc, found = retained_workload_items(ctx)
    if proc.returncode != 0:
        return command_failed(check_id, label, started, "fail")
    missing = [EXPECTED_WORKLOADS[key] for key in EXPECTED_WORKLOADS if key not in found]
    degraded = []
    for key, item in found.items():
        ready, desired = workload_readiness(item)
        if desired < 1 or ready < desired:
            degraded.append(EXPECTED_WORKLOADS[key])
    healthy = not missing and not degraded
    ready_count = len(EXPECTED_WORKLOADS) - len(missing) - len(degraded)
    return check_result(
        check_id,
        label,
        "ok" if healthy else "fail",
        f"{ready_count}/{len(EXPECTED_WORKLOADS)} retained workloads ready",
        started,
        readyWorkloads=ready_count,
        expectedWorkloads=len(EXPECTED_WORKLOADS),
        missingWorkloads=missing,
        degradedWorkloads=degraded,
    )


def check_k3s_resources(ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "k3s-resource-guardrails", "Retained k3s resource guardrails", time.monotonic()
    proc, found = retained_workload_items(ctx)
    if proc.returncode != 0:
        return command_failed(check_id, label, started)
    missing_workloads = [EXPECTED_WORKLOADS[key] for key in EXPECTED_WORKLOADS if key not in found]
    missing_fields = 0
    inspected_containers = 0
    for item in found.values():
        template = item.get("spec", {}).get("template", {})
        pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
        containers = pod_spec.get("containers", []) if isinstance(pod_spec, dict) else []
        for container in containers:
            if not isinstance(container, dict):
                continue
            inspected_containers += 1
            resources = container.get("resources") if isinstance(container.get("resources"), dict) else {}
            requests = resources.get("requests") if isinstance(resources.get("requests"), dict) else {}
            limits = resources.get("limits") if isinstance(resources.get("limits"), dict) else {}
            missing_fields += sum(1 for field in (requests.get("cpu"), requests.get("memory"), limits.get("cpu"), limits.get("memory")) if not field)
    healthy = not missing_workloads and inspected_containers > 0 and missing_fields == 0
    return check_result(
        check_id,
        label,
        "ok" if healthy else "warn",
        f"{inspected_containers} containers inspected; {missing_fields} guardrails missing",
        started,
        inspectedContainers=inspected_containers,
        missingGuardrails=missing_fields,
        missingWorkloads=missing_workloads,
    )


def files_under(path: Path) -> tuple[int, int]:
    if path.is_symlink():
        return 0, 0
    if path.is_file():
        stat = path.stat()
        return 1, stat.st_size
    count = 0
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
        for name in files:
            item = Path(root) / name
            if item.is_symlink():
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            count += 1
            total += stat.st_size
    return count, total


def check_backup_freshness(_ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "backup-freshness", "Pi4 backup freshness", time.monotonic()
    if not BACKUP_ROOT.is_dir():
        return check_result(check_id, label, "warn", "Pi4 backup directory is missing", started)
    children = [item for item in BACKUP_ROOT.iterdir() if not item.name.startswith(".work-")]
    if not children:
        return check_result(check_id, label, "warn", "no Pi4 backups were found", started, entryCount=0)
    newest = max(children, key=lambda item: item.stat().st_mtime)
    age_hours = max(0.0, (time.time() - newest.stat().st_mtime) / 3600)
    file_count, total_bytes = files_under(newest)
    healthy = age_hours <= 72 and file_count > 0 and total_bytes > 0
    return check_result(
        check_id,
        label,
        "ok" if healthy else "warn",
        f"newest backup is {age_hours:.1f} hours old with {file_count} files",
        started,
        ageHours=age_hours,
        fileCount=file_count,
        bytes=total_bytes,
    )


def within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def check_backup_artifacts(_ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "backup-artifacts", "Pi4 backup artifact integrity", time.monotonic()
    if not BACKUP_ROOT.is_dir():
        return check_result(check_id, label, "warn", "Pi4 backup directory is missing", started)
    archives = sorted(BACKUP_ROOT.rglob("*.tgz"), key=lambda item: item.stat().st_mtime, reverse=True)[:8]
    checksums = sorted(BACKUP_ROOT.rglob("*.sha256"), key=lambda item: item.stat().st_mtime, reverse=True)[:8]
    archive_failures = 0
    checksum_verified = 0
    checksum_failures = 0
    checksum_skipped = 0
    for archive in archives:
        try:
            if not within_root(archive, BACKUP_ROOT):
                archive_failures += 1
                continue
            with tarfile.open(archive.resolve(), "r:*") as bundle:
                if next(iter(bundle), None) is None:
                    archive_failures += 1
        except (OSError, tarfile.TarError):
            archive_failures += 1
    for checksum in checksums:
        try:
            if not within_root(checksum, BACKUP_ROOT):
                checksum_skipped += 1
                continue
            first_line = checksum.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
            match = re.fullmatch(r"([0-9a-fA-F]{64})\s+[ *]?(.+)", first_line)
            if not match:
                checksum_failures += 1
                continue
            target = Path(match.group(2))
            target = target if target.is_absolute() else checksum.parent / target
            if not within_root(target, BACKUP_ROOT) or not within_root(target, checksum.parent):
                checksum_skipped += 1
                continue
            if sha256_file(target.resolve()) == match.group(1).lower():
                checksum_verified += 1
            else:
                checksum_failures += 1
        except (IndexError, OSError, ValueError):
            checksum_failures += 1
    healthy = archive_failures == 0 and checksum_failures == 0 and bool(archives or checksum_verified)
    return check_result(
        check_id,
        label,
        "ok" if healthy else "warn",
        f"{len(archives)} archives checked and {checksum_verified} checksums verified",
        started,
        archiveCount=len(archives),
        archiveFailures=archive_failures,
        checksumVerified=checksum_verified,
        checksumFailures=checksum_failures,
        checksumSkipped=checksum_skipped,
    )


def listener_ignored(address: str) -> bool:
    clean = address.strip("[]")
    if clean in {"", "*", "0.0.0.0", "::"}:
        return False
    if "%" in clean:
        clean = clean.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(clean)
    except ValueError:
        return False
    return any(parsed in network for network in IGNORED_LISTENER_NETWORKS)


def parse_listener(line: str) -> tuple[str, str, int] | None:
    parts = line.split()
    if len(parts) < 5:
        return None
    protocol = parts[0].lower()
    local = parts[4]
    if local.startswith("[") and "]:" in local:
        address, port_text = local.rsplit("]:", 1)
        address = address[1:]
    elif ":" in local:
        address, port_text = local.rsplit(":", 1)
    else:
        return None
    try:
        return protocol, address, int(port_text)
    except ValueError:
        return None


def check_port_drift(ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "port-drift", "Pi4 open-port drift", time.monotonic()
    proc = ctx.run((SS, "-H", "-tuln"), timeout=4)
    if proc.returncode != 0:
        return command_failed(check_id, label, started)
    listeners: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        parsed = parse_listener(line)
        if not parsed:
            continue
        protocol, address, port = parsed
        if protocol not in {"tcp", "udp"} or listener_ignored(address):
            continue
        if protocol == "udp" and port > 20000 and port != 41641:
            continue
        listeners.add(f"{protocol}/{port}")
    unexpected = sorted(listeners - ALLOWED_LISTENERS)
    healthy = not unexpected
    return check_result(
        check_id,
        label,
        "ok" if healthy else "warn",
        f"{len(listeners)} listeners inspected; {len(unexpected)} unexpected",
        started,
        listenerCount=len(listeners),
        unexpectedListeners=unexpected,
    )


def check_journal(ctx: ProbeContext, check_id: str, label: str, since: str, patterns: tuple[re.Pattern[str], ...]) -> dict[str, object]:
    started = time.monotonic()
    proc = ctx.run((JOURNALCTL, "-k", "--since", since, "--no-pager", "--output=cat"), timeout=8)
    if proc.returncode != 0:
        return command_failed(check_id, label, started)
    matches = sum(1 for line in (proc.stdout or "").splitlines() if any(pattern.search(line) for pattern in patterns))
    return check_result(
        check_id,
        label,
        "ok" if matches == 0 else "warn",
        f"{matches} matching kernel events in the review window",
        started,
        matchCount=matches,
    )


def parse_systemd_timestamp(value: str) -> float | None:
    text = value.strip()
    if not text or text.lower() in {"n/a", "never"}:
        return None
    parts = text.split()
    if len(parts) >= 4:
        text = " ".join(parts[:3])
    try:
        return datetime.strptime(text, "%a %Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def systemd_fields(proc: subprocess.CompletedProcess[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value.strip()
    return fields


def check_timer(ctx: ProbeContext, check_id: str, label: str, unit: str, max_last_hours: float | None = None) -> dict[str, object]:
    started = time.monotonic()
    proc = ctx.run((SYSTEMCTL, "show", unit, "-p", "LoadState", "-p", "ActiveState", "-p", "LastTriggerUSec", "-p", "NextElapseUSecRealtime"), timeout=4)
    if proc.returncode != 0:
        return command_failed(check_id, label, started)
    fields = systemd_fields(proc)
    active = fields.get("ActiveState") == "active"
    loaded = fields.get("LoadState") == "loaded"
    last_timestamp = parse_systemd_timestamp(fields.get("LastTriggerUSec", ""))
    age_hours = max(0.0, (time.time() - last_timestamp) / 3600) if last_timestamp is not None else None
    fresh = max_last_hours is None or (age_hours is not None and age_hours <= max_last_hours)
    healthy = loaded and active and fresh
    message = "timer is active" if age_hours is None else f"timer is active; last trigger {age_hours:.1f} hours ago"
    if not healthy:
        message = "timer is missing, inactive, or stale"
    return check_result(check_id, label, "ok" if healthy else "warn", message, started, unit=unit, active=active, loaded=loaded, lastAgeHours=age_hours)


def check_service_result(ctx: ProbeContext, check_id: str, label: str, unit: str) -> dict[str, object]:
    started = time.monotonic()
    proc = ctx.run((SYSTEMCTL, "show", unit, "-p", "LoadState", "-p", "ActiveState", "-p", "Result", "-p", "ExecMainStatus", "-p", "ExecMainStartTimestamp"), timeout=4)
    if proc.returncode != 0:
        return command_failed(check_id, label, started, "fail")
    fields = systemd_fields(proc)
    loaded = fields.get("LoadState") == "loaded"
    active = fields.get("ActiveState", "unknown")
    result = fields.get("Result", "unknown")
    try:
        exit_status = int(fields.get("ExecMainStatus", "0") or 0)
    except ValueError:
        exit_status = -1
    has_run = bool(fields.get("ExecMainStartTimestamp"))
    healthy = loaded and (active in {"active", "activating"} or not has_run or (result == "success" and exit_status == 0))
    message = "service has not run during this boot" if not has_run else f"service result is {result} with exit {exit_status}"
    return check_result(check_id, label, "ok" if healthy else "fail", message, started, unit=unit, activeState=active, serviceResult=result, exitStatus=exit_status, hasRun=has_run)


def check_restore_state(_ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "restore-drill-state", "Pi4 restore-drill freshness", time.monotonic()
    if not RESTORE_STATE_FILE.is_file():
        return check_result(check_id, label, "warn", "restore-drill state is missing", started)
    try:
        payload = json.loads(RESTORE_STATE_FILE.read_text(encoding="utf-8"))
        age_hours = max(0.0, (time.time() - RESTORE_STATE_FILE.stat().st_mtime) / 3600)
    except (OSError, json.JSONDecodeError):
        return check_result(check_id, label, "warn", "restore-drill state is unreadable", started)
    healthy = isinstance(payload, dict) and payload.get("status") == "ok" and age_hours <= 192
    return check_result(check_id, label, "ok" if healthy else "warn", f"restore-drill state is {age_hours:.1f} hours old", started, ageHours=age_hours, successful=bool(isinstance(payload, dict) and payload.get("status") == "ok"))


def check_pi3_backup_restore_state(_ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "pi3-backup-restore-check", "Pi3 backup isolated restore-check", time.monotonic()
    if not PI3_BACKUP_RESTORE_STATE_FILE.is_file():
        return check_result(check_id, label, "warn", "Pi3 backup restore-check state is missing", started)
    try:
        payload = json.loads(PI3_BACKUP_RESTORE_STATE_FILE.read_text(encoding="utf-8"))
        state_age_hours = max(0.0, (time.time() - PI3_BACKUP_RESTORE_STATE_FILE.stat().st_mtime) / 3600)
    except (OSError, json.JSONDecodeError):
        return check_result(check_id, label, "warn", "Pi3 backup restore-check state is unreadable", started)
    checked_timestamp = parse_iso_timestamp(payload.get("checkedAt")) if isinstance(payload, dict) else None
    created_timestamp = parse_iso_timestamp(payload.get("backupCreatedAt")) if isinstance(payload, dict) else None
    checked_age_hours = max(0.0, (time.time() - checked_timestamp) / 3600) if checked_timestamp is not None else None
    backup_age_hours = max(0.0, (time.time() - created_timestamp) / 3600) if created_timestamp is not None else None
    healthy = (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("alertOutboxQuickCheck") == "ok"
        and payload.get("liveServicesChanged") is False
        and checked_age_hours is not None
        and checked_age_hours <= 26
        and backup_age_hours is not None
        and backup_age_hours <= 48
        and state_age_hours <= 26
        and checked_timestamp is not None
        and created_timestamp is not None
        and checked_timestamp >= created_timestamp
    )
    checked_text = f"{checked_age_hours:.1f}h" if checked_age_hours is not None else "unknown"
    backup_text = f"{backup_age_hours:.1f}h" if backup_age_hours is not None else "unknown"
    return check_result(
        check_id,
        label,
        "ok" if healthy else "warn",
        f"verifier age {checked_text}; backup creation age {backup_text}",
        started,
        verifierAgeHours=checked_age_hours,
        backupAgeHours=backup_age_hours,
        stateAgeHours=state_age_hours,
        successful=healthy,
    )


def check_pi3_backup_storage(_ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "pi3-backup-storage", "Pi3 backup storage guardrails", time.monotonic()
    expected = {
        "incoming": (12, 4 * 1024 * 1024 * 1024),
        "processing": (4, 2 * 1024 * 1024 * 1024 + 131072),
        "verified": (14 * 4, 14 * 2 * 1024 * 1024 * 1024),
        "rejected": (8 * 4, 8 * 2 * 1024 * 1024 * 1024),
        "restore-check": (1, 2 * 1024 * 1024 * 1024),
    }
    if not PI3_BACKUP_ROOT.is_dir() or PI3_BACKUP_ROOT.is_symlink():
        return check_result(check_id, label, "warn", "Pi3 backup storage root is missing or unsafe", started)
    issues = 0
    total_files = 0
    total_bytes = 0
    verified_directories = 0
    for name, (max_files, max_bytes) in expected.items():
        root = PI3_BACKUP_ROOT / name
        if not root.is_dir() or root.is_symlink():
            issues += 1
            continue
        root_metadata = os.lstat(root)
        if root_metadata.st_uid != ROOT_UID:
            issues += 1
        if name in {"verified", "rejected"} and root_metadata.st_mode & 0o077:
            issues += 1
        if name == "incoming" and not root_metadata.st_mode & stat.S_ISVTX:
            issues += 1
        if name == "processing" and root_metadata.st_mode & 0o020:
            issues += 1
        files = 0
        bytes_used = 0
        unsafe = 0
        try:
            for current, directories, filenames in os.walk(root, followlinks=False):
                current_path = Path(current)
                for child in directories:
                    metadata = os.lstat(current_path / child)
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                        unsafe += 1
                    elif name in {"verified", "rejected"} and (metadata.st_uid != ROOT_UID or metadata.st_mode & 0o077):
                        unsafe += 1
                for child in filenames:
                    metadata = os.lstat(current_path / child)
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        unsafe += 1
                        continue
                    if name in {"verified", "rejected"} and (metadata.st_uid != ROOT_UID or metadata.st_mode & 0o077):
                        unsafe += 1
                    files += 1
                    bytes_used += metadata.st_size
        except OSError:
            issues += 1
            continue
        if name == "verified":
            verified_directories = sum(1 for item in os.scandir(root) if item.is_dir(follow_symlinks=False))
            if verified_directories < 1 or verified_directories > 14:
                issues += 1
        if files > max_files or bytes_used > max_bytes or unsafe:
            issues += 1
        total_files += files
        total_bytes += bytes_used
    return check_result(
        check_id,
        label,
        "ok" if issues == 0 else "warn",
        f"{total_files} files and {total_bytes / (1024 * 1024):.0f} MiB across guarded backup storage; {issues} issues",
        started,
        fileCount=total_files,
        bytes=total_bytes,
        verifiedBackups=verified_directories,
        issueCount=issues,
    )


def check_disk(ctx: ProbeContext, check_id: str, label: str, path: Path) -> dict[str, object]:
    started = time.monotonic()
    if not path.exists():
        return check_result(check_id, label, "warn", "disk path is missing", started)
    usage = shutil.disk_usage(path)
    used_pct = (usage.used / usage.total * 100) if usage.total else 100.0
    inode_pct: float | None = None
    df_proc = ctx.run((DF, "-Pi", str(path)), timeout=4)
    lines = [line for line in (df_proc.stdout or "").splitlines() if line.strip()]
    if df_proc.returncode == 0 and len(lines) >= 2:
        fields = lines[-1].split()
        if len(fields) >= 5:
            try:
                inode_pct = float(fields[4].rstrip("%"))
            except ValueError:
                inode_pct = None
    mount_proc = ctx.run((FINDMNT, "--target", str(path), "-no", "OPTIONS"), timeout=4)
    options = {part.strip() for part in (mount_proc.stdout or "").strip().split(",") if part.strip()}
    read_only = mount_proc.returncode != 0 or "ro" in options or "rw" not in options
    healthy = used_pct < 85 and not read_only and (inode_pct is None or inode_pct < 85)
    return check_result(check_id, label, "ok" if healthy else "warn", f"{used_pct:.1f}% space used; filesystem is {'read-only' if read_only else 'read-write'}", started, usedPct=used_pct, inodePct=inode_pct, readOnly=read_only)


def check_mount(ctx: ProbeContext, check_id: str, label: str, path: Path) -> dict[str, object]:
    started = time.monotonic()
    proc = ctx.run((FINDMNT, "--target", str(path), "-J", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS,UUID"), timeout=4)
    if proc.returncode != 0:
        return check_result(check_id, label, "fail", "required mount was not found", started)
    try:
        payload = json.loads(proc.stdout or "{}")
        mount = payload["filesystems"][0]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return check_result(check_id, label, "fail", "required mount metadata is unreadable", started)
    options = {part.strip() for part in str(mount.get("options") or "").split(",") if part.strip()}
    fstype = str(mount.get("fstype") or "unknown")
    uuid_matches = str(mount.get("uuid") or "") == DATA_FS_UUID
    read_write = "rw" in options and "ro" not in options
    healthy = fstype == "ext4" and uuid_matches and read_write
    return check_result(check_id, label, "ok" if healthy else "fail", f"mount is {fstype} and {'read-write' if read_write else 'read-only'}", started, filesystem=fstype, uuidMatches=uuid_matches, readWrite=read_write)


def nested_get(payload: object, path: str, default: object = None) -> object:
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def check_smart(ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "data-hdd-smart", "Data HDD SMART health", time.monotonic()
    proc = ctx.run((SMARTCTL, "-j", "-a", "-d", "sat", "-n", "standby", str(SMART_DEVICE)), timeout=30)
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict) or not payload:
        return check_result(check_id, label, "warn", "SMART health data is unavailable", started, smartAvailable=False)
    power_mode = payload.get("power_mode")
    if isinstance(power_mode, dict):
        power_mode = power_mode.get("string") or power_mode.get("name")
    if str(power_mode or "").lower() in {"standby", "sleep", "sleeping"}:
        return check_result(check_id, label, "ok", "drive is in standby; SMART poll did not wake it", started, smartAvailable=True, standby=True)
    attributes: dict[int, int] = {}
    table = nested_get(payload, "ata_smart_attributes.table", [])
    for row in table if isinstance(table, list) else []:
        if isinstance(row, dict):
            attributes[safe_int(row.get("id"), -1)] = safe_int(nested_get(row, "raw.value"), 0)
    passed = nested_get(payload, "smart_status.passed")
    temperature = safe_int(nested_get(payload, "temperature.current"), attributes.get(194, 0))
    reallocated = attributes.get(5, 0)
    pending = attributes.get(197, 0)
    uncorrectable = attributes.get(198, 0)
    interface_errors = attributes.get(199, 0)
    exit_status = safe_int(nested_get(payload, "smartctl.exit_status"), proc.returncode)
    failures = passed is False or pending > 0 or uncorrectable > 0 or bool(exit_status & 0x88)
    warnings = reallocated > 0 or interface_errors > 0 or temperature >= 50 or bool(exit_status & 0x70)
    status = "fail" if failures else "warn" if warnings else "ok"
    return check_result(
        check_id,
        label,
        status,
        "SMART reports a failure" if failures else "SMART reports warnings" if warnings else "SMART is healthy",
        started,
        smartAvailable=True,
        smartPassed=passed if isinstance(passed, bool) else None,
        temperatureC=temperature or None,
        reallocatedSectors=reallocated,
        pendingSectors=pending,
        offlineUncorrectableSectors=uncorrectable,
        interfaceErrors=interface_errors,
        smartctlExitStatus=exit_status,
    )


def check_path_freshness(_ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "grid-brain-freshness", "GRID brain freshness", time.monotonic()
    if not BRAIN_MOUNT.is_dir():
        return check_result(check_id, label, "warn", "GRID brain path is missing", started)
    newest = BRAIN_MOUNT.stat().st_mtime
    file_count = 0
    for item in BRAIN_MOUNT.rglob("*"):
        if item.is_file() and not item.is_symlink():
            file_count += 1
            try:
                newest = max(newest, item.stat().st_mtime)
            except OSError:
                pass
    age_hours = max(0.0, (time.time() - newest) / 3600)
    healthy = file_count > 0 and age_hours <= 168
    return check_result(check_id, label, "ok" if healthy else "warn", f"{file_count} files; newest is {age_hours:.1f} hours old", started, fileCount=file_count, ageHours=age_hours)


def collect_markdown(root: Path, max_hash_files: int = 50) -> tuple[dict[str, tuple[int, str | None]], int]:
    result: dict[str, tuple[int, str | None]] = {}
    unreadable = 0
    files = sorted(item for item in root.rglob("*.md") if item.is_file() and not item.is_symlink())
    for index, item in enumerate(files):
        try:
            size = item.stat().st_size
            digest = sha256_file(item) if index < max_hash_files else None
            result[str(item.relative_to(root))] = (size, digest)
        except OSError:
            unreadable += 1
    return result, unreadable


def check_brain_parity(_ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "grid-brain-parity", "GRID vault path parity", time.monotonic()
    if not BRAIN_MOUNT.is_dir() or not BRAIN_STORAGE.is_dir():
        return check_result(check_id, label, "warn", "GRID source or storage path is missing", started)
    source, source_unreadable = collect_markdown(BRAIN_MOUNT)
    target, target_unreadable = collect_markdown(BRAIN_STORAGE)
    source_keys, target_keys = set(source), set(target)
    missing = source_keys - target_keys
    extra = target_keys - source_keys
    mismatched = {
        name
        for name in source_keys & target_keys
        if source[name][0] != target[name][0]
        or (source[name][1] is not None and target[name][1] is not None and source[name][1] != target[name][1])
    }
    unreadable = source_unreadable + target_unreadable
    healthy = not missing and not extra and not mismatched and unreadable == 0
    return check_result(
        check_id,
        label,
        "ok" if healthy else "warn",
        f"{len(source)} source and {len(target)} stored notes; {len(missing)} missing, {len(extra)} extra, {len(mismatched)} mismatched",
        started,
        sourceCount=len(source),
        targetCount=len(target),
        missingCount=len(missing),
        extraCount=len(extra),
        mismatchCount=len(mismatched),
        unreadableCount=unreadable,
    )


def parse_iso_timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def check_grid_sync(_ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "grid-vault-sync", "GRID vault and index sync", time.monotonic()
    try:
        status_code, payload = http_json(GRID_STATS_URL, timeout=4)
    except Exception:
        return check_result(check_id, label, "warn", "GRID index status is unavailable", started)
    notes = safe_int(payload.get("notes"), 0)
    scan_timestamp = parse_iso_timestamp(payload.get("last_scan_at"))
    scan_age_minutes = max(0.0, (time.time() - scan_timestamp) / 60) if scan_timestamp is not None else None
    healthy = status_code == 200 and notes >= 1 and scan_age_minutes is not None and scan_age_minutes <= 30
    return check_result(check_id, label, "ok" if healthy else "warn", f"{notes} indexed notes; scan age is {scan_age_minutes:.1f} minutes" if scan_age_minutes is not None else f"{notes} indexed notes; scan age is unknown", started, noteCount=notes, scanAgeMinutes=scan_age_minutes, httpStatus=status_code)


def check_backup_retention(_ctx: ProbeContext) -> dict[str, object]:
    check_id, label, started = "backup-retention", "Pi4 backup retention pressure", time.monotonic()
    if not BACKUP_ROOT.is_dir():
        return check_result(check_id, label, "warn", "Pi4 backup directory is missing", started)
    entries = []
    for item in BACKUP_ROOT.iterdir():
        try:
            entries.append(item.stat().st_mtime)
        except OSError:
            continue
    oldest_days = max(0.0, (time.time() - min(entries)) / 86400) if entries else 0.0
    healthy = len(entries) <= 50 and oldest_days <= 180
    return check_result(check_id, label, "ok" if healthy else "warn", f"{len(entries)} entries; oldest is {oldest_days:.0f} days", started, entryCount=len(entries), oldestAgeDays=oldest_days)


CheckFunction = Callable[[ProbeContext], dict[str, object]]


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    label: str
    function: CheckFunction


def bind_check(function: Callable[..., dict[str, object]], *args: object) -> CheckFunction:
    return lambda ctx: function(ctx, *args)


CHECKS: dict[str, tuple[CheckSpec, ...]] = {
    "five-minute": (
        CheckSpec("pi4-power", "Pi4 power and throttle flags", check_power),
        CheckSpec("pi4-boot-state", "Pi4 clean-shutdown state", check_boot_state),
        CheckSpec("portfolio-loopback", "Portfolio loopback health", check_portfolio_loopback),
    ),
    "hourly": (
        CheckSpec("k3s-node", "Pi4 k3s node readiness", check_k3s_nodes),
        CheckSpec("k3s-workloads", "Retained Pi4 k3s workloads", check_k3s_workloads),
        CheckSpec("k3s-resource-guardrails", "Retained k3s resource guardrails", check_k3s_resources),
        CheckSpec("backup-freshness", "Pi4 backup freshness", check_backup_freshness),
        CheckSpec("backup-artifacts", "Pi4 backup artifact integrity", check_backup_artifacts),
        CheckSpec("port-drift", "Pi4 open-port drift", check_port_drift),
    ),
    "morning": (
        CheckSpec("overnight-storage-events", "Overnight storage events", bind_check(check_journal, "overnight-storage-events", "Overnight storage events", "12 hours ago", STORAGE_PATTERNS)),
    ),
    "nightly": (
        CheckSpec("logrotate-timer", "Log rotation timer", bind_check(check_timer, "logrotate-timer", "Log rotation timer", "logrotate.timer", 36)),
        CheckSpec("log2ram-flush-timer", "log2ram daily flush timer", bind_check(check_timer, "log2ram-flush-timer", "log2ram daily flush timer", "log2ram-daily.timer", 36)),
        CheckSpec("tmpfiles-clean-timer", "Temporary-file cleanup timer", bind_check(check_timer, "tmpfiles-clean-timer", "Temporary-file cleanup timer", "systemd-tmpfiles-clean.timer", 48)),
        CheckSpec("dpkg-backup-timer", "Package database backup timer", bind_check(check_timer, "dpkg-backup-timer", "Package database backup timer", "dpkg-db-backup.timer", 36)),
        CheckSpec("pi4-backup-timer", "Pi4 backup timer", bind_check(check_timer, "pi4-backup-timer", "Pi4 backup timer", "pi4-backup.timer")),
        CheckSpec("restore-drill-timer", "Restore drill timer", bind_check(check_timer, "restore-drill-timer", "Restore drill timer", "pi4-restore-drill.timer")),
        CheckSpec("filesystem-trim-timer", "Filesystem trim timer", bind_check(check_timer, "filesystem-trim-timer", "Filesystem trim timer", "fstrim.timer", 192)),
        CheckSpec("smart-short-timer", "Data HDD short SMART timer", bind_check(check_timer, "smart-short-timer", "Data HDD short SMART timer", "pi4-smart-short.timer")),
        CheckSpec("smart-long-timer", "Data HDD long SMART timer", bind_check(check_timer, "smart-long-timer", "Data HDD long SMART timer", "pi4-smart-long.timer")),
        CheckSpec("backup-service-result", "Pi4 backup service result", bind_check(check_service_result, "backup-service-result", "Pi4 backup service result", "pi4-backup.service")),
        CheckSpec("restore-service-result", "Restore-drill service result", bind_check(check_service_result, "restore-service-result", "Restore-drill service result", "pi4-restore-drill.service")),
        CheckSpec("restore-drill-state", "Pi4 restore-drill freshness", check_restore_state),
        CheckSpec("pi3-backup-restore-check", "Pi3 backup isolated restore-check", check_pi3_backup_restore_state),
        CheckSpec("pi3-backup-storage", "Pi3 backup storage guardrails", check_pi3_backup_storage),
        CheckSpec("kernel-storage-power-events", "Kernel storage and power events", bind_check(check_journal, "kernel-storage-power-events", "Kernel storage and power events", "24 hours ago", KERNEL_PATTERNS)),
        CheckSpec("root-disk", "Root disk headroom", bind_check(check_disk, "root-disk", "Root disk headroom", Path("/"))),
        CheckSpec("data-disk", "Data HDD headroom", bind_check(check_disk, "data-disk", "Data HDD headroom", DATA_MOUNT)),
        CheckSpec("data-mount", "Data HDD mount integrity", bind_check(check_mount, "data-mount", "Data HDD mount integrity", DATA_MOUNT)),
        CheckSpec("brain-mount", "GRID brain mount integrity", bind_check(check_mount, "brain-mount", "GRID brain mount integrity", BRAIN_MOUNT)),
        CheckSpec("data-hdd-smart", "Data HDD SMART health", check_smart),
        CheckSpec("grid-brain-freshness", "GRID brain freshness", check_path_freshness),
        CheckSpec("grid-brain-parity", "GRID vault path parity", check_brain_parity),
        CheckSpec("grid-vault-sync", "GRID vault and index sync", check_grid_sync),
        CheckSpec("backup-retention", "Pi4 backup retention pressure", check_backup_retention),
    ),
}


def execute_check(ctx: ProbeContext, spec: CheckSpec) -> dict[str, object]:
    started = time.monotonic()
    try:
        result = spec.function(ctx)
    except subprocess.TimeoutExpired:
        return check_result(spec.check_id, spec.label, "warn", "local check timed out", started)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return check_result(spec.check_id, spec.label, "warn", "local check could not be completed", started)
    if result.get("id") != spec.check_id or result.get("label") != spec.label:
        return check_result(spec.check_id, spec.label, "fail", "local check returned an invalid result", started)
    return result


def run_probe(cadence: str, ctx: ProbeContext | None = None) -> dict[str, object]:
    if cadence not in CHECKS:
        raise ValueError("cadence is not allowed")
    context = ctx or ProbeContext()
    internal_checks = [execute_check(context, spec) for spec in CHECKS[cadence]]
    checks = [
        {
            "id": check["id"],
            "label": check["label"],
            "host": "Pi4",
            "status": check["status"],
            "message": check["message"],
            "durationMs": check["durationMs"],
        }
        for check in internal_checks
    ]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "cadence": cadence,
        "generatedAt": utc_now(),
        "checks": checks,
    }


def error_document(code: str, message: str) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "probeVersion": PROBE_VERSION,
        "probe": "pi4-operations",
        "error": {"code": sanitize_text(code, 64), "message": sanitize_text(message)},
    }


def parse_argv(argv: Sequence[str]) -> str | None:
    if len(argv) != 2 or argv[0] != "--cadence" or argv[1] not in ALLOWED_CADENCES:
        return None
    return argv[1]


def main(argv: Sequence[str] | None = None) -> int:
    cadence = parse_argv(list(sys.argv[1:] if argv is None else argv))
    if cadence is None:
        print(json.dumps(error_document("invalid_request", "exactly one allowed cadence is required"), separators=(",", ":")))
        return 64
    try:
        document = run_probe(cadence)
    except Exception:
        print(json.dumps(error_document("probe_failure", "the Pi4 probe could not complete"), separators=(",", ":")))
        return 70
    print(json.dumps(document, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
