import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PROBE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "pi4-ops-probe.py"
SPEC = importlib.util.spec_from_file_location("pi4_ops_probe", PROBE_PATH)
probe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def completed(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode()
        self.status = status

    def read(self, _limit):
        return self.payload

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Pi4OpsProbeTests(unittest.TestCase):
    def test_argument_parser_accepts_only_exact_allowlist(self):
        for cadence in probe.ALLOWED_CADENCES:
            self.assertEqual(probe.parse_argv(["--cadence", cadence]), cadence)
        rejected = [
            [],
            ["five-minute"],
            ["--cadence", "five-minute", "id"],
            ["--cadence", "five-minute;id"],
            ["--cadence", "daily"],
            ["--url", "http://example.test"],
        ]
        for argv in rejected:
            with self.subTest(argv=argv):
                self.assertIsNone(probe.parse_argv(argv))

    def test_contract_is_versioned_and_sanitizes_secret_like_text(self):
        started = time.monotonic()

        def unsafe(_ctx):
            return probe.check_result(
                "fixed",
                "Fixed check",
                "warn",
                "token=abc123 leaked",
                started,
                detail="password: hunter2",
            )

        checks = {"five-minute": (probe.CheckSpec("fixed", "Fixed check", unsafe),)}
        with patch.object(probe, "CHECKS", checks):
            document = probe.run_probe("five-minute")
        encoded = json.dumps(document)
        self.assertEqual(set(document), {"schemaVersion", "cadence", "generatedAt", "checks"})
        self.assertEqual(document["schemaVersion"], 1)
        self.assertEqual(document["cadence"], "five-minute")
        self.assertEqual(
            set(document["checks"][0]),
            {"id", "label", "host", "status", "message", "durationMs"},
        )
        self.assertEqual(document["checks"][0]["host"], "Pi4")
        self.assertNotIn("abc123", encoded)
        self.assertNotIn("hunter2", encoded)
        self.assertIn("[redacted]", encoded)
        self.assertNotIn("metrics", encoded)

    def test_every_cadence_has_expected_fixed_scope(self):
        ids = {cadence: {item.check_id for item in specs} for cadence, specs in probe.CHECKS.items()}
        self.assertEqual(set(ids), set(probe.ALLOWED_CADENCES))
        self.assertEqual(ids["five-minute"], {"pi4-power", "pi4-boot-state", "portfolio-loopback"})
        self.assertTrue({"k3s-node", "k3s-workloads", "k3s-resource-guardrails", "backup-freshness", "backup-artifacts", "port-drift"}.issubset(ids["hourly"]))
        self.assertEqual(ids["morning"], {"overnight-storage-events"})
        self.assertTrue({"pi4-backup-timer", "backup-service-result", "restore-drill-state", "pi3-backup-restore-check", "pi3-backup-storage", "kernel-storage-power-events", "root-disk", "data-disk", "data-mount", "brain-mount", "data-hdd-smart", "grid-brain-freshness", "grid-brain-parity", "grid-vault-sync", "backup-retention"}.issubset(ids["nightly"]))

    def test_pi3_restore_check_requires_fresh_backup_and_fresh_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "last.json"
            now = datetime.now(timezone.utc).replace(microsecond=0)

            def write_state(created, checked):
                state_path.write_text(json.dumps({
                    "schemaVersion": 1,
                    "status": "ok",
                    "backupCreatedAt": created.isoformat().replace("+00:00", "Z"),
                    "checkedAt": checked.isoformat().replace("+00:00", "Z"),
                    "alertOutboxQuickCheck": "ok",
                    "liveServicesChanged": False,
                }))

            write_state(now - timedelta(days=3), now)
            with patch.object(probe, "PI3_BACKUP_RESTORE_STATE_FILE", state_path):
                stale_backup = probe.check_pi3_backup_restore_state(probe.ProbeContext())
            write_state(now, now - timedelta(hours=30))
            with patch.object(probe, "PI3_BACKUP_RESTORE_STATE_FILE", state_path):
                stale_verifier = probe.check_pi3_backup_restore_state(probe.ProbeContext())
            write_state(now, now)
            with patch.object(probe, "PI3_BACKUP_RESTORE_STATE_FILE", state_path):
                fresh = probe.check_pi3_backup_restore_state(probe.ProbeContext())
        self.assertEqual(stale_backup["status"], "warn")
        self.assertEqual(stale_verifier["status"], "warn")
        self.assertEqual(fresh["status"], "ok")

    def test_pi3_backup_storage_guardrails_require_sticky_and_root_only_zones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modes = {
                "incoming": 0o1770,
                "processing": 0o750,
                "verified": 0o700,
                "rejected": 0o700,
                "restore-check": 0o730,
            }
            for name, mode in modes.items():
                path = root / name
                path.mkdir()
                path.chmod(mode)
            verified = root / "verified/pi3-backup-20260722T010101Z"
            verified.mkdir()
            verified.chmod(0o700)
            archive = verified / "archive.tgz"
            archive.write_text("fixture")
            archive.chmod(0o600)
            with patch.object(probe, "PI3_BACKUP_ROOT", root), patch.object(probe, "ROOT_UID", probe.os.getuid()):
                healthy = probe.check_pi3_backup_storage(probe.ProbeContext())
            (root / "incoming").chmod(0o770)
            with patch.object(probe, "PI3_BACKUP_ROOT", root), patch.object(probe, "ROOT_UID", probe.os.getuid()):
                unsafe = probe.check_pi3_backup_storage(probe.ProbeContext())
        self.assertEqual(healthy["status"], "ok")
        self.assertEqual(unsafe["status"], "warn")

    def test_boot_state_omits_boot_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            boot_id_path = Path(tmp) / "boot-id"
            state_path.write_text(json.dumps({
                "currentBootId": "current-sensitive-id",
                "currentBootClean": False,
                "previousBootId": "previous-sensitive-id",
                "previousBootClean": True,
                "uncleanBootCount": 0,
            }))
            boot_id_path.write_text("current-sensitive-id")
            with patch.object(probe, "BOOT_STATE_FILE", state_path), patch.object(probe, "BOOT_ID_FILE", boot_id_path):
                result = probe.check_boot_state(probe.ProbeContext())
        encoded = json.dumps(result)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("current-sensitive-id", encoded)
        self.assertNotIn("previous-sensitive-id", encoded)

    def test_portfolio_check_uses_fixed_loopback_url(self):
        with patch.object(probe.urlrequest, "urlopen", return_value=FakeResponse({"ok": True})) as mocked:
            result = probe.check_portfolio_loopback(probe.ProbeContext())
        self.assertEqual(result["status"], "ok")
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8099/api/health")

    def test_retained_workloads_exclude_migrated_sites(self):
        payload = {
            "items": [
                {"kind": "Deployment", "metadata": {"namespace": "homelab", "name": "grid"}, "spec": {"replicas": 1, "template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "1m", "memory": "1Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}}}]}}}, "status": {"readyReplicas": 1}},
                {"kind": "Deployment", "metadata": {"namespace": "eagleeye", "name": "eagleeye"}, "spec": {"replicas": 1, "template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "1m", "memory": "1Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}}}]}}}, "status": {"readyReplicas": 1}},
                {"kind": "Deployment", "metadata": {"namespace": "wedding", "name": "wedding-website"}, "spec": {"replicas": 1}, "status": {"readyReplicas": 0}},
                {"kind": "Deployment", "metadata": {"namespace": "cabrera-work-website", "name": "cabrera-work-website"}, "spec": {"replicas": 1}, "status": {"readyReplicas": 0}},
            ]
        }
        ctx = probe.ProbeContext()
        with patch.object(ctx, "run", return_value=completed(json.dumps(payload))):
            workload = probe.check_k3s_workloads(ctx)
            resources = probe.check_k3s_resources(ctx)
        self.assertEqual(workload["status"], "ok")
        self.assertEqual(resources["status"], "ok")
        self.assertEqual(workload["metrics"]["expectedWorkloads"], 2)

    def test_port_drift_ignores_internal_addresses_and_flags_public_listener(self):
        stdout = "\n".join([
            "tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:*",
            "tcp LISTEN 0 128 127.0.0.1:3000 0.0.0.0:*",
            "tcp LISTEN 0 128 10.43.0.1:443 0.0.0.0:*",
            "tcp LISTEN 0 128 0.0.0.0:12345 0.0.0.0:*",
        ])
        ctx = probe.ProbeContext()
        with patch.object(ctx, "run", return_value=completed(stdout)):
            result = probe.check_port_drift(ctx)
        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["metrics"]["unexpectedListeners"], ["tcp/12345"])

    def test_forced_command_and_install_artifacts_are_restrictive(self):
        root = Path(__file__).resolve().parent.parent / "scripts"
        wrapper = (root / "pi4-ops-probe-ssh").read_text()
        sshd = (root / "pi4-ops-probe.sshd_config").read_text()
        sudoers = (root / "pi4-ops-probe.sudoers").read_text()
        client = (root / "pi3-pi4-ops-probe.ssh_config").read_text()
        installer = (root / "install-pi4-ops-probe.sh").read_text()
        self.assertNotIn("eval", wrapper)
        self.assertIn("${SSH_ORIGINAL_COMMAND-}", wrapper)
        self.assertIn("ForceCommand /usr/local/libexec/pi4-ops-probe-ssh", sshd)
        self.assertIn("DisableForwarding yes", sshd)
        self.assertIn('from="192.168.0.142",restrict', installer)
        self.assertEqual(sudoers.count("/usr/local/libexec/pi4-ops-probe --cadence"), 4)
        self.assertIn("StrictHostKeyChecking yes", client)
        self.assertIn("HostKeyAlgorithms ssh-ed25519", client)
        self.assertIn("ClearAllForwardings yes", client)


if __name__ == "__main__":
    unittest.main()
