#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import copy
import ipaddress
import json
import math
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

import psutil
from flask import Flask, Response, jsonify, request, send_from_directory, session

try:
    from .tplink_collector import TplinkTopologyCollector
except ImportError:  # pragma: no cover - used when app.py is executed directly on the Pi
    from tplink_collector import TplinkTopologyCollector

APP_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = Path(os.environ.get("PI4_NOC_DIST", APP_ROOT / "dist"))
SUDO_HELPER = Path(os.environ.get("PI4_NOC_SUDO_HELPER", APP_ROOT / "server" / "sudo_ops.py"))
ADGUARD_CREDS = Path(os.environ.get("PI4_NOC_ADGUARD_CREDS", "/home/pi4/.adguard-home-admin"))
LAN_IP = os.environ.get("PI4_NOC_LAN_IP", "192.168.0.101")
ROUTER_IP = os.environ.get("PI4_NOC_ROUTER_IP", "192.168.0.1")
ROUTER_NAME = os.environ.get("PI4_NOC_ROUTER_NAME", "TP-Link Archer BE400")
ROUTER_CREDS_FILE = Path(os.environ.get("PI4_NOC_ROUTER_CREDS", "/home/pi4/.pi4-noc-router-admin"))
ROUTER_TOPOLOGY_FILE = Path(os.environ.get("PI4_NOC_ROUTER_TOPOLOGY", "/home/pi4/.pi4-noc-router-topology.json"))
DEVICE_ALIASES_FILE = Path(os.environ.get("PI4_NOC_DEVICE_ALIASES", "/home/pi4/.pi4-noc-device-aliases.json"))
NETBIOS_NAME_LOOKUPS = os.environ.get("PI4_NOC_NETBIOS_NAMES", "0").lower() in {"1", "true", "yes"}
AUTH_USER = os.environ.get("PI4_NOC_AUTH_USER", "pi4")
AUTH_SERVICE = os.environ.get("PI4_NOC_AUTH_SERVICE", "login")
SESSION_SECRET_FILE = Path(os.environ.get("PI4_NOC_SESSION_SECRET_FILE", "/etc/pi4-noc/session-secret"))
HISTORY_LEN = 60

DEFAULT_APS = [
    {
        "id": "basement",
        "name": "ArcherAX3000Pro_Basement",
        "ip": "192.168.0.117",
        "mac": "98:03:8e:65:a4:ec",
        "location": "Basement",
    },
    {
        "id": "loft",
        "name": "ArcherAX3000Pro_Loft",
        "ip": "192.168.0.176",
        "mac": "98:03:8e:44:f7:e4",
        "location": "Loft",
    },
]
VALID_AP_IDS = {"", "main", "basement", "loft", "unknown"}
VALID_LINK_TYPES = {"", "unknown", "wired", "wifi", "2.4g", "5g", "6g", "lan-observed"}
NAME_SOURCE_RANK = {"router": 60, "configured_ap": 55, "adguard": 45, "resolved": 35, "arp": 10}
LINK_SOURCE_RANK = {"router": 60, "configured_ap": 55, "adguard": 15, "resolved": 10, "arp": 5}

UNIT_CONFIG = [
    {"id": "adguard", "label": "AdGuard Home", "unit": "AdGuardHome.service", "ports": ["53", "8080"], "glyph": "brandShield", "ui": f"http://{LAN_IP}:8080"},
    {"id": "k3s", "label": "k3s", "unit": "k3s.service", "ports": ["6443"], "glyph": "brandCubes"},
    {"id": "smbd", "label": "Samba (smbd)", "unit": "smbd.service", "ports": ["445"], "glyph": "brandFolderNet"},
    {"id": "nmbd", "label": "Samba (nmbd)", "unit": "nmbd.service", "ports": ["139"], "glyph": "brandFolderNet"},
    {"id": "ssh", "label": "SSH", "unit": "ssh.service", "ports": ["22"], "glyph": "brandTerminal"},
    {"id": "podman", "label": "Podman socket", "unit": "podman.socket", "ports": [], "glyph": "brandSocket"},
]

WEB_APP_CONFIG = [
    {"id": "cabrera-network", "label": "Cabrera Network", "url": "http://cabrera.home.arpa/", "port": "80", "glyph": "activity", "kind": "dashboard"},
    {"id": "adguard", "label": "AdGuard Home", "url": "http://adguard.home.arpa:8080/", "port": "8080", "glyph": "brandShield", "kind": "admin"},
    {"id": "uptime-kuma", "label": "Uptime Kuma", "url": "http://kuma.home.arpa:3001/", "port": "3001", "glyph": "brandHeartbeat", "kind": "monitoring"},
    {"id": "grid-wiki", "label": "GRID Wiki", "url": "http://grid.home.arpa:8090/", "port": "8090", "glyph": "globe", "kind": "knowledge"},
    {"id": "grid-api", "label": "GRID MCP/API", "url": "http://grid-api.home.arpa:7777/", "port": "7777", "glyph": "brandSocket", "kind": "api"},
]

ALLOWED_UNITS = {row["unit"] for row in UNIT_CONFIG}
PROTECTED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "ingress-nginx"}
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@:/-]{1,160}$")
SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret|apikey|api_key|authorization)([=: ]+)(\S+)")
MAC_RE = re.compile(r"^(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.I)
AUTH_EXEMPT_API_PATHS = {"/api/session", "/api/login", "/api/logout"}
LOGIN_FAILURES: dict[str, deque[float]] = {}
LOGIN_LOCK = threading.RLock()


def load_session_secret() -> str:
    secret = os.environ.get("PI4_NOC_SESSION_SECRET", "").strip()
    if secret:
        return secret
    try:
        secret = SESSION_SECRET_FILE.read_text().strip()
        if secret:
            return secret
    except Exception:
        pass
    return secrets.token_urlsafe(32)


def throttle_login_attempt(key: str) -> None:
    now = time.monotonic()
    with LOGIN_LOCK:
        failures = LOGIN_FAILURES.setdefault(key, deque(maxlen=8))
        while failures and now - failures[0] > 60:
            failures.popleft()
        delay = min(2.0, max(0, len(failures) - 2) * 0.35)
    if delay:
        time.sleep(delay)


def record_login_failure(key: str) -> None:
    with LOGIN_LOCK:
        LOGIN_FAILURES.setdefault(key, deque(maxlen=8)).append(time.monotonic())


def clear_login_failures(key: str) -> None:
    with LOGIN_LOCK:
        LOGIN_FAILURES.pop(key, None)


def verify_login_password(password: str) -> bool:
    if not password:
        return False
    try:
        import pam  # type: ignore

        return bool(pam.pam().authenticate(AUTH_USER, password, service=AUTH_SERVICE))
    except ImportError:
        pass
    except Exception:
        return False
    try:
        import pamela  # type: ignore

        pamela.authenticate(AUTH_USER, password, service=AUTH_SERVICE)
        return True
    except Exception:
        return False


def login_client_key() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "local").split(",")[0].strip() or "local"


def authenticated() -> bool:
    return bool(session.get("authenticated") and session.get("user") == AUTH_USER)


def redact(text: str) -> str:
    return SECRET_RE.sub(r"\1\2<redacted>", text or "")


def run_cmd(argv: list[str], timeout: int = 8) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def run_text(argv: list[str], timeout: int = 8) -> str:
    try:
        proc = run_cmd(argv, timeout=timeout)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return redact(proc.stdout.strip())


def run_json(argv: list[str], timeout: int = 8, default=None):
    try:
        proc = run_cmd(argv, timeout=timeout)
        if proc.returncode != 0:
            return default
        return json.loads(proc.stdout or "null")
    except Exception:
        return default


def run_privileged(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    if not SUDO_HELPER.exists():
        return subprocess.CompletedProcess(args, 127, "", f"sudo helper missing at {SUDO_HELPER}")
    return run_cmd(["/usr/bin/sudo", "-n", str(SUDO_HELPER), *args], timeout=timeout)


def run_privileged_text(args: list[str], timeout: int = 20) -> str:
    try:
        proc = run_privileged(args, timeout=timeout)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return redact(proc.stdout.strip())


def run_privileged_json(args: list[str], timeout: int = 20, default=None):
    try:
        proc = run_privileged(args, timeout=timeout)
        if proc.returncode != 0:
            return default
        return json.loads(proc.stdout or "null")
    except Exception:
        return default


def status_tone(status: str) -> str:
    if status in {"running", "active", "ok", "ready"}:
        return "ok"
    if status in {"exited", "inactive", "pending", "unknown"}:
        return "warn"
    return "fail"


def safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace("%", "").replace("MB", "").strip())
    except Exception:
        return default


