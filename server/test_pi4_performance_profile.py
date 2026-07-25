import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "pi4-performance-profile.sh"


class Pi4PerformanceProfileTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "root"
        self.root.mkdir()

        boot = self.root / "boot" / "firmware" / "config.txt"
        boot.parent.mkdir(parents=True)
        boot.write_text(
            "# keep this comment\n"
            "dtparam=audio=on\n"
            "camera_auto_detect=1\n"
            "display_auto_detect=1\n"
            "dtoverlay=vc4-kms-v3d\n"
            "max_framebuffers=2\n"
            "arm_boost=1\n"
            "[all]\n",
            encoding="utf-8",
        )

        policy = self.root / "sys" / "devices" / "system" / "cpu" / "cpufreq" / "policy0"
        policy.mkdir(parents=True)
        (policy / "scaling_available_governors").write_text("conservative ondemand performance schedutil\n", encoding="utf-8")
        (policy / "scaling_governor").write_text("ondemand\n", encoding="utf-8")
        (policy / "scaling_min_freq").write_text("600000\n", encoding="utf-8")
        (policy / "scaling_max_freq").write_text("1800000\n", encoding="utf-8")

        units = self.root / "usr" / "lib" / "systemd" / "system"
        units.mkdir(parents=True)
        for unit in (
            "lightdm.service",
            "display-backlight.service",
            "glamor-test.service",
            "wpa_supplicant.service",
            "k3s.service",
            "cpufrequtils.service",
        ):
            (units / unit).write_text("[Unit]\n", encoding="utf-8")

        self.systemctl_log = Path(self.tempdir.name) / "systemctl.log"
        self.systemctl = Path(self.tempdir.name) / "systemctl-stub"
        self.systemctl.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$PI4_PROFILE_SYSTEMCTL_LOG\"\n"
            "case \" $* \" in\n"
            "  *' get-default '*) printf 'multi-user.target\\n' ;;\n"
            "  *' is-enabled '*) printf 'disabled\\n'; exit 1 ;;\n"
            "  *' is-active k3s.service '*) printf 'active\\n' ;;\n"
            "  *' is-active '*) printf 'inactive\\n'; exit 3 ;;\n"
            "  *' list-unit-files '*) exit 0 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        self.systemctl.chmod(0o755)

        self.env = os.environ.copy()
        self.env.update(
            {
                "PI4_PROFILE_ROOT": str(self.root),
                "PI4_PROFILE_SYSTEMCTL": str(self.systemctl),
                "PI4_PROFILE_SYSTEMCTL_LOG": str(self.systemctl_log),
            }
        )

    def run_profile(self, action, check=True):
        return subprocess.run(
            ["bash", str(SCRIPT), action],
            env=self.env,
            check=check,
            capture_output=True,
            text=True,
        )

    def test_apply_is_idempotent_and_never_requests_reboot(self):
        first = self.run_profile("apply")
        boot = self.root / "boot" / "firmware" / "config.txt"
        first_boot = boot.read_bytes()

        second = self.run_profile("apply")
        self.assertEqual(first_boot, boot.read_bytes())
        self.assertIn("no reboot was requested", first.stdout)
        self.assertIn("unchanged", second.stdout)

        config = boot.read_text(encoding="utf-8")
        self.assertIn("# keep this comment", config)
        self.assertIn("dtoverlay=vc4-kms-v3d", config)
        self.assertIn("# BEGIN pi4-performance-profile\n[all]\n", config)
        self.assertEqual(config.count("dtparam=audio=off"), 1)
        self.assertEqual(config.count("dtoverlay=disable-wifi"), 1)
        self.assertEqual(config.count("dtoverlay=disable-bt"), 1)
        self.assertNotIn("dtparam=audio=on", config)

        self.assertEqual(
            (self.root / "etc" / "default" / "cpufrequtils").read_text(encoding="utf-8"),
            (REPO_ROOT / "config" / "performance" / "cpufrequtils").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (self.root / "etc" / "rancher" / "k3s" / "config.yaml").read_text(encoding="utf-8"),
            (REPO_ROOT / "config" / "performance" / "k3s-config.yaml").read_text(encoding="utf-8"),
        )
        k3s_config = (self.root / "etc" / "rancher" / "k3s" / "config.yaml").read_text(encoding="utf-8")
        for retained_setting in (
            'node-ip: "192.168.0.101"',
            'advertise-address: "192.168.0.101"',
            '  - "192.168.0.101"',
            'data-dir: "/mnt/ssd/k3s"',
            'default-local-storage-path: "/mnt/ssd/k3s-storage"',
            'write-kubeconfig-mode: "0644"',
        ):
            self.assertIn(retained_setting, k3s_config)
        for disabled_component in ("traefik", "servicelb", "metrics-server", "local-storage"):
            self.assertIn(f"  - {disabled_component}\n", k3s_config)
        self.assertEqual(
            (self.root / "etc" / "systemd" / "system" / "k3s.service.d" / "30-performance-profile.conf").read_text(encoding="utf-8"),
            (REPO_ROOT / "config" / "performance" / "k3s-execstart.conf").read_text(encoding="utf-8"),
        )

        calls = self.systemctl_log.read_text(encoding="utf-8")
        self.assertIn("set-default multi-user.target", calls)
        self.assertIn("disable --now lightdm.service", calls)
        self.assertIn("disable --now display-backlight.service", calls)
        self.assertIn("disable --now glamor-test.service", calls)
        self.assertIn("disable --now wpa_supplicant.service", calls)
        self.assertNotIn("disable --now bluetooth.service", calls)
        self.assertIn("restart k3s.service", calls)
        self.assertNotRegex(calls, r"\b(reboot|poweroff|halt)\b")

    def test_audit_is_read_only(self):
        boot = self.root / "boot" / "firmware" / "config.txt"
        before = boot.read_bytes()

        result = self.run_profile("audit")

        self.assertEqual(before, boot.read_bytes())
        self.assertFalse((self.root / "etc" / "default" / "cpufrequtils").exists())
        self.assertIn("performance profile audit", result.stdout)
        calls = self.systemctl_log.read_text(encoding="utf-8")
        self.assertNotRegex(calls, r"\b(disable|enable|restart|stop|set-default|daemon-reload)\b")

    def test_apply_changes_only_adguard_querylog_interval(self):
        state = {
            "config": {
                "anonymize_client_ip": False,
                "enabled": True,
                "ignored": ["health.example"],
                "ignored_enabled": True,
                "interval": 7776000000,
            },
            "updates": [],
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(state["config"]).encode())

            def do_PUT(self):
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size))
                state["updates"].append(body)
                state["config"] = body
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format, *args):
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            thread.join(2)
            server.server_close()

        self.addCleanup(stop_server)

        original = dict(state["config"])
        creds = Path(self.tempdir.name) / "adguard-creds"
        creds.write_text(
            f"url=http://127.0.0.1:{server.server_port}\nusername=test\npassword=test\n",
            encoding="utf-8",
        )
        self.env["PI4_PROFILE_ADGUARD_CREDS"] = str(creds)

        self.run_profile("apply")

        self.assertEqual(len(state["updates"]), 1)
        self.assertEqual(state["config"]["interval"], 604800000)
        self.assertEqual(
            {key: value for key, value in state["config"].items() if key != "interval"},
            {key: value for key, value in original.items() if key != "interval"},
        )
        self.run_profile("apply")
        self.assertEqual(len(state["updates"]), 1)

    def test_verify_accepts_absent_units_and_detects_drift(self):
        self.run_profile("apply")
        managed_files = (
            self.root / "boot" / "firmware" / "config.txt",
            self.root / "etc" / "default" / "cpufrequtils",
            self.root / "etc" / "rancher" / "k3s" / "config.yaml",
            self.root / "etc" / "systemd" / "system" / "k3s.service.d" / "30-performance-profile.conf",
        )
        before = {path: path.read_bytes() for path in managed_files}
        log_offset = self.systemctl_log.stat().st_size

        passed = self.run_profile("verify")
        self.assertIn("performance profile verified", passed.stdout)
        self.assertIn("bluetooth.service is absent", passed.stdout)
        self.assertEqual(before, {path: path.read_bytes() for path in managed_files})
        verify_calls = self.systemctl_log.read_text(encoding="utf-8")[log_offset:]
        self.assertNotRegex(verify_calls, r"\b(disable|enable|restart|stop|set-default|daemon-reload)\b")

        (self.root / "etc" / "default" / "cpufrequtils").write_text('GOVERNOR="performance"\n', encoding="utf-8")
        failed = self.run_profile("verify", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("cpufrequtils profile is missing or drifted", failed.stderr)


if __name__ == "__main__":
    unittest.main()
