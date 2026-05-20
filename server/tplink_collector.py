from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import random
import re
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlencode


SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret|apikey|api_key|authorization|stok|sign|data)([=: ]+)(\S+)")
MAC_RE = re.compile(r"^(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.I)

SHA256_CERTS = {"RG", "SG_L1_S2", "CE_RED", "ANATEL", "IMDA TS RG-SEC", "SG CLS L1 STAGE2", "EU CE RED", "Brazil ANATEL"}
REPLACE_HASH_CERTS = {"SG_L1_S2", "SG CLS L1 STAGE2"}
RSA_OAEP_CERTS = {"SG_L1_S2", "SG CLS L1 STAGE2"}

READ_ENDPOINTS = [
    ("gameDevices", "/admin/smart_network?form=game_accelerator", {"operation": "loadDevice"}),
    ("gameSpeeds", "/admin/smart_network?form=game_accelerator", {"operation": "loadSpeed"}),
    ("hostInfo", "/admin/smart_network?form=get_host_info", {"operation": "read"}),
    ("meshClients", "/admin/easymesh_network?form=mesh_sclient_list_all", {"operation": "read"}),
    ("meshDevices", "/admin/easymesh_network?form=get_mesh_device_list_all", {"operation": "read"}),
]
AP_READ_ENDPOINTS = READ_ENDPOINTS[:2]


class RouterCredentialsError(RuntimeError):
    pass


class RouterApiError(RuntimeError):
    pass


def redact(text: str) -> str:
    return SECRET_RE.sub(r"\1\2<redacted>", str(text or ""))


def clean_text(value: Any, max_len: int = 100) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def normalize_mac(value: Any) -> str:
    if not value:
        return ""
    raw = str(value).strip().lower()
    compact = re.sub(r"[^0-9a-f]", "", raw)
    if len(compact) == 12:
        raw = ":".join(compact[index : index + 2] for index in range(0, 12, 2))
    else:
        raw = raw.replace("-", ":")
    if not MAC_RE.match(raw) or raw == "00:00:00:00:00:00":
        return ""
    return raw


def is_ipv4(value: Any) -> bool:
    if not value:
        return False
    try:
        ipaddress.ip_address(str(value))
        return "." in str(value)
    except ValueError:
        return False


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def is_private_lan_ip(value: str) -> bool:
    if not is_ipv4(value):
        return False
    try:
        addr = ipaddress.ip_address(value)
        return bool(addr.is_private and not addr.is_loopback and not addr.is_multicast)
    except ValueError:
        return False


def read_router_credentials(path: Path) -> dict[str, str]:
    if not path.exists():
        raise RouterCredentialsError(f"router credentials missing at {path}")
    st = path.stat()
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise RouterCredentialsError(f"router credentials at {path} must be mode 0600")
    creds: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        creds[key.strip()] = value.strip()
    creds.setdefault("url", "http://192.168.0.1")
    creds.setdefault("username", "admin")
    if not creds.get("password"):
        raise RouterCredentialsError("router credentials file must include password")
    return creds


def certification_set(device_config: dict[str, Any]) -> set[str]:
    raw = device_config.get("certification") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(item) for item in raw if item}


def serialize_form(data: dict[str, Any]) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in data.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        elif isinstance(value, (dict, list)):
            pairs.append((key, json.dumps(value, separators=(",", ":"))))
        else:
            pairs.append((key, str(value)))
    return urlencode(pairs)


