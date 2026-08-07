"""Small, dependency-free client for the local k3s inventory API.

The dashboard only needs read-only list operations.  This client obtains the
same administrator credentials used by the local kubectl command, loads the
client certificate and key through anonymous Linux memfd files, and then talks
to the API server directly.  Construction is intentionally side-effect free;
configuration and TLS state are created on the first inventory request.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import ssl
import subprocess
import threading
from dataclasses import dataclass, field
from http import client as httpclient
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit


KUBECTL_CONFIG_COMMAND = (
    "/usr/local/bin/kubectl",
    "config",
    "view",
    "--raw",
    "-o",
    "json",
)
KUBECTL_CONFIG_ENV = {
    "K3S_CONFIG_FILE": "/dev/null",
    "KUBECONFIG": "/etc/rancher/k3s/k3s.yaml",
}

# The order is stable so callers receive the same combined document shape as
# `kubectl get nodes,pods,events,deployments,statefulsets,daemonsets -o json`.
INVENTORY_ENDPOINTS = (
    ("nodes", "/api/v1/nodes", "Node"),
    ("pods", "/api/v1/pods", "Pod"),
    ("events", "/api/v1/events", "Event"),
    ("deployments", "/apis/apps/v1/deployments", "Deployment"),
    ("statefulsets", "/apis/apps/v1/statefulsets", "StatefulSet"),
    ("daemonsets", "/apis/apps/v1/daemonsets", "DaemonSet"),
)

DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = 5.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 4.0
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class K3sClientError(RuntimeError):
    """Base class for safe-to-report direct-client failures."""


class K3sBootstrapError(K3sClientError):
    """kubectl configuration or TLS initialization failed."""


class K3sRequestError(K3sClientError):
    """A Kubernetes API request failed."""


class K3sResponseError(K3sClientError):
    """A Kubernetes API response did not have the expected list shape."""


class _RetryableRequestError(K3sRequestError):
    """Transport/authentication failure that may be fixed by cert rotation."""


@dataclass(frozen=True)
class _Credentials:
    server: str
    ca_data: bytes = field(repr=False)
    client_certificate_data: bytes = field(repr=False)
    client_key_data: bytes = field(repr=False)


@dataclass(frozen=True)
class _Connection:
    server: str
    opener: Any = field(repr=False)


def _bounded_timeout(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return min(max(parsed, minimum), maximum)


def _configuration_error(detail: str) -> K3sBootstrapError:
    # Detail strings are constants chosen by this module.  Never include the
    # kubectl document, subprocess output, URLs, or certificate material.
    return K3sBootstrapError(f"Kubernetes configuration is malformed ({detail})")


def _named_entry(document: dict[str, Any], collection: str, name: str, value_key: str) -> dict[str, Any]:
    entries = document.get(collection)
    if not isinstance(entries, list):
        raise _configuration_error(f"invalid {collection}")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("name") != name:
            continue
        value = entry.get(value_key)
        if isinstance(value, dict):
            return value
        break
    raise _configuration_error(f"missing {value_key}")


def _decode_data(value: Any, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise _configuration_error(f"missing {label}")
    if len(value) > MAX_CONFIG_BYTES * 2:
        raise _configuration_error(f"oversized {label}")
    compact = "".join(value.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise _configuration_error(f"invalid {label}") from None
    if not decoded or len(decoded) > MAX_CONFIG_BYTES:
        raise _configuration_error(f"invalid {label}")
    return decoded


def _parse_kubeconfig(document: Any) -> _Credentials:
    if not isinstance(document, dict):
        raise _configuration_error("invalid document")

    current_context = document.get("current-context")
    if not isinstance(current_context, str) or not current_context:
        raise _configuration_error("missing current context")
    context = _named_entry(document, "contexts", current_context, "context")

    cluster_name = context.get("cluster")
    user_name = context.get("user")
    if not isinstance(cluster_name, str) or not cluster_name:
        raise _configuration_error("missing cluster reference")
    if not isinstance(user_name, str) or not user_name:
        raise _configuration_error("missing user reference")

    cluster = _named_entry(document, "clusters", cluster_name, "cluster")
    user = _named_entry(document, "users", user_name, "user")

    server = cluster.get("server")
    if not isinstance(server, str) or not server or any(char.isspace() for char in server):
        raise _configuration_error("invalid server")
    try:
        parsed_server = urlsplit(server)
        # Accessing port performs additional bracket and range validation.
        parsed_server.port
    except ValueError:
        raise _configuration_error("invalid server") from None
    if (
        parsed_server.scheme != "https"
        or not parsed_server.hostname
        or parsed_server.username is not None
        or parsed_server.password is not None
        or parsed_server.query
        or parsed_server.fragment
    ):
        raise _configuration_error("invalid server")

    return _Credentials(
        server=server.rstrip("/"),
        ca_data=_decode_data(cluster.get("certificate-authority-data"), "certificate authority"),
        client_certificate_data=_decode_data(user.get("client-certificate-data"), "client certificate"),
        client_key_data=_decode_data(user.get("client-key-data"), "client key"),
    )


def _create_memfd(name: str) -> int:
    creator = getattr(os, "memfd_create", None)
    if creator is None:
        raise K3sBootstrapError("Linux memfd support is required for Kubernetes client credentials")
    try:
        return creator(name, getattr(os, "MFD_CLOEXEC", 0))
    except (OSError, TypeError):
        raise K3sBootstrapError("Unable to create anonymous Kubernetes credential files") from None


def _write_memfd(fd: int, content: bytes) -> None:
    os.fchmod(fd, 0o600)
    view = memoryview(content)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("short memfd write")
        written += count
    os.lseek(fd, 0, os.SEEK_SET)


def _ca_data_for_ssl(ca_data: bytes) -> str | bytes:
    if b"-----BEGIN CERTIFICATE-----" not in ca_data:
        return ca_data
    try:
        return ca_data.decode("ascii")
    except UnicodeDecodeError:
        raise K3sBootstrapError("Unable to initialize verified Kubernetes TLS") from None


def _build_ssl_context(credentials: _Credentials) -> ssl.SSLContext:
    fds: list[int] = []
    try:
        context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH,
            cadata=_ca_data_for_ssl(credentials.ca_data),
        )
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        if hasattr(ssl, "TLSVersion"):
            context.minimum_version = ssl.TLSVersion.TLSv1_2

        cert_fd = _create_memfd("k3s-client-certificate")
        fds.append(cert_fd)
        _write_memfd(cert_fd, credentials.client_certificate_data)

        key_fd = _create_memfd("k3s-client-key")
        fds.append(key_fd)
        _write_memfd(key_fd, credentials.client_key_data)

        context.load_cert_chain(
            certfile=f"/proc/self/fd/{cert_fd}",
            keyfile=f"/proc/self/fd/{key_fd}",
        )
        return context
    except K3sBootstrapError:
        raise
    except (OSError, ssl.SSLError, TypeError, ValueError):
        raise K3sBootstrapError("Unable to initialize verified Kubernetes TLS") from None
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass


def _default_opener(context: ssl.SSLContext) -> Any:
    # The API server is local infrastructure.  Explicitly bypass ambient proxy
    # settings while preserving urllib's normal HTTP status handling.
    return urlrequest.build_opener(
        urlrequest.ProxyHandler({}),
        urlrequest.HTTPSHandler(context=context),
    )


class K3sClient:
    """Lazy, reusable read-only client for the six dashboard inventory lists.

    Transport and authentication failures invalidate the cached TLS state and
    trigger one fresh kubectl bootstrap, covering normal k3s certificate
    rotation.  All public failures derive from :class:`K3sClientError`, so a
    caller can safely fall back to its existing kubectl collector.
    """

    def __init__(
        self,
        *,
        bootstrap_timeout: float = DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        command_runner: Callable[..., Any] | None = None,
        opener_factory: Callable[[ssl.SSLContext], Any] | None = None,
    ) -> None:
        self.bootstrap_timeout = _bounded_timeout(bootstrap_timeout, DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS, 1.0, 30.0)
        self.request_timeout = _bounded_timeout(request_timeout, DEFAULT_REQUEST_TIMEOUT_SECONDS, 0.5, 15.0)
        self._command_runner = command_runner or subprocess.run
        self._opener_factory = opener_factory or _default_opener
        self._connection: _Connection | None = None
        self._bootstrap_lock = threading.Lock()

    def close(self) -> None:
        """Discard cached TLS state; the next request bootstraps again."""

        self.invalidate_credentials()

    def invalidate_credentials(self) -> None:
        """Force a fresh kubeconfig read before the next inventory request."""

        with self._bootstrap_lock:
            self._connection = None

    def get_inventory(self) -> dict[str, list[dict[str, Any]]]:
        """Return a kubectl-compatible combined inventory document."""

        for attempt in range(2):
            connection = self._get_connection()
            try:
                return self._query_inventory(connection)
            except _RetryableRequestError:
                self._invalidate_if_current(connection)
                if attempt:
                    raise
        # The loop always returns or raises, but this keeps the type contract
        # explicit for static checkers.
        raise K3sRequestError("Kubernetes API inventory request failed")

    def _get_connection(self) -> _Connection:
        connection = self._connection
        if connection is not None:
            return connection
        with self._bootstrap_lock:
            if self._connection is None:
                self._connection = self._bootstrap()
            return self._connection

    def _invalidate_if_current(self, connection: _Connection) -> None:
        with self._bootstrap_lock:
            if self._connection is connection:
                self._connection = None

    def _bootstrap(self) -> _Connection:
        env = dict(os.environ)
        env.update(KUBECTL_CONFIG_ENV)
        try:
            result = self._command_runner(
                list(KUBECTL_CONFIG_COMMAND),
                capture_output=True,
                text=True,
                check=False,
                timeout=self.bootstrap_timeout,
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            raise K3sBootstrapError("Unable to read local Kubernetes client configuration") from None

        if getattr(result, "returncode", 1) != 0:
            raise K3sBootstrapError("Unable to read local Kubernetes client configuration")
        stdout = getattr(result, "stdout", "")
        if not isinstance(stdout, str) or not stdout or len(stdout.encode("utf-8", errors="ignore")) > MAX_CONFIG_BYTES:
            raise K3sBootstrapError("Unable to read local Kubernetes client configuration")
        try:
            document = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            raise K3sBootstrapError("Unable to parse local Kubernetes client configuration") from None

        credentials = _parse_kubeconfig(document)
        context = _build_ssl_context(credentials)
        try:
            opener = self._opener_factory(context)
        except Exception:
            raise K3sBootstrapError("Unable to initialize Kubernetes API transport") from None
        return _Connection(server=credentials.server, opener=opener)

    def _query_inventory(self, connection: _Connection) -> dict[str, list[dict[str, Any]]]:
        combined: list[dict[str, Any]] = []
        for label, path, expected_kind in INVENTORY_ENDPOINTS:
            combined.extend(self._query_list(connection, label, path, expected_kind))
        return {"items": combined}

    def _query_list(
        self,
        connection: _Connection,
        label: str,
        path: str,
        expected_kind: str,
    ) -> list[dict[str, Any]]:
        request = urlrequest.Request(
            f"{connection.server}{path}",
            headers={
                "Accept": "application/json",
                "User-Agent": "pi4-noc-k3s-inventory/1",
            },
            method="GET",
        )
        try:
            with connection.opener.open(request, timeout=self.request_timeout) as response:
                status = getattr(response, "status", None)
                if status is None and hasattr(response, "getcode"):
                    status = response.getcode()
                if status != 200:
                    raise K3sRequestError(f"Kubernetes API request for {label} failed with HTTP {status}")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urlerror.HTTPError as exc:
            code = int(exc.code)
            try:
                exc.close()
            except Exception:
                pass
            error_type = _RetryableRequestError if code in {401, 403} else K3sRequestError
            raise error_type(f"Kubernetes API request for {label} failed with HTTP {code}") from None
        except (urlerror.URLError, ssl.SSLError, httpclient.HTTPException, TimeoutError, OSError):
            raise _RetryableRequestError(f"Kubernetes API transport failed for {label}") from None

        if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
            raise K3sResponseError(f"Kubernetes API returned an invalid response for {label}")
        try:
            document = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            raise K3sResponseError(f"Kubernetes API returned malformed JSON for {label}") from None
        if not isinstance(document, dict) or not isinstance(document.get("items"), list):
            raise K3sResponseError(f"Kubernetes API returned a malformed list for {label}")

        normalized: list[dict[str, Any]] = []
        for item in document["items"]:
            if not isinstance(item, dict):
                raise K3sResponseError(f"Kubernetes API returned a malformed item for {label}")
            normalized_item = dict(item)
            normalized_item["kind"] = expected_kind
            normalized.append(normalized_item)
        return normalized


__all__ = [
    "K3sBootstrapError",
    "K3sClient",
    "K3sClientError",
    "K3sRequestError",
    "K3sResponseError",
]