def safe_int(value, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except Exception:
        return default


def parse_bytes_pair(text: str) -> tuple[float, float]:
    parts = [p.strip() for p in str(text).split("/")]
    if len(parts) != 2:
        return 0.0, 0.0
    return parse_size_mb(parts[0]), parse_size_mb(parts[1])


def parse_size_mb(value: str) -> float:
    value = value.strip()
    m = re.match(r"([0-9.]+)\s*([KMGT]?B)?", value, re.I)
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    if unit == "KB":
        return num / 1024
    if unit == "MB":
        return num
    if unit == "GB":
        return num * 1024
    if unit == "TB":
        return num * 1024 * 1024
    return num / (1024 * 1024)


def uptime_label(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def local_ip() -> str:
    try:
        out = run_text(["/bin/hostname", "-I"], timeout=2)
        for part in out.split():
            if part.startswith("192.168."):
                return part
        for part in out.split():
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", part):
                return part
    except Exception:
        pass
    return LAN_IP


def service_show(unit: str) -> dict[str, str]:
    proc = run_cmd(
        [
            "/usr/bin/systemctl",
            "show",
            unit,
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "NRestarts",
            "-p",
            "ExecMainStartTimestamp",
            "-p",
            "MainPID",
        ],
        timeout=4,
    )
    data: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v
    return data


def listening_ports() -> set[str]:
    ports: set[str] = set()
    proc = run_cmd(["/usr/bin/ss", "-H", "-ltnu"], timeout=4)
    for line in (proc.stdout or "").splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        local = fields[4]
        port = local.rsplit(":", 1)[-1].strip("[]")
        if port.isdigit():
            ports.add(port)
    return ports


def k3s_host_ports() -> set[str]:
    pods = run_json(["/usr/local/bin/kubectl", "get", "pods", "-A", "-o", "json"], timeout=12, default={"items": []}) or {"items": []}
    ports: set[str] = set()
    for pod in pods.get("items", []):
        if pod.get("status", {}).get("phase") != "Running":
            continue
        spec = pod.get("spec", {})
        containers = [*spec.get("initContainers", []), *spec.get("containers", [])]
        for container in containers:
            for port in container.get("ports") or []:
                host_port = port.get("hostPort")
                if isinstance(host_port, int) and host_port > 0:
                    ports.add(str(host_port))
    return ports


def read_adguard_creds() -> dict[str, str]:
    creds: dict[str, str] = {}
    if not ADGUARD_CREDS.exists():
        return creds
    for line in ADGUARD_CREDS.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            creds[key.strip()] = value.strip()
    if "url" not in creds:
        creds["url"] = "http://127.0.0.1:8080"
    return creds


def adguard_api(path: str, method: str = "GET", body=None):
    creds = read_adguard_creds()
    user = creds.get("username")
    password = creds.get("password")
    url = creds.get("url", "http://127.0.0.1:8080").rstrip("/") + path
    if not user or not password:
        raise RuntimeError("AdGuard credentials are unavailable")
    headers = {
        "Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode(),
        "Content-Type": "application/json",
    }
    payload = None if body is None else json.dumps(body).encode()
    req = urlrequest.Request(url, data=payload, headers=headers, method=method)
    with urlrequest.urlopen(req, timeout=4) as resp:
        raw = resp.read().decode()
        if not raw:
            return {}
        return json.loads(raw)


def top_items(rows, limit: int = 5, include_ip: bool = False) -> list[dict]:
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for name, count in row.items():
            item = {"name": str(name), "count": int(count or 0)}
            if include_ip and re.match(r"^\d+\.\d+\.\d+\.\d+$", str(name)):
                item["ip"] = str(name)
            items.append(item)
            break
    return items[:limit]


def normalize_mac(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).strip().lower().replace("-", ":")
    if not MAC_RE.match(raw) or raw == "00:00:00:00:00:00":
        return ""
    return raw


def is_ipv4(value: str | None) -> bool:
    if not value:
        return False
    try:
        ipaddress.ip_address(str(value))
        return "." in str(value)
    except ValueError:
        return False


def ip_sort_key(value: str | None) -> tuple[int, int, int, int]:
    if not is_ipv4(value):
        return (999, 999, 999, 999)
    return tuple(int(part) for part in str(value).split("."))


def ip_cmd() -> str:
    return shutil.which("ip") or "/usr/sbin/ip"


def default_gateway() -> str:
    out = run_text([ip_cmd(), "route", "show", "default"], timeout=2)
    match = re.search(r"\bvia\s+(\d+\.\d+\.\d+\.\d+)", out)
    return match.group(1) if match else ROUTER_IP


def interface_label(value: str | None) -> str:
    raw = (value or "").strip()
    low = raw.lower()
    if low in {"lan-observed", "lan observed"}:
        return "LAN observed"
    if low in {"2.4g", "2g", "wifi-2g", "wlan2g"}:
        return "2.4G"
    if low in {"5g", "5g-1", "5g-2", "wifi-5g", "wlan5g"}:
        return "5G"
    if low in {"6g", "wifi-6g", "wlan6g"}:
        return "6G"
    if low == "lan":
        return "LAN"
    if low in {"wired", "ethernet"} or low.startswith(("eth", "en", "br-", "bond")):
        return "Wired"
    if low.startswith(("wl", "wifi", "wlan")):
        return "Wi-Fi"
    return raw or "unknown"


def normalize_link_type(value: str | None) -> str:
    label = interface_label(value)
    if label == "Wired":
        return "wired"
    if label == "2.4G":
        return "2.4g"
    if label == "5G":
        return "5g"
    if label == "6G":
        return "6g"
    if label == "Wi-Fi":
        return "wifi"
    if label in {"LAN", "LAN observed"}:
        return "lan-observed"
    return "unknown"


def link_label(value: str | None) -> str:
    link_type = normalize_link_type(value)
    return {
        "wired": "Wired",
        "wifi": "Wi-Fi",
        "2.4g": "2.4G",
        "5g": "5G",
        "6g": "6G",
        "lan-observed": "LAN observed",
    }.get(link_type, "Unknown")


def clean_text(value, max_len: int = 80) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def device_key(ip: str | None = "", mac: str | None = "") -> str:
    mac = normalize_mac(mac)
    if mac:
        return f"mac:{mac}"
    if is_ipv4(ip):
        return f"ip:{ip}"
    return ""


def validate_alias_key(key: str) -> bool:
    key = str(key or "").strip().lower()
    if key.startswith("mac:"):
        return bool(normalize_mac(key[4:]))
    if key.startswith("ip:"):
        return is_ipv4(key[3:])
    return False


def normalize_ap_id(value: str | None) -> str:
    raw = clean_text(value, 80).lower()
    if not raw:
        return ""
    raw_compact = re.sub(r"[^a-z0-9.:-]+", "", raw)
    for ap in DEFAULT_APS:
        if raw in {ap["id"], ap["name"].lower(), ap["ip"], ap["mac"]}:
            return ap["id"]
        if raw_compact in {ap["id"], ap["name"].lower().replace("_", ""), ap["ip"], ap["mac"].replace(":", "")}:
            return ap["id"]
        if ap["id"] in raw or ap["location"].lower() in raw:
            return ap["id"]
    if raw in {"main", "router", "gateway", "be400", "archer be400", ROUTER_IP} or "be400" in raw:
        return "main"
    if raw in {"unknown", "none"}:
        return "unknown"
    return ""


def looks_like_default_ap(name: str, ip: str, mac: str) -> bool:
    low = clean_text(name, 100).lower()
    mac = normalize_mac(mac)
    for ap in DEFAULT_APS:
        if ip == ap["ip"] or mac == ap["mac"] or low == ap["name"].lower():
            return True
    return "archerax3000pro" in low or ("archer" in low and ("basement" in low or "loft" in low))


def confidence_for(sources: list[str], alias_entry: dict | None) -> str:
    if alias_entry and alias_entry.get("alias"):
        return "manual"
    if any(source in sources for source in ["router", "configured_ap"]):
        return "high"
    if any(source in sources for source in ["adguard", "resolved"]):
        return "medium"
    if "arp" in sources:
        return "observed"
    return "unknown"


def client_glyph(name: str, ip: str, link_type: str, is_mesh: bool = False) -> str:
    low = f"{name} {ip}".lower()
    if is_mesh or "archer" in low or "mesh" in low:
        return "mesh"
    if "raspberry" in low or re.search(r"\bpi[45]?\b", low):
        return "cpu"
    if any(term in low for term in ["iphone", "ipad", "android", "phone", "kindle"]):
        return "phone"
    if any(term in low for term in ["macbook", "laptop", "desktop", "imac", "pc-"]):
        return "laptop"
    if any(term in low for term in ["tv", "roku", "apple tv", "chromecast", "shield"]):
        return "tv"
    if any(term in low for term in ["plug", "bulb", "switch", "camera", "echo", "amazon"]):
        return "iot"
    if normalize_link_type(link_type) == "wired":
        return "plug"
    return "device"


def load_device_aliases() -> dict:
    empty = {"devices": {}, "updatedAt": None}
    if not DEVICE_ALIASES_FILE.exists():
        return empty
    try:
        raw = json.loads(DEVICE_ALIASES_FILE.read_text())
    except Exception:
        return empty
    raw_devices = raw.get("devices", raw) if isinstance(raw, dict) else {}
    if not isinstance(raw_devices, dict):
        return empty
    devices = {}
    for key, entry in raw_devices.items():
        key = str(key or "").strip().lower()
        if not validate_alias_key(key) or not isinstance(entry, dict):
            continue
        link_type = normalize_link_type(entry.get("linkType") or entry.get("interface")) if entry.get("linkType") or entry.get("interface") else ""
        ap_id = normalize_ap_id(entry.get("apId") or entry.get("ap"))
        devices[key] = {
            "alias": clean_text(entry.get("alias") or entry.get("name"), 80),
            "location": clean_text(entry.get("location"), 80),
            "linkType": link_type if link_type in VALID_LINK_TYPES else "",
            "apId": ap_id if ap_id in VALID_AP_IDS else "",
        }
    return {"devices": devices, "updatedAt": raw.get("updatedAt") if isinstance(raw, dict) else None}


def save_device_alias(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("alias payload must be an object")
    key = str(payload.get("key") or payload.get("deviceKey") or "").strip().lower()
    if not validate_alias_key(key):
        raise ValueError("device key must be mac:xx:xx:xx:xx:xx:xx or ip:x.x.x.x")

    current = load_device_aliases()
    devices = current.get("devices", {})
    existing = devices.get(key, {})

    next_entry = {
        "alias": clean_text(payload.get("alias", existing.get("alias", "")), 80),
        "location": clean_text(payload.get("location", existing.get("location", "")), 80),
        "linkType": existing.get("linkType", ""),
        "apId": existing.get("apId", ""),
    }
    if "linkType" in payload or "interface" in payload:
        raw_link = payload.get("linkType") if "linkType" in payload else payload.get("interface")
        if clean_text(raw_link, 40) == "":
            next_entry["linkType"] = ""
        else:
            link_type = normalize_link_type(raw_link)
            next_entry["linkType"] = link_type if link_type in VALID_LINK_TYPES else ""
    if "apId" in payload or "ap" in payload:
        raw_ap = payload.get("apId") if "apId" in payload else payload.get("ap")
        if clean_text(raw_ap, 40) == "":
            next_entry["apId"] = ""
        else:
            ap_id = normalize_ap_id(raw_ap)
            next_entry["apId"] = ap_id if ap_id in VALID_AP_IDS else ""

    if any(next_entry.values()):
        devices[key] = next_entry
    else:
        devices.pop(key, None)

    doc = {"devices": devices, "updatedAt": datetime.now().isoformat()}
    DEVICE_ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEVICE_ALIASES_FILE.with_name(f"{DEVICE_ALIASES_FILE.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    tmp.replace(DEVICE_ALIASES_FILE)
    return doc


class DashboardCache:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.histories = {
            "cpu": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "ram": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "temp": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "ssdPct": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "dnsPerMin": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "containers": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "loadAvg": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "netIn": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "netOut": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "dnsTotal": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "dnsBlocked": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "diskRead": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
            "diskWrite": deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN),
        }
        self.prev_time = time.monotonic()
        self.prev_net = psutil.net_io_counters()
        self.prev_disk = psutil.disk_io_counters()
        self.prev_adguard = None
        self.adguard_client_hints: dict[str, dict] = {}
        self.name_cache: dict[str, dict] = {}
        self.router_topology_doc: dict = {}
        self.router_collector_status: dict = self.empty_router_collector_status("not_polled", "Router collector has not polled yet")
        self.router_collector = TplinkTopologyCollector(
            ROUTER_CREDS_FILE,
            ROUTER_TOPOLOGY_FILE,
            router_name=ROUTER_NAME,
            router_ip=ROUTER_IP,
            aps=DEFAULT_APS,
        )
        self.snapshot_data = self._empty_snapshot()
        psutil.cpu_percent(interval=None)

    def empty_router_collector_status(self, state: str, message: str = "") -> dict:
        return {
            "status": "warn",
            "state": state,
            "lastOkAt": None,
            "lastPollAt": None,
            "lastError": redact(message),
            "endpoints": [],
            "endpointErrors": {},
            "clientCount": 0,
            "elapsedMs": 0,
        }

    def _empty_snapshot(self) -> dict:
        return {
            "HISTORY": {k: list(v) for k, v in self.histories.items()},
            "KPIS": [],
            "SERVICES": [],
            "CONTAINERS": [],
            "ADGUARD": {
                "queries": 0,
                "blocked": 0,
                "blockRatio": 0,
                "upstream": "unknown",
                "topDomains": [],
                "topClients": [],
                "status": "unknown",
            },
            "K3S": {"version": "unknown", "nodes": [], "podsByNs": [], "events": [], "workloads": []},
            "STORAGE": {},
            "TOPOLOGY": {
                "router": {"name": ROUTER_NAME, "ip": ROUTER_IP, "model": "Archer BE400"},
                "host": {"name": "pi4", "ip": LAN_IP},
                "clients": [],
                "counts": {"total": 0, "online": 0, "lan": 0, "wired": 0, "wifi": 0, "mesh": 0, "dnsKnown": 0},
                "aggregateKbps": 0,
                "source": "waiting for LAN collectors",
                "routerCollector": copy.deepcopy(self.router_collector_status),
                "updatedAt": datetime.now().isoformat(),
            },
            "LOGS": [],
            "WEB_APPS": self.web_apps_snapshot(set()),
            "HOST": {},
            "META": {"updatedAt": datetime.now().isoformat(), "source": "pi4-noc-sidecar"},
        }

    def start(self) -> None:
        self.update_hot()
        threading.Thread(target=self._initial_refresh, daemon=True).start()
        for interval, target in [(1, self._hot_loop), (5, self._service_loop), (20, self._heavy_loop)]:
            thread = threading.Thread(target=target, args=(interval,), daemon=True)
            thread.start()

    def _initial_refresh(self) -> None:
        self.update_services()
        self.update_adguard()
        self.update_k3s()
        self.update_router_collector()
        self.update_topology()

    def _hot_loop(self, interval: int) -> None:
        while True:
            time.sleep(interval)
            self.update_hot()

    def _service_loop(self, interval: int) -> None:
        while True:
            time.sleep(interval)
            self.update_services()
            self.update_topology()

    def _heavy_loop(self, interval: int) -> None:
        while True:
            time.sleep(interval)
            self.update_adguard()
            self.update_k3s()
            self.update_router_collector()
            self.update_topology()

    def update_hot(self) -> None:
        now = time.monotonic()
        dt = max(0.1, now - self.prev_time)
        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        load = os.getloadavg()
        temp = self.temperature_c()
        net = psutil.net_io_counters()
        disk = psutil.disk_io_counters()
        net_in = max(0, net.bytes_recv - self.prev_net.bytes_recv) * 8 / dt / 1024
        net_out = max(0, net.bytes_sent - self.prev_net.bytes_sent) * 8 / dt / 1024
        disk_read = max(0, disk.read_bytes - self.prev_disk.read_bytes) / dt / (1024 * 1024)
        disk_write = max(0, disk.write_bytes - self.prev_disk.write_bytes) / dt / (1024 * 1024)
        self.prev_time = now
        self.prev_net = net
        self.prev_disk = disk

        root_usage = psutil.disk_usage("/")
        ssd_usage = psutil.disk_usage("/mnt/ssd") if Path("/mnt/ssd").exists() else root_usage
        with self.lock:
            self.histories["cpu"].append(cpu)
            self.histories["ram"].append(vm.percent)
            self.histories["temp"].append(temp)
            self.histories["ssdPct"].append(ssd_usage.percent)
            self.histories["loadAvg"].append(load[0])
            self.histories["netIn"].append(net_in)
            self.histories["netOut"].append(net_out)
            self.histories["diskRead"].append(disk_read)
            self.histories["diskWrite"].append(disk_write)
            self.snapshot_data["HOST"] = {
                "name": socket.gethostname(),
                "ip": local_ip(),
                "os": platform.platform(),
                "arch": platform.machine(),
                "kernel": platform.release(),
                "uptime": uptime_label(time.time() - psutil.boot_time()),
                "loadAvg": [round(v, 2) for v in load],
                "cpuPct": round(cpu, 1),
                "ramPct": round(vm.percent, 1),
                "ramUsed": round(vm.used / (1024 * 1024)),
                "ramTotal": round(vm.total / (1024 * 1024)),
                "swap": round(swap.percent, 1),
                "tempC": round(temp, 1),
            }
            self.snapshot_data["HISTORY"] = {k: list(v) for k, v in self.histories.items()}
            self.snapshot_data["STORAGE"] = self.storage_snapshot(root_usage, ssd_usage)
            self.snapshot_data["KPIS"] = self.kpis()
            self.snapshot_data["META"] = {"updatedAt": datetime.now().isoformat(), "source": "pi4-noc-sidecar"}

    def temperature_c(self) -> float:
        try:
            temps = psutil.sensors_temperatures()
            for entries in temps.values():
                if entries:
                    return float(entries[0].current)
        except Exception:
            pass
        out = run_text(["/usr/bin/vcgencmd", "measure_temp"], timeout=2)
        m = re.search(r"temp=([0-9.]+)", out)
        return float(m.group(1)) if m else 0.0

    def storage_snapshot(self, root, ssd) -> dict:
        root_gb = max(1, round(root.total / (1024**3)))
        root_used = round(root.used / (1024**3))
        ssd_gb = max(1, round(ssd.total / (1024**3)))
        ssd_used = round(ssd.used / (1024**3))
        podman = self.path_size_gb("/mnt/ssd/podman")
        nas = self.path_size_gb("/mnt/ssd/nas")
        other = max(0, ssd_used - podman - nas)
        return {
            "root": {"used": root_used, "total": root_gb, "fs": "ext4", "mount": "/"},
            "ssd": {
                "used": ssd_used,
                "total": ssd_gb,
                "fs": "ext4",
                "mount": "/mnt/ssd",
                "segments": [
                    {"label": "podman", "value": podman, "tone": "cyan"},
                    {"label": "nas", "value": nas, "tone": "ok"},
                    {"label": "other", "value": other, "tone": "muted"},
                ],
            },
        }

    def path_size_gb(self, path: str) -> int:
        if not Path(path).exists():
            return 0
        out = run_text(["/usr/bin/du", "-sBG", path], timeout=10)
        m = re.match(r"([0-9]+)G", out)
        return int(m.group(1)) if m else 0

    def kpis(self) -> list[dict]:
        host = self.snapshot_data.get("HOST", {})
        storage = self.snapshot_data.get("STORAGE", {})
        adguard = self.snapshot_data.get("ADGUARD", {})
        containers = self.snapshot_data.get("CONTAINERS", [])
        running = sum(1 for c in containers if c.get("status") == "running")
        total = len(containers)
        ssd = storage.get("ssd", {"used": 0, "total": 1})
        ssd_pct = (ssd["used"] / max(1, ssd["total"])) * 100
        return [
            self.kpi("cpu", "CPU", "cpu", host.get("cpuPct", 0), "%", "ok" if host.get("cpuPct", 0) < 85 else "warn", self.histories["cpu"]),
            self.kpi("ram", "Memory", "ram", host.get("ramPct", 0), "%", "ok" if host.get("ramPct", 0) < 85 else "warn", self.histories["ram"], f"{host.get('ramUsed', 0)} / {host.get('ramTotal', 0)} MB"),
            self.kpi("temp", "Temperature", "thermo", host.get("tempC", 0), "°C", "ok" if host.get("tempC", 0) < 70 else "warn", self.histories["temp"]),
            self.kpi("ssd", "SSD Used", "disk", round(ssd_pct, 1), "%", "ok" if ssd_pct < 80 else "warn", self.histories["ssdPct"], f"{ssd.get('used', 0)} / {ssd.get('total', 0)} GB"),
            self.kpi("dns", "DNS / min", "dns", round(self.histories["dnsPerMin"][-1]), "", "ok", self.histories["dnsPerMin"]),
            self.kpi("ctn", "Containers", "containerStack", f"{running}/{total}", "", "ok" if running == total else "warn", self.histories["containers"]),
        ]

    def kpi(self, id_: str, label: str, glyph: str, value, suffix: str, status: str, history, sub: str | None = None) -> dict:
        values = list(history)
        delta = values[-1] - values[-2] if len(values) > 1 and all(isinstance(v, (int, float)) for v in values[-2:]) else 0
        delta_label = f"{delta:+.1f}" if abs(delta) < 10 else f"{delta:+.0f}"
        if suffix:
            delta_label += suffix
        out = {
            "id": id_,
            "label": label,
            "glyph": glyph,
            "value": value,
            "suffix": suffix,
            "status": status,
            "delta": delta_label,
            "deltaTone": "up" if delta > 0 else "down" if delta < 0 else "neutral",
            "history": values,
        }
        if sub:
            out["sub"] = sub
        return out

    def update_services(self) -> None:
        ports = listening_ports()
        services = []
        for cfg in UNIT_CONFIG:
            show = service_show(cfg["unit"])
            active = show.get("ActiveState", "unknown")
            sub = show.get("SubState", "unknown")
            port_ok = all(p in ports for p in cfg["ports"]) if cfg["ports"] else active in {"active", "activating"}
            ok = active == "active" and port_ok
            svc = {
                "id": cfg["id"],
                "label": cfg["label"],
                "unit": cfg["unit"],
                "port": " · ".join(cfg["ports"]) if cfg["ports"] else "—",
                "status": "ok" if ok else "warn" if active in {"active", "activating"} else "fail",
                "glyph": cfg["glyph"],
                "activeState": active,
                "subState": sub,
                "pid": show.get("MainPID", ""),
                "restarts": int(show.get("NRestarts") or 0),
            }
            if cfg.get("ui"):
                svc["ui"] = cfg["ui"]
            services.append(svc)

        containers = self.collect_containers()
        web_app_ports = ports | k3s_host_ports()
        web_apps = self.web_apps_snapshot(web_app_ports)
        with self.lock:
            self.snapshot_data["SERVICES"] = services
            self.snapshot_data["CONTAINERS"] = containers
            self.snapshot_data["WEB_APPS"] = web_apps
            self.histories["containers"].append(sum(1 for c in containers if c.get("status") == "running"))
            self.snapshot_data["KPIS"] = self.kpis()
            self.snapshot_data["LOGS"] = self.recent_log_cards()

    def web_apps_snapshot(self, ports: set[str]) -> list[dict]:
        apps = []
        for cfg in WEB_APP_CONFIG:
            port = str(cfg["port"])
            ok = port in ports
            apps.append(
                {
                    **cfg,
                    "status": "ok" if ok else "warn",
                    "statusLabel": "listening" if ok else "not listening",
                }
            )
        return apps

    def collect_containers(self) -> list[dict]:
        ps_rows = run_privileged_json(["podman_ps"], timeout=15, default=[]) or []
        stat_rows = run_privileged_json(["podman_stats"], timeout=15, default=[]) or []
        stats_by_name = {str(row.get("name") or row.get("Name")): row for row in stat_rows if row.get("name") or row.get("Name")}
        containers = []
        for row in ps_rows:
            names = row.get("Names") or []
            name = str(names[0] if names else row.get("Name") or row.get("Id") or "unknown")
            image = row.get("Image") or row.get("ImageName") or "unknown"
            if row.get("IsInfra") or "podman-pause" in str(image):
                continue
            stat = stats_by_name.get(name, {})
            state = str(row.get("State") or "").lower()
            status = "running" if state == "running" or row.get("Status", "").startswith("Up") else state or "unknown"
            mem_usage, _ = parse_bytes_pair(stat.get("mem_usage") or stat.get("MemUsage") or "0 / 0")
            containers.append(
                {
                    "id": name,
                    "label": name,
                    "image": image,
                    "status": status,
                    "uptime": row.get("Status") or uptime_label(max(0, time.time() - float(row.get("StartedAt") or row.get("Started") or time.time()))),
                    "restarts": int(row.get("Restarts") or 0),
                    "cpu": safe_float(stat.get("cpu_percent") or stat.get("CPU") or 0),
                    "mem": round(mem_usage),
                    "status_tone": status_tone(status),
                }
            )
        return containers

    def recent_log_cards(self) -> list[dict]:
        cards = []
        for svc in self.snapshot_data.get("SERVICES", [])[:4]:
            cards.append({"t": datetime.now().strftime("%H:%M:%S"), "src": svc["id"], "level": svc["status"], "msg": f"{svc['unit']} {svc['activeState']}/{svc['subState']}"})
        return cards

    def update_router_collector(self) -> None:
        topology_doc, status = self.router_collector.poll()
        with self.lock:
            if topology_doc:
                self.router_topology_doc = topology_doc
            self.router_collector_status = status

    def update_topology(self) -> None:
        topology = self.collect_topology()
        with self.lock:
            self.snapshot_data["TOPOLOGY"] = topology

    def collect_topology(self) -> dict:
        with self.lock:
            host = copy.deepcopy(self.snapshot_data.get("HOST", {}))
            adguard_hints = copy.deepcopy(self.adguard_client_hints)
            history = copy.deepcopy(self.snapshot_data.get("HISTORY", {}))
            router_live = copy.deepcopy(self.router_topology_doc)
            router_collector = copy.deepcopy(self.router_collector_status)

        host_ip = host.get("ip") or local_ip()
        gateway = default_gateway()
        router_file = router_live or self.read_router_topology_file()
        alias_doc = load_device_aliases()
        alias_devices = alias_doc.get("devices", {})
        now_iso = datetime.now().isoformat()
        router = {
            "name": router_file.get("router", {}).get("name") or ROUTER_NAME,
            "ip": router_file.get("router", {}).get("ip") or gateway or ROUTER_IP,
            "mac": normalize_mac(router_file.get("router", {}).get("mac")),
            "model": router_file.get("router", {}).get("model") or "Archer BE400",
            "id": "main",
        }
        clients: dict[str, dict] = {}
        aliases: dict[str, str] = {}

        def keys_for(item: dict) -> list[str]:
            keys = []
            mac = normalize_mac(item.get("mac"))
            ip = item.get("ip")
            name = str(item.get("name") or "").strip().lower()
            if mac:
                keys.append(f"mac:{mac}")
            if is_ipv4(ip):
                keys.append(f"ip:{ip}")
            if name:
                keys.append(f"name:{name}")
            return keys

        def alias_for(item: dict) -> tuple[str, dict]:
            mac_key = device_key(mac=item.get("mac"))
            ip_key = device_key(ip=item.get("ip"))
            for key in [mac_key, ip_key]:
                if key and key in alias_devices:
                    return key, alias_devices[key]
            return mac_key or ip_key or str(item.get("key") or ""), {}

        def upsert(item: dict, source: str) -> None:
            item = {**item, "mac": normalize_mac(item.get("mac"))}
            item_keys = keys_for(item)
            if not item_keys:
                return
            key = next((aliases[k] for k in item_keys if k in aliases), item_keys[0])
            existing = clients.get(key)
            rank = NAME_SOURCE_RANK.get(source, 0)
            link_rank = LINK_SOURCE_RANK.get(source, 0)
            incoming_name = clean_text(item.get("sourceName") or item.get("name"), 80)
            usable_name = incoming_name if incoming_name and not is_ipv4(incoming_name) and not normalize_mac(incoming_name) else ""
            link_type = normalize_link_type(item.get("linkType") or item.get("interface"))
            ap_id = normalize_ap_id(
                item.get("apId")
                or item.get("ap")
                or item.get("accessPoint")
                or item.get("connectedTo")
                or item.get("parent")
                or item.get("parentName")
            )
            is_ap = bool(item.get("isAp") or looks_like_default_ap(usable_name, item.get("ip", ""), item.get("mac", "")))
            if not existing:
                existing = {
                    "id": key,
                    "key": device_key(item.get("ip", ""), item.get("mac", "")) or key,
                    "sourceName": usable_name,
                    "displayName": usable_name or item.get("ip") or item.get("mac") or "unknown",
                    "name": usable_name or item.get("ip") or item.get("mac") or "unknown",
                    "ip": item.get("ip", ""),
                    "mac": item.get("mac", ""),
                    "linkType": link_type,
                    "interface": link_label(link_type),
                    "apId": ap_id,
                    "location": clean_text(item.get("location"), 80),
                    "state": item.get("state", ""),
                    "online": bool(item.get("online", False)),
                    "rxKbps": safe_float(item.get("rxKbps")),
                    "txKbps": safe_float(item.get("txKbps")),
                    "queryCount": safe_int(item.get("queryCount")),
                    "isMesh": bool(item.get("isMesh", False) or is_ap),
                    "isAp": is_ap,
                    "sources": [],
                    "lastSeen": item.get("lastSeen") or (now_iso if item.get("online") else ""),
                    "_nameRank": rank if usable_name else 0,
                    "_linkRank": link_rank if link_type != "unknown" else 0,
                    "_apRank": rank if ap_id else 0,
                    "_locationRank": rank if item.get("location") else 0,
                }
                clients[key] = existing
            else:
                if usable_name and (rank >= existing.get("_nameRank", 0) or not existing.get("sourceName")):
                    existing["sourceName"] = usable_name
                    existing["_nameRank"] = rank
                for field in ["ip", "mac", "state"]:
                    if item.get(field) and not existing.get(field):
                        existing[field] = item[field]
                if item.get("lastSeen") or item.get("online"):
                    existing["lastSeen"] = item.get("lastSeen") or now_iso
                if link_type != "unknown" and (link_rank >= existing.get("_linkRank", 0) or existing.get("linkType") in {"", "unknown"}):
                    existing["linkType"] = link_type
                    existing["interface"] = link_label(link_type)
                    existing["_linkRank"] = link_rank
                if ap_id and (rank >= existing.get("_apRank", 0) or not existing.get("apId")):
                    existing["apId"] = ap_id
                    existing["_apRank"] = rank
                location = clean_text(item.get("location"), 80)
                if location and (rank >= existing.get("_locationRank", 0) or not existing.get("location")):
                    existing["location"] = location
                    existing["_locationRank"] = rank
                existing["online"] = bool(existing.get("online") or item.get("online"))
                existing["rxKbps"] = max(safe_float(existing.get("rxKbps")), safe_float(item.get("rxKbps")))
                existing["txKbps"] = max(safe_float(existing.get("txKbps")), safe_float(item.get("txKbps")))
                existing["queryCount"] = max(safe_int(existing.get("queryCount")), safe_int(item.get("queryCount")))
                existing["isAp"] = bool(existing.get("isAp") or is_ap)
                existing["isMesh"] = bool(existing.get("isMesh") or item.get("isMesh") or is_ap)

            if source not in existing["sources"]:
                existing["sources"].append(source)
            for alias in item_keys:
                aliases[alias] = key

        for ap in DEFAULT_APS:
            upsert(
                {
                    "name": ap["name"],
                    "ip": ap["ip"],
                    "mac": ap["mac"],
                    "interface": "wired",
                    "apId": ap["id"],
                    "location": ap["location"],
                    "online": False,
                    "isAp": True,
                    "isMesh": True,
                    "state": "configured",
                },
                "configured_ap",
            )

        def router_item(row: dict, is_ap: bool = False) -> dict:
            name = row.get("name") or row.get("hostname") or row.get("id") or row.get("deviceName")
            return {
                "name": name,
                "ip": row.get("ip") or row.get("ipaddr") or row.get("ipAddress"),
                "mac": row.get("mac") or row.get("macaddr") or row.get("mac_addr") or row.get("macAddress"),
                "interface": row.get("interface") or row.get("band") or row.get("network") or row.get("linkType"),
                "apId": row.get("apId") or row.get("ap") or row.get("accessPoint") or row.get("connectedTo") or row.get("parent") or row.get("parentName"),
                "location": row.get("location"),
                "online": row.get("online", True),
                "rxKbps": row.get("rxKbps") or row.get("rx_rate") or row.get("downloadKbps") or row.get("downKbps"),
                "txKbps": row.get("txKbps") or row.get("tx_rate") or row.get("uploadKbps") or row.get("upKbps"),
                "isAp": is_ap or row.get("isAp") or row.get("apNode") or looks_like_default_ap(str(name or ""), str(row.get("ip") or ""), str(row.get("mac") or "")),
                "isMesh": row.get("isMesh") or row.get("mesh") or is_ap,
                "state": row.get("state", "router"),
                "lastSeen": row.get("lastSeen") or router_file.get("updatedAt"),
            }

        for row in router_file.get("aps", []) or []:
            if isinstance(row, dict):
                upsert(router_item(row, is_ap=True), "router")

        for row in router_file.get("clients", []):
            if isinstance(row, dict):
                upsert(router_item(row), "router")

        for hint in adguard_hints.values():
            upsert(hint, "adguard")

        for neighbor in self.collect_neighbors():
            upsert(neighbor, "arp")

        resolved = 0
        for row in list(clients.values()):
            if resolved >= 2:
                break
            if row.get("sourceName"):
                continue
            hint = self.resolve_client_name(row.get("ip", ""), row.get("mac", ""))
            if hint.get("name"):
                upsert({"ip": row.get("ip"), "mac": row.get("mac"), "name": hint["name"], "online": row.get("online")}, "resolved")
                resolved += 1

        ap_rows = []
        client_rows = []
        router_ip = router.get("ip")
        for row in clients.values():
            if row.get("ip") in {router_ip, host_ip}:
                continue
            key, alias_entry = alias_for(row)
            row["key"] = key or row.get("key") or device_key(row.get("ip"), row.get("mac"))
            source_name = clean_text(row.get("sourceName"), 80)
            display_name = clean_text(alias_entry.get("alias") or source_name or row.get("ip") or row.get("mac") or "unknown", 80)
            if alias_entry.get("linkType"):
                row["linkType"] = normalize_link_type(alias_entry["linkType"])
                row["interface"] = link_label(row["linkType"])
            if alias_entry.get("apId"):
                row["apId"] = normalize_ap_id(alias_entry["apId"])
            if alias_entry.get("location"):
                row["location"] = alias_entry["location"]
            link_type = normalize_link_type(row.get("linkType") or row.get("interface"))
            is_mesh = bool(row.get("isMesh") or looks_like_default_ap(display_name, row.get("ip", ""), row.get("mac", "")) or "mesh" in display_name.lower())
            row["id"] = row["key"] or row.get("id")
            row["sourceName"] = source_name
            row["displayName"] = display_name
            row["alias"] = alias_entry.get("alias", "")
            row["manualLocation"] = alias_entry.get("location", "")
            row["manualLinkType"] = alias_entry.get("linkType", "")
            row["manualApId"] = alias_entry.get("apId", "")
            row["name"] = display_name
            row["linkType"] = link_type
            row["interface"] = link_label(link_type)
            row["isMesh"] = is_mesh
            row["isAp"] = bool(row.get("isAp") or looks_like_default_ap(display_name, row.get("ip", ""), row.get("mac", "")))
            if row["isAp"] and not row.get("apId"):
                row["apId"] = normalize_ap_id(display_name)
            row["glyph"] = "mesh" if row["isAp"] else client_glyph(display_name, row.get("ip", ""), link_type, is_mesh)
            row["tone"] = "ok" if row.get("online") else "warn"
            row["rxKbps"] = round(safe_float(row.get("rxKbps")), 1)
            row["txKbps"] = round(safe_float(row.get("txKbps")), 1)
            row["confidence"] = confidence_for(row.get("sources", []), alias_entry)
            row["sourceText"] = " + ".join(row.get("sources", [])) or "unknown"
            row["location"] = clean_text(row.get("location"), 80)
            for private_key in ["_nameRank", "_linkRank", "_apRank", "_locationRank"]:
                row.pop(private_key, None)
            if row["isAp"]:
                ap_rows.append(row)
            else:
                row["groupId"] = self.topology_group_id(row)
                client_rows.append(row)

        client_rows.sort(
            key=lambda row: (
                0 if row.get("online") else 1,
                -safe_float(row.get("rxKbps")) - safe_float(row.get("txKbps")) - min(safe_int(row.get("queryCount")), 100000) / 1000,
                ip_sort_key(row.get("ip")),
                str(row.get("name", "")).lower(),
            )
        )
        ap_rows.sort(key=lambda row: ["basement", "loft", "unknown", ""].index(row.get("apId", "")) if row.get("apId", "") in {"basement", "loft", "unknown", ""} else 9)
        net_in = (history.get("netIn") or [0])[-1]
        net_out = (history.get("netOut") or [0])[-1]
        group_defs = self.topology_groups(client_rows)
        counts = {
            "total": len(client_rows),
            "known": len(client_rows) + len(ap_rows),
            "online": sum(1 for row in client_rows if row.get("online")),
            "aps": len(ap_rows),
            "apsOnline": sum(1 for row in ap_rows if row.get("online")),
            "lan": sum(1 for row in client_rows if row.get("linkType") == "lan-observed"),
            "wired": sum(1 for row in client_rows if row.get("linkType") == "wired"),
            "wifi": sum(1 for row in client_rows if row.get("linkType") in {"wifi", "2.4g", "5g", "6g"}),
            "unknown": sum(1 for row in client_rows if row.get("linkType") in {"unknown", "lan-observed"}),
            "mesh": sum(1 for row in client_rows if row.get("isMesh")),
            "dnsKnown": sum(1 for row in client_rows if "adguard" in row.get("sources", [])),
        }
        source_bits = ["Pi ARP/neighbors"]
        if adguard_hints:
            source_bits.append("AdGuard clients")
        if router_file.get("clients"):
            source_bits.append("TP-Link router/APs" if router_collector.get("state") == "live" else "router cache")
        if alias_devices:
            source_bits.append("dashboard aliases")
        return {
            "router": router,
            "host": {
                "name": host.get("name") or socket.gethostname(),
                "ip": host_ip,
                "linkType": "wired",
                "interface": "Wired",
                "glyph": "cpu",
                "id": "pi4",
            },
            "aps": ap_rows,
            "clients": client_rows[:96],
            "groups": group_defs,
            "nodes": [
                {"type": "router", **router},
                {"id": "pi4", "type": "host", "name": host.get("name") or socket.gethostname(), "ip": host_ip, "linkType": "wired"},
                *[{"type": "ap", **ap, "id": ap.get("apId") or ap.get("key")} for ap in ap_rows],
            ],
            "links": [
                {"source": router.get("id", "main"), "target": "pi4", "type": "wired"},
                *[{"source": router.get("id", "main"), "target": ap.get("apId") or ap.get("key"), "type": "wired"} for ap in ap_rows],
            ],
            "counts": counts,
            "aggregateKbps": round(safe_float(net_in) + safe_float(net_out), 1),
            "source": " + ".join(source_bits),
            "routerCollector": router_collector,
            "updatedAt": datetime.now().isoformat(),
        }

    def topology_group_id(self, row: dict) -> str:
        ap_id = normalize_ap_id(row.get("apId"))
        link_type = normalize_link_type(row.get("linkType") or row.get("interface"))
        if ap_id in {"basement", "loft"}:
            return ap_id
        if link_type == "wired":
            return "wired"
        if link_type in {"wifi", "2.4g", "5g", "6g"}:
            return "wifi-unknown"
        return "unknown"

    def topology_groups(self, clients: list[dict]) -> list[dict]:
        labels = {
            "wired": "Wired LAN",
            "basement": "Basement AP",
            "loft": "Loft AP",
            "wifi-unknown": "Wi-Fi AP unknown",
            "unknown": "Link unknown",
        }
        order = ["wired", "basement", "loft", "wifi-unknown", "unknown"]
        groups = []
        for group_id in order:
            rows = [row for row in clients if row.get("groupId") == group_id]
            groups.append(
                {
                    "id": group_id,
                    "label": labels[group_id],
                    "count": len(rows),
                    "online": sum(1 for row in rows if row.get("online")),
                    "rxKbps": round(sum(safe_float(row.get("rxKbps")) for row in rows), 1),
                    "txKbps": round(sum(safe_float(row.get("txKbps")) for row in rows), 1),
                }
            )
        return groups

    def read_router_topology_file(self) -> dict:
        if not ROUTER_TOPOLOGY_FILE.exists():
            return {}
        try:
            data = json.loads(ROUTER_TOPOLOGY_FILE.read_text())
            if isinstance(data, list):
                return {"clients": data}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def resolve_client_name(self, ip: str, mac: str = "") -> dict:
        if not is_ipv4(ip):
            return {}
        cache_key = device_key(ip, mac) or f"ip:{ip}"
        cached = self.name_cache.get(cache_key)
        if cached and time.time() - cached.get("at", 0) < 3600:
            return cached.get("value", {})

        def remember(value: dict) -> dict:
            self.name_cache[cache_key] = {"at": time.time(), "value": value}
            return value

        candidates = []
        out = run_text(["/usr/bin/getent", "hosts", ip], timeout=1)
        fields = out.split()
        if len(fields) >= 2 and fields[0] == ip:
            candidates.extend(fields[1:])

        avahi = shutil.which("avahi-resolve-address")
        if avahi:
            out = run_text([avahi, ip], timeout=1)
            parts = out.split()
            if len(parts) >= 2 and parts[0] == ip:
                candidates.append(parts[1])

        nmblookup = shutil.which("nmblookup") if NETBIOS_NAME_LOOKUPS else None
        if nmblookup:
            out = run_text([nmblookup, "-A", ip], timeout=2)
            for line in out.splitlines():
                match = re.match(r"\s*([A-Za-z0-9_. -]{1,32})\s+<00>\s+-\s+<UNIQUE>", line)
                if match:
                    candidates.append(match.group(1))

        for candidate in candidates:
            name = clean_text(candidate.split(".local")[0].rstrip("."), 80)
            if name and not is_ipv4(name) and name.lower() not in {"localhost", "broadcasthost"}:
                return remember({"name": name})
        return remember({})

    def collect_neighbors(self) -> list[dict]:
        rows = []
        raw = run_json([ip_cmd(), "-j", "neigh", "show"], timeout=3, default=None)
        if isinstance(raw, list):
            for item in raw:
                ip = item.get("dst")
                if not is_ipv4(ip):
                    continue
                state = item.get("state", "")
                state_text = " ".join(state) if isinstance(state, list) else str(state)
                mac = normalize_mac(item.get("lladdr"))
                online = bool(mac) and state_text.upper() not in {"FAILED", "INCOMPLETE"}
                if mac or online:
                    rows.append(
                        {
                            "ip": ip,
                            "mac": mac,
                            "interface": "LAN",
                            "state": state_text or "neighbor",
                            "online": online,
                        }
                    )
            return rows

        out = run_text([ip_cmd(), "neigh", "show"], timeout=3)
        for line in out.splitlines():
            match = re.match(r"(\d+\.\d+\.\d+\.\d+)\s+dev\s+(\S+)(?:\s+lladdr\s+([0-9a-f:.-]+))?.*?\s(\S+)$", line, re.I)
            if not match:
                continue
            ip, dev, mac, state = match.groups()
            mac = normalize_mac(mac)
            online = bool(mac) and state.upper() not in {"FAILED", "INCOMPLETE"}
            if mac or online:
                rows.append({"ip": ip, "mac": mac, "interface": "LAN", "state": state, "online": online})
        if rows:
            return rows

        arp_path = Path("/proc/net/arp")
        if arp_path.exists():
            for line in arp_path.read_text().splitlines()[1:]:
                parts = line.split()
                if len(parts) < 6:
                    continue
                ip, _, flags, mac, _, dev = parts[:6]
                mac = normalize_mac(mac)
                if not is_ipv4(ip) or not mac:
                    continue
                rows.append({"ip": ip, "mac": mac, "interface": "LAN", "state": flags, "online": flags != "0x0"})
        return rows

    def collect_adguard_client_hints(self, stats: dict) -> dict[str, dict]:
        hints: dict[str, dict] = {}

        def add(identity: str, **fields) -> None:
            identity = str(identity or "").strip()
            if not identity:
                return
            mac = normalize_mac(identity)
            ip = identity if is_ipv4(identity) else fields.get("ip", "")
            key = f"mac:{mac}" if mac else f"ip:{ip}" if is_ipv4(ip) else f"name:{identity.lower()}"
            item = hints.setdefault(key, {"name": "", "ip": "", "mac": "", "interface": "DNS", "online": False, "queryCount": 0})
            if mac:
                item["mac"] = mac
            if is_ipv4(ip):
                item["ip"] = ip
            if fields.get("name"):
                item["name"] = str(fields["name"])
            elif not item.get("name") and not is_ipv4(identity) and not mac:
                item["name"] = identity
            item["online"] = bool(item.get("online") or fields.get("online"))
            item["queryCount"] = max(safe_int(item.get("queryCount")), safe_int(fields.get("queryCount")))

        for item in top_items(stats.get("top_clients"), limit=80, include_ip=True):
            add(item.get("ip") or item.get("name"), name=item.get("name"), queryCount=item.get("count", 0), online=False)

        try:
            clients_doc = adguard_api("/control/clients")
        except Exception:
            return hints
        if not isinstance(clients_doc, dict):
            return hints

        for row in clients_doc.get("clients", []) or []:
            if not isinstance(row, dict):
                continue
            name = row.get("name") or row.get("client_id") or ""
            for identity in row.get("ids", []) or []:
                add(identity, name=name, online=False)
        for row in clients_doc.get("auto_clients", []) or []:
            if not isinstance(row, dict):
                continue
            whois = row.get("whois_info") if isinstance(row.get("whois_info"), dict) else {}
            name = row.get("name") or whois.get("orgname") or ""
            identity = row.get("ip") or row.get("name")
            add(identity, name=name, ip=row.get("ip"), online=False)
        return hints

    def update_adguard(self) -> None:
        try:
            status = adguard_api("/control/status")
            stats = adguard_api("/control/stats")
            queries = int(stats.get("num_dns_queries") or 0)
            blocked = int(stats.get("num_blocked_filtering") or 0)
            now = time.monotonic()
            if self.prev_adguard:
                prev_time, prev_queries, prev_blocked = self.prev_adguard
                dt_min = max((now - prev_time) / 60, 0.1)
                qpm = max(0, (queries - prev_queries) / dt_min)
                bpm = max(0, (blocked - prev_blocked) / dt_min)
            else:
                qpm = (stats.get("dns_queries") or [0])[-1] / 60 if stats.get("dns_queries") else 0
                bpm = (stats.get("blocked_filtering") or [0])[-1] / 60 if stats.get("blocked_filtering") else 0
            self.prev_adguard = (now, queries, blocked)
            upstreams = top_items(stats.get("top_upstreams_responses"), limit=1)
            upstream = upstreams[0]["name"] if upstreams else "unknown"
            adguard = {
                "queries": queries,
                "blocked": blocked,
                "blockRatio": round((blocked / queries) * 100, 1) if queries else 0,
                "upstream": upstream.replace("https://", ""),
                "topDomains": top_items(stats.get("top_blocked_domains"), limit=5),
                "topClients": top_items(stats.get("top_clients"), limit=5, include_ip=True),
                "status": "ok" if status.get("running") and status.get("protection_enabled") else "warn",
                "version": status.get("version", "unknown"),
                "avgProcessingMs": round(float(stats.get("avg_processing_time") or 0) * 1000, 2),
            }
            client_hints = self.collect_adguard_client_hints(stats)
        except Exception as exc:
            adguard = {
                **self.snapshot_data.get("ADGUARD", {}),
                "status": "fail",
                "error": redact(str(exc)),
            }
            qpm = bpm = 0
            client_hints = {}

        with self.lock:
            self.histories["dnsPerMin"].append(qpm)
            self.histories["dnsTotal"].append(qpm)
            self.histories["dnsBlocked"].append(bpm)
            self.adguard_client_hints = client_hints
            self.snapshot_data["ADGUARD"] = adguard
            self.snapshot_data["KPIS"] = self.kpis()

    def update_k3s(self) -> None:
        nodes_json = run_json(["/usr/local/bin/kubectl", "get", "nodes", "-o", "json"], timeout=12, default={"items": []}) or {"items": []}
        pods_json = run_json(["/usr/local/bin/kubectl", "get", "pods", "-A", "-o", "json"], timeout=12, default={"items": []}) or {"items": []}
        events_json = run_json(["/usr/local/bin/kubectl", "get", "events", "-A", "--sort-by=.lastTimestamp", "-o", "json"], timeout=12, default={"items": []}) or {"items": []}
        workloads_json = run_json(["/usr/local/bin/kubectl", "get", "deploy,statefulset,daemonset", "-A", "-o", "json"], timeout=12, default={"items": []}) or {"items": []}

        nodes = []
        version = "unknown"
        for item in nodes_json.get("items", []):
            info = item.get("status", {}).get("nodeInfo", {})
            version = info.get("kubeletVersion", version)
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in item.get("status", {}).get("conditions", []))
            roles = item.get("metadata", {}).get("labels", {})
            role_names = [k.rsplit("/", 1)[-1] for k in roles if k.startswith("node-role.kubernetes.io/")]
            nodes.append(
                {
                    "name": item.get("metadata", {}).get("name", "unknown"),
                    "role": ",".join(role_names) or "node",
                    "version": info.get("kubeletVersion", "unknown"),
                    "ready": ready,
                    "age": self.age_label(item.get("metadata", {}).get("creationTimestamp")),
                }
            )

        ns_counts: dict[str, Counter] = {}
        for item in pods_json.get("items", []):
            ns = item.get("metadata", {}).get("namespace", "default")
            phase = str(item.get("status", {}).get("phase", "Unknown")).lower()
            ns_counts.setdefault(ns, Counter())[phase] += 1
        pods_by_ns = [
            {"ns": ns, "running": counts["running"], "pending": counts["pending"], "failed": counts["failed"]}
            for ns, counts in sorted(ns_counts.items())
        ]

        events = []
        for item in events_json.get("items", [])[-8:][::-1]:
            last = item.get("lastTimestamp") or item.get("eventTime") or item.get("metadata", {}).get("creationTimestamp")
            involved = item.get("involvedObject", {})
            events.append(
                {
                    "t": self.time_label(last),
                    "kind": item.get("type", "Normal"),
                    "reason": item.get("reason", "Event"),
                    "obj": f"{involved.get('kind', '').lower()}/{involved.get('name', '')}".strip("/"),
                    "msg": item.get("message", ""),
                }
            )

        workloads = []
        for item in workloads_json.get("items", []):
            meta = item.get("metadata", {})
            ns = meta.get("namespace", "default")
            kind = item.get("kind", "Workload").lower()
            workloads.append(
                {
                    "namespace": ns,
                    "kind": kind,
                    "name": meta.get("name", "unknown"),
                    "ready": item.get("status", {}).get("readyReplicas", 0),
                    "desired": item.get("status", {}).get("replicas", item.get("spec", {}).get("replicas", 1)),
                    "rolloutAllowed": ns not in PROTECTED_NAMESPACES and kind in {"deployment", "statefulset", "daemonset"},
                }
            )

        with self.lock:
            self.snapshot_data["K3S"] = {
                "version": version,
                "nodes": nodes,
                "podsByNs": pods_by_ns,
                "events": events,
                "workloads": workloads,
            }

    def time_label(self, timestamp: str | None) -> str:
        if not timestamp:
            return datetime.now().strftime("%H:%M:%S")
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone()
            return dt.strftime("%H:%M:%S")
        except Exception:
            return timestamp[:8]

    def age_label(self, timestamp: str | None) -> str:
        if not timestamp:
            return "?"
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
            return uptime_label(time.time() - dt)
        except Exception:
            return "?"

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.snapshot_data)

    def known_container(self, name: str) -> bool:
        with self.lock:
            return any(c.get("id") == name for c in self.snapshot_data.get("CONTAINERS", []))

    def known_workload(self, namespace: str, kind: str, name: str) -> bool:
        with self.lock:
            return any(
                w.get("namespace") == namespace and w.get("kind") == kind and w.get("name") == name and w.get("rolloutAllowed")
                for w in self.snapshot_data.get("K3S", {}).get("workloads", [])
            )


class ActionJobs:
    def __init__(self, cache: DashboardCache) -> None:
        self.cache = cache
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}

    def create(self, payload: dict) -> dict:
        self.validate(payload)
        job = {
            "id": uuid.uuid4().hex[:12],
            "label": self.label(payload),
            "status": "queued",
            "createdAt": datetime.now().isoformat(),
            "output": "",
            "error": "",
        }
        with self.lock:
            self.jobs[job["id"]] = job
        threading.Thread(target=self._run, args=(job["id"], payload), daemon=True).start()
        return copy.deepcopy(job)

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return copy.deepcopy(job) if job else None

    def validate(self, payload: dict) -> None:
        type_ = payload.get("type")
        action = payload.get("action")
        if type_ == "systemd":
            unit = payload.get("unit")
            if action != "restart" or unit not in ALLOWED_UNITS:
                raise ValueError("systemd action is not allowlisted")
            return
        if type_ == "podman":
            if action == "start_all":
                return
            name = payload.get("container", "")
            if action not in {"start", "stop", "restart"} or not SAFE_NAME_RE.match(name) or not self.cache.known_container(name):
                raise ValueError("podman action is not allowlisted")
            return
        if type_ == "k3s":
            namespace = payload.get("namespace", "")
            kind = payload.get("kind", "")
            name = payload.get("name", "")
            if (
                action != "rollout_restart"
                or namespace in PROTECTED_NAMESPACES
                or kind not in {"deployment", "statefulset", "daemonset"}
                or not all(SAFE_NAME_RE.match(v) for v in [namespace, kind, name])
                or not self.cache.known_workload(namespace, kind, name)
            ):
                raise ValueError("k3s action is not allowlisted")
            return
        if type_ == "adguard" and action in {"refresh_filters", "clear_cache"}:
            return
        raise ValueError("action is not allowlisted")

    def label(self, payload: dict) -> str:
        type_ = payload.get("type")
        if type_ == "systemd":
            return f"restart {payload.get('unit')}"
        if type_ == "podman":
            return "start all containers" if payload.get("action") == "start_all" else f"{payload.get('action')} {payload.get('container')}"
        if type_ == "k3s":
            return f"rollout restart {payload.get('namespace')}/{payload.get('name')}"
        if type_ == "adguard":
            return payload.get("action", "adguard").replace("_", " ")
        return "action"

    def _run(self, job_id: str, payload: dict) -> None:
        self._update(job_id, status="running", startedAt=datetime.now().isoformat())
        try:
            output = self.execute(payload)
            self._update(job_id, status="succeeded", finishedAt=datetime.now().isoformat(), output=redact(output))
            threading.Thread(target=self._refresh_after_action, daemon=True).start()
        except Exception as exc:
            self._update(job_id, status="failed", finishedAt=datetime.now().isoformat(), error=redact(str(exc)), output=redact(str(exc)))

    def _update(self, job_id: str, **fields) -> None:
        with self.lock:
            self.jobs[job_id].update(fields)

    def _refresh_after_action(self) -> None:
        time.sleep(1)
        self.cache.update_services()
        self.cache.update_adguard()
        self.cache.update_k3s()

    def execute(self, payload: dict) -> str:
        type_ = payload.get("type")
        action = payload.get("action")
        if type_ == "systemd":
            proc = run_privileged(["systemd_restart", payload["unit"]], timeout=45)
            if proc.returncode != 0:
                raise RuntimeError(proc.stdout.strip())
            return proc.stdout or f"{payload['unit']} restarted"
        if type_ == "podman":
            if action == "start_all":
                proc = run_privileged(["podman_start_all"], timeout=60)
            else:
                proc = run_privileged(["podman_action", action, payload["container"]], timeout=60)
            if proc.returncode != 0:
                raise RuntimeError(proc.stdout.strip())
            return proc.stdout or "podman action completed"
        if type_ == "k3s":
            proc = run_cmd(
                [
                    "/usr/local/bin/kubectl",
                    "rollout",
                    "restart",
                    f"{payload['kind']}/{payload['name']}",
                    "-n",
                    payload["namespace"],
                ],
                timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stdout.strip())
            return proc.stdout or "rollout restart requested"
        if type_ == "adguard":
            if action == "refresh_filters":
                adguard_api("/control/filtering/refresh", method="POST")
                return "AdGuard filter refresh requested"
            if action == "clear_cache":
                adguard_api("/control/cache_clear", method="POST")
                return "AdGuard DNS cache cleared"
        raise ValueError("unsupported action")