class TplinkEncryptor:
    def __init__(
        self,
        password_hash: str,
        sequence: int,
        rsa_key: list[str],
        *,
        replace_hash: bool = False,
        rsa_oaep: bool = False,
    ) -> None:
        self.password_hash = password_hash
        self.sequence = int(sequence)
        self.rsa_key = rsa_key
        self.replace_hash = replace_hash
        self.rsa_oaep = rsa_oaep
        self.key = "".join(str(random.randint(0, 9)) for _ in range(16))
        self.iv = "".join(str(random.randint(0, 9)) for _ in range(16))

    @property
    def aes_formatted_key(self) -> str:
        return f"k={self.key}&i={self.iv}"

    def encrypt(self, payload: str, *, with_aes_key: bool, token_valid: bool) -> dict[str, str]:
        encrypted = self._aes_encrypt(payload)
        if self.replace_hash and token_valid and not with_aes_key:
            self.password_hash = hashlib.sha256(encrypted.encode()).hexdigest()
        return {
            "sign": self._signature(self.sequence + len(encrypted), with_aes_key=with_aes_key),
            "data": encrypted,
        }

    def decrypt(self, payload: str) -> dict[str, Any]:
        raw = self._aes_decrypt(payload)
        return json.loads(raw or "{}")

    def _signature(self, sequence_value: int, *, with_aes_key: bool) -> str:
        text = f"h={self.password_hash}&s={sequence_value}"
        if with_aes_key:
            text = f"{self.aes_formatted_key}&{text}"
        parts = [text[index : index + 53] for index in range(0, len(text), 53)]
        if with_aes_key:
            return "".join(rsa_encrypt(part, self.rsa_key, oaep=self.rsa_oaep) for part in parts)
        return "".join(hmac.new(self.aes_formatted_key.encode(), part.encode(), hashlib.sha256).hexdigest() for part in parts)

    def _aes_encrypt(self, payload: str) -> str:
        try:
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except Exception as exc:
            raise RouterApiError(f"cryptography dependency unavailable: {exc}") from exc

        padder = padding.PKCS7(128).padder()
        padded = padder.update(payload.encode()) + padder.finalize()
        cipher = Cipher(algorithms.AES(self.key.encode()), modes.CBC(self.iv.encode()))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(ciphertext).decode()

    def _aes_decrypt(self, payload: str) -> str:
        try:
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except Exception as exc:
            raise RouterApiError(f"cryptography dependency unavailable: {exc}") from exc

        ciphertext = base64.b64decode(payload)
        cipher = Cipher(algorithms.AES(self.key.encode()), modes.CBC(self.iv.encode()))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        raw = unpadder.update(padded) + unpadder.finalize()
        return raw.decode()


def rsa_encrypt(payload: str, key_pair: list[str], *, oaep: bool = False) -> str:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except Exception as exc:
        raise RouterApiError(f"cryptography dependency unavailable: {exc}") from exc

    if len(key_pair) < 2:
        raise RouterApiError("router RSA key response is incomplete")
    n_hex, e_hex = str(key_pair[0]), str(key_pair[1])
    public_key = rsa.RSAPublicNumbers(int(e_hex, 16), int(n_hex, 16)).public_key()
    pad = (
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA1()), algorithm=hashes.SHA1(), label=None)
        if oaep
        else padding.PKCS1v15()
    )
    encrypted = public_key.encrypt(payload.encode(), pad)
    return encrypted.hex().zfill(len(n_hex))


