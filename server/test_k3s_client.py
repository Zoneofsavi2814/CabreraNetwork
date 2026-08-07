import base64
import json
import ssl
import subprocess
import types
import unittest
from unittest.mock import patch
from urllib import error as urlerror

from server import k3s_client


def encoded(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def kubeconfig(*, server: str = "https://127.0.0.1:6443") -> dict:
    return {
        "current-context": "local-context",
        "contexts": [
            {"name": "ignored-context", "context": {"cluster": "ignored", "user": "ignored"}},
            {"name": "local-context", "context": {"cluster": "local-cluster", "user": "local-user"}},
        ],
        "clusters": [
            {"name": "ignored", "cluster": {"server": "https://invalid.example", "certificate-authority-data": encoded("ignored")}},
            {
                "name": "local-cluster",
                "cluster": {
                    "server": server,
                    "certificate-authority-data": encoded("-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n"),
                },
            },
        ],
        "users": [
            {"name": "ignored", "user": {"client-certificate-data": encoded("ignored"), "client-key-data": encoded("ignored")}},
            {
                "name": "local-user",
                "user": {
                    "client-certificate-data": encoded("client certificate"),
                    "client-key-data": encoded("client private key"),
                },
            },
        ],
    }


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


class InventoryOpener:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def open(self, request, timeout):
        path = request.full_url.split(":6443", 1)[-1]
        self.calls.append((path, timeout, request.get_header("Accept"), request.get_method()))
        response = self.responses.get(path, {"items": [{"metadata": {"name": path.rsplit("/", 1)[-1]}}]})
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, FakeResponse) else FakeResponse(response)


class FakeSslContext:
    def __init__(self):
        self.check_hostname = None
        self.verify_mode = None
        self.minimum_version = None
        self.cert_chain = None

    def load_cert_chain(self, certfile, keyfile):
        self.cert_chain = (certfile, keyfile)
        with open(certfile, "rb") as cert_handle, open(keyfile, "rb") as key_handle:
            self.cert_contents = cert_handle.read()
            self.key_contents = key_handle.read()