cache = DashboardCache()
jobs = ActionJobs(cache)
app = Flask(__name__, static_folder=str(DIST_DIR), static_url_path="")
app.config.update(
    SECRET_KEY=load_session_secret(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PI4_NOC_SESSION_SECURE", "0").lower() in {"1", "true", "yes"},
)


@app.before_request
def require_api_auth():
    if request.path.startswith("/api/") and request.path not in AUTH_EXEMPT_API_PATHS and not authenticated():
        return jsonify({"authenticated": False, "error": "authentication required"}), 401


@app.after_request
def no_buffering(resp):
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.get("/api/session")
def api_session():
    return jsonify({"authenticated": authenticated(), "user": session.get("user") if authenticated() else None})


@app.post("/api/login")
def api_login():
    payload = request.get_json(force=True, silent=True) or {}
    password = str(payload.get("password") or "")
    key = login_client_key()
    throttle_login_attempt(key)
    if not verify_login_password(password):
        record_login_failure(key)
        return jsonify({"authenticated": False, "error": "invalid password"}), 401
    clear_login_failures(key)
    session.clear()
    session["authenticated"] = True
    session["user"] = AUTH_USER
    session["login_at"] = datetime.now().isoformat()
    return jsonify({"authenticated": True, "user": AUTH_USER})


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"authenticated": False})