class TplinkClient:
    def __init__(self, url: str, username: str, password: str, timeout: float = 4.0) -> None:
        try:
            import requests
        except Exception as exc:
            raise RouterApiError(f"requests dependency unavailable: {exc}") from exc

        self.requests = requests
        self.base_url = url.rstrip("/")
        self.username = username or "admin"
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.token = ""
        self.device_config: dict[str, Any] = {}
        self.encryptor: TplinkEncryptor | None = None

    def login(self) -> None:
        self.device_config = self.plain_read("/device_config?form=config")
        certs = certification_set(self.device_config)
        auth = self.plain_read("/login?form=auth")
        password_keys = self.plain_read("/login?form=keys")
        login_hash = self._login_hash(certs)
        self.encryptor = TplinkEncryptor(
            login_hash,
            auth.get("seq") or auth.get("sequence") or 0,
            auth.get("key") or auth.get("keys") or [],
            replace_hash=bool(certs & REPLACE_HASH_CERTS),
            rsa_oaep=bool(certs & RSA_OAEP_CERTS),
        )
        password_key = password_keys.get("password") or password_keys.get("key") or []
        encrypted_password = rsa_encrypt(self.password, password_key, oaep=False)
        response = self.encrypted_request(
            "/login?form=login",
            {"operation": "login", "password": encrypted_password, "confirm": True},
            with_aes_key=True,
            token="",
        )
        token = response.get("stok") or response.get("token")
        if not token:
            raise RouterApiError("router login response did not include a session token")
        self.token = str(token)

    def _login_hash(self, certs: set[str]) -> str:
        raw = f"{self.username}{self.password}".encode()
        if certs & SHA256_CERTS:
            return hashlib.sha256(raw).hexdigest()
        return hashlib.md5(raw).hexdigest()

    def plain_read(self, path: str) -> dict[str, Any]:
        return self._post(path, {"operation": "read"}, encrypt=False)

    def request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            self.login()
        return self.encrypted_request(path, payload)

    def encrypted_request(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        with_aes_key: bool = False,
        token: str | None = None,
    ) -> dict[str, Any]:
        if not self.encryptor:
            raise RouterApiError("router encryptor is not initialized")
        encrypted = self.encryptor.encrypt(serialize_form(payload), with_aes_key=with_aes_key, token_valid=bool(self.token))
        return self._post(path, encrypted, encrypt=True, token=token)

    def _post(self, path: str, data: dict[str, Any], *, encrypt: bool, token: str | None = None) -> dict[str, Any]:
        stok = self.token if token is None else token
        path = path if path.startswith("/") else f"/{path}"
        url = f"{self.base_url}/cgi-bin/luci/;stok={stok}{path}"
        try:
            resp = self.session.post(
                url,
                data=serialize_form(data),
                timeout=self.timeout,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
            )
            resp.raise_for_status()
            doc = resp.json()
        except Exception as exc:
            raise RouterApiError(redact(str(exc))) from exc
        return self._unwrap_response(doc, encrypt=encrypt)

    def _unwrap_response(self, doc: Any, *, encrypt: bool) -> dict[str, Any]:
        if not isinstance(doc, dict):
            raise RouterApiError("router response was not an object")
        if encrypt and isinstance(doc.get("data"), str):
            if not self.encryptor:
                raise RouterApiError("encrypted router response arrived before encryptor initialization")
            doc = self.encryptor.decrypt(doc["data"])
        if doc.get("success") is False:
            error_code = doc.get("errorCode") or doc.get("error") or doc.get("errorcode") or "router request failed"
            raise RouterApiError(redact(str(error_code)))
        data = doc.get("data")
        if isinstance(data, dict):
            return data
        return doc


def link_type_from(value: Any) -> str:
    raw = clean_text(value, 60).lower().replace("_", "-")
    if not raw:
        return ""
    if raw in {"wired", "wire", "ethernet", "lan"} or raw.startswith(("eth", "lan-")):
        return "wired"
    if raw in {"2g", "2.4g", "2.4ghz", "iot-2.4g", "iot-2g"}:
        return "2.4g"
    if raw in {"5g", "5ghz", "5g1", "5g2", "5g-1", "5g-2", "iot-5g", "iot-5g1", "iot-5g2"}:
        return "5g"
    if raw in {"6g", "6ghz"}:
        return "6g"
    if raw in {"mlo", "wifi", "wireless", "wlan"}:
        return "wifi"
    if raw == "offline":
        return "unknown"
    if raw in {"unknown", "unknow", "none", "n/a", "--"}:
        return ""
    return ""