class K3sClientTests(unittest.TestCase):
    def make_runner(self, documents, calls):
        documents = list(documents)

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            document = documents.pop(0)
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr="")

        return runner

    def test_bootstrap_is_lazy_cached_and_uses_isolated_k3s_environment(self):
        command_calls = []
        opener = InventoryOpener()
        client = k3s_client.K3sClient(
            command_runner=self.make_runner([kubeconfig()], command_calls),
            opener_factory=lambda context: opener,
        )
        self.assertEqual(command_calls, [])

        fake_context = FakeSslContext()
        with patch.object(k3s_client, "_build_ssl_context", return_value=fake_context) as create_context:
            first = client.get_inventory()
            second = client.get_inventory()

        self.assertEqual(len(command_calls), 1)
        argv, options = command_calls[0]
        self.assertEqual(tuple(argv), k3s_client.KUBECTL_CONFIG_COMMAND)
        self.assertEqual(options["env"]["K3S_CONFIG_FILE"], "/dev/null")
        self.assertEqual(options["env"]["KUBECONFIG"], "/etc/rancher/k3s/k3s.yaml")
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["timeout"], 5.0)
        self.assertEqual(len(first["items"]), 6)
        self.assertEqual(second, first)
        self.assertEqual(len(opener.calls), 12)
        self.assertEqual(create_context.call_count, 1)

    def test_parsing_selects_current_context_and_decodes_credentials(self):
        document = kubeconfig(server="https://[::1]:6443/root/")
        document["clusters"][1]["cluster"]["certificate-authority-data"] = "\n".join(
            [document["clusters"][1]["cluster"]["certificate-authority-data"][:10], document["clusters"][1]["cluster"]["certificate-authority-data"][10:]]
        )
        credentials = k3s_client._parse_kubeconfig(document)
        self.assertEqual(credentials.server, "https://[::1]:6443/root")
        self.assertIn(b"BEGIN CERTIFICATE", credentials.ca_data)
        self.assertEqual(credentials.client_certificate_data, b"client certificate")
        self.assertEqual(credentials.client_key_data, b"client private key")
        self.assertNotIn("client private key", repr(credentials))

    def test_parsing_rejects_missing_context_invalid_server_and_bad_base64_without_echoing_secrets(self):
        cases = []
        missing = kubeconfig()
        missing.pop("current-context")
        cases.append(missing)
        cases.append(kubeconfig(server="https://user:super-secret@127.0.0.1:6443"))
        invalid_data = kubeconfig()
        invalid_data["users"][1]["user"]["client-key-data"] = "super-secret!!!"
        cases.append(invalid_data)

        for document in cases:
            with self.subTest(document=document.get("current-context")):
                with self.assertRaises(k3s_client.K3sBootstrapError) as raised:
                    k3s_client._parse_kubeconfig(document)
                self.assertNotIn("super-secret", str(raised.exception))

    def test_combines_all_endpoints_in_order_and_normalizes_item_kinds(self):
        responses = {}
        for index, (_label, path, _kind) in enumerate(k3s_client.INVENTORY_ENDPOINTS):
            responses[path] = {"items": [{"kind": "WrongListKind", "metadata": {"name": str(index)}}]}
        opener = InventoryOpener(responses)
        client = k3s_client.K3sClient(request_timeout=999)
        client._connection = k3s_client._Connection("https://127.0.0.1:6443", opener)

        inventory = client.get_inventory()

        self.assertEqual([item["kind"] for item in inventory["items"]], [endpoint[2] for endpoint in k3s_client.INVENTORY_ENDPOINTS])
        self.assertEqual([item["metadata"]["name"] for item in inventory["items"]], [str(index) for index in range(6)])
        self.assertEqual([call[0] for call in opener.calls], [endpoint[1] for endpoint in k3s_client.INVENTORY_ENDPOINTS])
        self.assertTrue(all(call[1] == 15.0 and call[2] == "application/json" and call[3] == "GET" for call in opener.calls))

    def test_malformed_json_and_list_items_raise_sanitized_response_errors(self):
        malformed_cases = [
            FakeResponse(b"{not-json"),
            {"not_items": []},
            {"items": ["not-an-object"]},
        ]
        for malformed in malformed_cases:
            with self.subTest(malformed=type(malformed).__name__):
                opener = InventoryOpener({"/api/v1/nodes": malformed})
                client = k3s_client.K3sClient()
                client._connection = k3s_client._Connection("https://127.0.0.1:6443", opener)
                with self.assertRaises(k3s_client.K3sResponseError) as raised:
                    client.get_inventory()
                self.assertIn("nodes", str(raised.exception))
                self.assertNotIn("not-json", str(raised.exception))

    def test_http_failure_is_bounded_sanitized_and_does_not_retry_server_errors(self):
        secret_url = "https://127.0.0.1:6443/api/v1/nodes?token=do-not-leak"
        failure = urlerror.HTTPError(secret_url, 500, "server leaked secret=do-not-leak", {}, None)
        opener = InventoryOpener({"/api/v1/nodes": failure})
        client = k3s_client.K3sClient(request_timeout=0)
        client._connection = k3s_client._Connection("https://127.0.0.1:6443", opener)

        with self.assertRaises(k3s_client.K3sRequestError) as raised:
            client.get_inventory()

        self.assertEqual(str(raised.exception), "Kubernetes API request for nodes failed with HTTP 500")
        self.assertEqual(opener.calls[0][1], 0.5)
        self.assertEqual(len(opener.calls), 1)

    def test_transport_failure_rebootstraps_once_for_certificate_rotation(self):
        command_calls = []
        stale_opener = InventoryOpener({"/api/v1/nodes": urlerror.URLError(ssl.SSLError("private-key-do-not-leak"))})
        fresh_opener = InventoryOpener()
        openers = iter([stale_opener, fresh_opener])
        client = k3s_client.K3sClient(
            command_runner=self.make_runner([kubeconfig(), kubeconfig()], command_calls),
            opener_factory=lambda context: next(openers),
        )

        with patch.object(k3s_client, "_build_ssl_context", side_effect=[FakeSslContext(), FakeSslContext()]):
            inventory = client.get_inventory()

        self.assertEqual(len(command_calls), 2)
        self.assertEqual(len(stale_opener.calls), 1)
        self.assertEqual(len(fresh_opener.calls), 6)
        self.assertEqual(len(inventory["items"]), 6)

    def test_tls_loader_closes_both_memfds_after_success_and_failure(self):
        credentials = k3s_client._parse_kubeconfig(kubeconfig())

        for fail_load in (False, True):
            written = []
            closed = []
            context = FakeSslContext()
            if fail_load:
                context.load_cert_chain = lambda certfile, keyfile: (_ for _ in ()).throw(ssl.SSLError("secret material"))
            else:
                context.load_cert_chain = lambda certfile, keyfile: None

            with (
                patch.object(k3s_client, "_create_memfd", side_effect=[101, 102]),
                patch.object(k3s_client, "_write_memfd", side_effect=lambda fd, data: written.append((fd, data))),
                patch.object(k3s_client.os, "close", side_effect=lambda fd: closed.append(fd)),
                patch.object(k3s_client.ssl, "create_default_context", return_value=context),
            ):
                if fail_load:
                    with self.assertRaises(k3s_client.K3sBootstrapError):
                        k3s_client._build_ssl_context(credentials)
                else:
                    k3s_client._build_ssl_context(credentials)

            self.assertEqual(written, [(101, b"client certificate"), (102, b"client private key")])
            self.assertEqual(closed, [102, 101])

    def test_memfd_unavailable_has_stable_test_friendly_error(self):
        credentials = k3s_client._parse_kubeconfig(kubeconfig())
        with patch.object(k3s_client, "_create_memfd", side_effect=k3s_client.K3sBootstrapError("Linux memfd support is required")), patch.object(
            k3s_client.ssl, "create_default_context", return_value=FakeSslContext()
        ):
            with self.assertRaises(k3s_client.K3sBootstrapError) as raised:
                k3s_client._build_ssl_context(credentials)
        self.assertEqual(str(raised.exception), "Linux memfd support is required")


if __name__ == "__main__":
    unittest.main()