@app.get("/api/snapshot")
def api_snapshot():
    return jsonify(cache.snapshot())


@app.get("/api/events")
def api_events():
    def stream():
        while True:
            yield f"data: {json.dumps(cache.snapshot(), separators=(',', ':'))}\n\n"
            time.sleep(1)

    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@app.get("/api/logs")
def api_logs():
    source_type = request.args.get("sourceType", "")
    id_ = request.args.get("id", "")
    lines = max(1, min(int(request.args.get("lines", "160")), 600))
    namespace = request.args.get("namespace", "")
    container = request.args.get("container", "")

    if source_type == "systemd":
        if id_ not in ALLOWED_UNITS:
            return jsonify({"error": "unit is not allowlisted"}), 400
        text = run_privileged_text(["journal", id_, str(lines)], timeout=20)
        return jsonify({"title": f"{id_} logs", "hint": "journalctl", "text": text})

    if source_type == "podman":
        if not SAFE_NAME_RE.match(id_) or not cache.known_container(id_):
            return jsonify({"error": "container is not allowlisted"}), 400
        text = run_privileged_text(["podman_logs", id_, str(lines)], timeout=20)
        return jsonify({"title": f"{id_} logs", "hint": "podman logs", "text": text})

    if source_type == "k3s-events":
        events = cache.snapshot().get("K3S", {}).get("events", [])
        text = "\n".join(f"{e['t']} {e['reason']} {e['obj']} {e['msg']}" for e in events) or "No k3s events in cache."
        return jsonify({"title": "k3s events", "hint": "cached kubectl events", "text": text})

    if source_type == "k3s-pod":
        if not all(SAFE_NAME_RE.match(v) for v in [namespace, id_]):
            return jsonify({"error": "pod reference is invalid"}), 400
        argv = ["/usr/local/bin/kubectl", "logs", "-n", namespace, id_, "--tail", str(lines)]
        if container and SAFE_NAME_RE.match(container):
            argv.extend(["-c", container])
        text = run_text(argv, timeout=20)
        return jsonify({"title": f"{namespace}/{id_} logs", "hint": "kubectl logs", "text": text})

    return jsonify({"error": "unsupported log source"}), 400