def first_value(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in {None, ""}:
            return row.get(key)
    return ""


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def ap_id_from_value(value: Any, aps: list[dict[str, str]]) -> str:
    raw = clean_text(value, 100).lower()
    mac = normalize_mac(raw)
    compact = re.sub(r"[^a-z0-9.:-]+", "", raw)
    for ap in aps:
        if raw in {ap["id"], ap["name"].lower(), ap["ip"], ap["mac"]}:
            return ap["id"]
        if mac and mac == ap["mac"]:
            return ap["id"]
        if compact in {ap["id"], ap["name"].lower().replace("_", ""), ap["ip"], ap["mac"].replace(":", "")}:
            return ap["id"]
        if ap["id"] in raw or ap["location"].lower() in raw:
            return ap["id"]
    if raw in {"main", "router", "gateway", "be400", "archer be400"} or "be400" in raw:
        return "main"
    return ""


def normalize_client_row(row: dict[str, Any], aps: list[dict[str, str]]) -> dict[str, Any] | None:
    mac = normalize_mac(first_value(row, ["mac", "macaddr", "mac_addr", "macAddress", "client_mac", "clientMac", "key"]))
    ip = clean_text(first_value(row, ["ip", "ipaddr", "ipAddress", "client_ip", "clientIp"]), 64)
    if ip == "0.0.0.0" or not is_private_lan_ip(ip):
        ip = ""
    name = clean_text(first_value(row, ["deviceName", "name", "hostname", "hostName", "alias", "clientName", "model"]), 80)
    link = ""
    for key in ["networkMode", "deviceTag", "interface", "wireType", "wire_type", "band", "mode", "accessMode"]:
        link = link_type_from(row.get(key))
        if link:
            break
    signal = first_value(row, ["signal", "rssi", "RSSI"])
    if not link and signal not in {None, ""}:
        link = "wifi"
    ap_id = ""
    for key in [
        "apId",
        "ap",
        "accessPoint",
        "connectedTo",
        "parent",
        "parentName",
        "agent_mac",
        "agentMac",
        "parent_mac",
        "parentMac",
        "slave_mac",
        "slaveMac",
        "ap_mac",
        "apMac",
        "bssid",
    ]:
        ap_id = ap_id_from_value(row.get(key), aps)
        if ap_id:
            break
    device_tag = clean_text(row.get("deviceTag"), 40).lower()
    online = bool(row.get("online") is True or row.get("isOnline") is True or row.get("status") == "connected")
    if device_tag and device_tag != "offline":
        online = True
    if ip or mac:
        online = online or device_tag != "offline"
    if not (mac or ip or name):
        return None
    item = {
        "name": name,
        "ip": ip,
        "mac": mac,
        "interface": link,
        "apId": ap_id,
        "online": online,
        "rxKbps": first_value(row, ["downloadSpeed", "downSpeed", "down_speed", "rxKbps", "rx_rate"]),
        "txKbps": first_value(row, ["uploadSpeed", "upSpeed", "up_speed", "txKbps", "tx_rate"]),
        "rxRate": first_value(row, ["rxrate", "rxRate"]),
        "txRate": first_value(row, ["txrate", "txRate"]),
        "signal": signal,
        "lastSeen": datetime.now().isoformat() if online else "",
    }
    return item


def merge_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"mac:{row['mac']}" if row.get("mac") else f"ip:{row.get('ip')}" if row.get("ip") else f"name:{row.get('name', '').lower()}"
        if not key:
            continue
        current = merged.setdefault(key, {})
        for field, value in row.items():
            if value is None or value == "":
                continue
            if field in {"rxKbps", "txKbps"}:
                current[field] = max(safe_float(current.get(field)), safe_float(value))
            elif field == "online":
                current[field] = bool(current.get(field) or value)
            elif not current.get(field):
                current[field] = value
    return list(merged.values())


def row_key(row: dict[str, Any]) -> str:
    mac = normalize_mac(row.get("mac"))
    ip = clean_text(row.get("ip"), 64)
    name = clean_text(row.get("name"), 80).lower()
    if mac:
        return f"mac:{mac}"
    if ip:
        return f"ip:{ip}"
    if name:
        return f"name:{name}"
    return ""


def merge_client_row(current: dict[str, Any], incoming: dict[str, Any], *, prefer_incoming: bool) -> dict[str, Any]:
    preferred_fields = {"interface", "apId", "lastSeen", "rxRate", "txRate", "signal"}
    for field, value in incoming.items():
        if value is None or value == "":
            continue
        if field in {"rxKbps", "txKbps"}:
            current[field] = max(safe_float(current.get(field)), safe_float(value))
        elif field == "online":
            current[field] = bool(current.get(field) or value)
        elif field in preferred_fields and prefer_incoming:
            current[field] = value
        elif field in {"ip", "mac", "name"}:
            if not current.get(field):
                current[field] = value
        elif not current.get(field):
            current[field] = value
    return current


def merge_router_topologies(base: dict[str, Any], incoming: dict[str, Any], *, prefer_incoming_clients: bool = True) -> dict[str, Any]:
    merged = {
        **base,
        "aps": merge_rows([*(base.get("aps") or []), *(incoming.get("aps") or [])]),
        "clients": [],
        "updatedAt": datetime.now().isoformat(),
    }
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in base.get("clients") or []:
        key = row_key(row)
        if not key:
            continue
        by_key[key] = dict(row)
        order.append(key)
    for row in incoming.get("clients") or []:
        key = row_key(row)
        if not key:
            continue
        if key not in by_key:
            by_key[key] = {}
            order.append(key)
        merge_client_row(by_key[key], row, prefer_incoming=prefer_incoming_clients)
    merged["clients"] = [by_key[key] for key in order]
    return merged


def normalize_router_topology(
    docs: dict[str, Any],
    *,
    router_name: str,
    router_ip: str,
    aps: list[dict[str, str]],
    default_ap_id: str = "",
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for doc in docs.values():
        for candidate in walk_dicts(doc):
            row = normalize_client_row(candidate, aps)
            if row:
                rows.append(row)

    merged = merge_rows(rows)
    ap_rows: list[dict[str, Any]] = []
    client_rows: list[dict[str, Any]] = []
    for row in merged:
        row.setdefault("name", "")
        row.setdefault("ip", "")
        row.setdefault("mac", "")
        row.setdefault("interface", "")
        row.setdefault("apId", "")
        row.setdefault("rxKbps", 0)
        row.setdefault("txKbps", 0)
        row.setdefault("online", False)
        matched_ap = next((ap for ap in aps if row.get("mac") == ap["mac"] or row.get("ip") == ap["ip"] or clean_text(row.get("name")).lower() == ap["name"].lower()), None)
        if matched_ap:
            row.update(
                {
                    "name": row.get("name") or matched_ap["name"],
                    "ip": row.get("ip") or matched_ap["ip"],
                    "mac": row.get("mac") or matched_ap["mac"],
                    "apId": matched_ap["id"],
                    "location": matched_ap["location"],
                    "interface": "wired",
                    "isAp": True,
                    "isMesh": True,
                    "online": True,
                }
            )
            ap_rows.append(row)
        elif row.get("ip") != router_ip:
            if default_ap_id and not row.get("apId"):
                row["apId"] = default_ap_id
            client_rows.append(row)

    return {
        "router": {"name": router_name, "ip": router_ip, "model": "Archer BE400"},
        "aps": ap_rows,
        "clients": client_rows,
        "updatedAt": datetime.now().isoformat(),
    }


class TplinkTopologyCollector:
    def __init__(
        self,
        credentials_path: Path,
        cache_path: Path,
        *,
        router_name: str,
        router_ip: str,
        aps: list[dict[str, str]],
        client_factory=TplinkClient,
        timeout: float = 4.0,
    ) -> None:
        self.credentials_path = credentials_path
        self.cache_path = cache_path
        self.router_name = router_name
        self.router_ip = router_ip
        self.aps = aps
        self.client_factory = client_factory
        self.timeout = timeout
        self.last_good: dict[str, Any] = self._read_cache()
        self.last_ok_at = self.last_good.get("updatedAt") if isinstance(self.last_good, dict) else None

    def poll(self) -> tuple[dict[str, Any], dict[str, Any]]:
        started = time.monotonic()
        endpoints_used: list[str] = []
        endpoint_errors: dict[str, str] = {}
        ap_client_counts: dict[str, int] = {}
        try:
            creds = read_router_credentials(self.credentials_path)
            client = self.client_factory(creds["url"], creds.get("username", "admin"), creds["password"], timeout=self.timeout)
            if hasattr(client, "login"):
                client.login()
            docs: dict[str, Any] = {}
            for endpoint_id, path, payload in READ_ENDPOINTS:
                try:
                    docs[endpoint_id] = client.request(path, payload)
                    endpoints_used.append(endpoint_id)
                except Exception as exc:
                    endpoint_errors[endpoint_id] = redact(str(exc))
            if not docs:
                raise RouterApiError("; ".join(endpoint_errors.values()) or "no router read endpoints succeeded")
            topology = normalize_router_topology(docs, router_name=self.router_name, router_ip=self.router_ip, aps=self.aps)
            for ap in self.aps:
                ap_id = ap.get("id", "")
                if not ap_id or not ap.get("ip"):
                    continue
                ap_docs: dict[str, Any] = {}
                try:
                    ap_client = self.client_factory(self._ap_url(creds["url"], ap), creds.get("username", "admin"), creds["password"], timeout=self.timeout)
                    if hasattr(ap_client, "login"):
                        ap_client.login()
                    for endpoint_id, path, payload in AP_READ_ENDPOINTS:
                        namespaced_endpoint = f"{ap_id}:{endpoint_id}"
                        try:
                            ap_docs[endpoint_id] = ap_client.request(path, payload)
                            endpoints_used.append(namespaced_endpoint)
                        except Exception as exc:
                            endpoint_errors[namespaced_endpoint] = redact(str(exc))
                except Exception as exc:
                    endpoint_errors[f"{ap_id}:login"] = redact(str(exc))
                if ap_docs:
                    ap_topology = normalize_router_topology(
                        ap_docs,
                        router_name=ap.get("name") or ap_id,
                        router_ip=ap.get("ip") or "",
                        aps=self.aps,
                        default_ap_id=ap_id,
                    )
                    ap_client_counts[ap_id] = len(ap_topology.get("clients", []))
                    topology = merge_router_topologies(topology, ap_topology, prefer_incoming_clients=True)
            self.last_good = topology
            self.last_ok_at = topology.get("updatedAt")
            self._write_cache(topology)
            status = self._status(
                "ok",
                "live",
                endpoints_used=endpoints_used,
                endpoint_errors=endpoint_errors,
                client_count=len(topology.get("clients", [])),
                ap_client_counts=ap_client_counts,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            return topology, status
        except Exception as exc:
            state = "missing_credentials" if isinstance(exc, RouterCredentialsError) else "unavailable"
            if self.last_good:
                state = "stale"
            status = self._status(
                "warn" if self.last_good else "fail",
                state,
                endpoints_used=endpoints_used,
                endpoint_errors=endpoint_errors,
                last_error=redact(str(exc)),
                client_count=len(self.last_good.get("clients", [])) if isinstance(self.last_good, dict) else 0,
                ap_client_counts=ap_client_counts,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            return self.last_good if isinstance(self.last_good, dict) else {}, status

    def _ap_url(self, router_url: str, ap: dict[str, str]) -> str:
        if ap.get("url"):
            return str(ap["url"]).rstrip("/")
        parsed = urlparse(str(router_url or "http://192.168.0.1"))
        scheme = parsed.scheme or "http"
        return f"{scheme}://{ap['ip']}"

    def _status(
        self,
        status: str,
        state: str,
        *,
        endpoints_used: list[str],
        endpoint_errors: dict[str, str],
        client_count: int,
        elapsed_ms: int,
        ap_client_counts: dict[str, int] | None = None,
        last_error: str = "",
    ) -> dict[str, Any]:
        return {
            "status": status,
            "state": state,
            "lastOkAt": self.last_ok_at,
            "lastPollAt": datetime.now().isoformat(),
            "lastError": redact(last_error),
            "endpoints": endpoints_used,
            "endpointErrors": {key: redact(value) for key, value in endpoint_errors.items()},
            "clientCount": client_count,
            "apClientCounts": ap_client_counts or {},
            "elapsedMs": elapsed_ms,
        }

    def _read_cache(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_cache(self, topology: dict[str, Any]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_name(f"{self.cache_path.name}.tmp")
            tmp.write_text(json.dumps(topology, indent=2, sort_keys=True) + "\n")
            os.chmod(tmp, 0o600)
            tmp.replace(self.cache_path)
        except Exception:
            pass