@app.post("/api/actions")
def api_actions():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        job = jobs.create(payload)
    except Exception as exc:
        return jsonify({"error": redact(str(exc))}), 400
    return jsonify(job), 202


@app.get("/api/action-jobs/<job_id>")
def api_action_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


@app.get("/api/topology/aliases")
def api_topology_aliases():
    return jsonify(load_device_aliases())


@app.post("/api/topology/aliases")
def api_topology_aliases_update():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        doc = save_device_alias(payload)
        cache.update_topology()
    except Exception as exc:
        return jsonify({"error": redact(str(exc))}), 400
    return jsonify(doc)


@app.get("/")
def index():
    return send_from_directory(DIST_DIR, "index.html")


@app.get("/<path:path>")
def static_or_index(path: str):
    target = DIST_DIR / path
    if target.exists() and target.is_file():
        return send_from_directory(DIST_DIR, path)
    return send_from_directory(DIST_DIR, "index.html")


def parse_args():
    parser = argparse.ArgumentParser(description="Cabrera Network dashboard sidecar")
    parser.add_argument("--host", default=os.environ.get("PI4_NOC_HOST", "0.0.0.0"))
    parser.add_argument("--port", default=int(os.environ.get("PI4_NOC_PORT", "80")), type=int)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cache.start()
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
