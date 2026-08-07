import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "rp5-balanced-efficiency.sh"


class Rp5BalancedEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "root"
        (self.root / "boot" / "firmware").mkdir(parents=True)
        (self.root / "proc" / "device-tree").mkdir(parents=True)
        (self.root / "proc" / "device-tree" / "model").write_bytes(b"Raspberry Pi 5 Model B Rev 1.0\0")
        (self.root / "boot" / "firmware" / "config.txt").write_text(
            "# preserve me\ndtparam=audio=on\ncamera_auto_detect=1\ndisplay_auto_detect=1\n"
            "dtoverlay=vc4-kms-v3d\narm_boost=1\ndtparam=cooling_fan=on\n",
            encoding="utf-8",
        )
        (self.root / "boot" / "firmware" / "cmdline.txt").write_text(
            "console=tty1 root=/dev/test quiet splash plymouth.ignore-serial-consoles ds=nocloud;i=test cgroup_memory=1\n",
            encoding="utf-8",
        )
        (self.root / "etc" / "pi5-critical-alerts").mkdir(parents=True)
        (self.root / "etc" / "pi5-critical-alerts" / "monitor.env").write_text(
            "SECRET_SETTING=preserved\nPI5_ALERTS_SKIP_CHECKS=service-nginx\n", encoding="utf-8"
        )
        units = self.root / "etc" / "systemd" / "system"
        units.mkdir(parents=True)
        for unit in ("k3s.service", "lightdm.service", "wayvnc.service", "rp5-runner-monitor.service"):
            (units / unit).write_text("[Unit]\n", encoding="utf-8")

        self.systemctl_log = Path(self.tempdir.name) / "systemctl.log"
        self.systemctl = Path(self.tempdir.name) / "systemctl-stub"
        self.systemctl.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$RP5_PROFILE_SYSTEMCTL_LOG\"\n"
            "case \" $* \" in\n"
            "  *' get-default '*) printf 'multi-user.target\\n' ;;\n"
            "  *' is-active --quiet k3s.service '*) exit 0 ;;\n"
            "  *' is-active --quiet pi5-storage-growth.timer '*) exit 0 ;;\n"
            "  *' is-enabled pi5-storage-growth.timer '*) printf 'enabled\\n' ;;\n"
            "  *' is-active --quiet '*) exit 3 ;;\n"
            "  *' list-unit-files '*) exit 0 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        self.systemctl.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            {
                "RP5_PROFILE_ROOT": str(self.root),
                "RP5_PROFILE_SYSTEMCTL": str(self.systemctl),
                "RP5_PROFILE_SYSTEMCTL_LOG": str(self.systemctl_log),
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

    def test_apply_is_idempotent_and_preserves_unmanaged_state(self):
        first = self.run_profile("apply")
        boot = self.root / "boot" / "firmware" / "config.txt"
        cmdline = self.root / "boot" / "firmware" / "cmdline.txt"
        monitor = self.root / "etc" / "pi5-critical-alerts" / "monitor.env"
        first_state = (boot.read_bytes(), cmdline.read_bytes(), monitor.read_bytes())
        first_log_size = self.systemctl_log.stat().st_size
        second = self.run_profile("apply")
        self.assertEqual(first_state, (boot.read_bytes(), cmdline.read_bytes(), monitor.read_bytes()))
        self.assertIn("reboot was not requested", first.stdout)
        self.assertIn("unchanged", second.stdout)
        self.assertIn("# preserve me", boot.read_text(encoding="utf-8"))
        self.assertIn("dtoverlay=vc4-kms-v3d", boot.read_text(encoding="utf-8"))
        self.assertIn("dtparam=audio=off", boot.read_text(encoding="utf-8"))
        self.assertIn("display_auto_detect=1", boot.read_text(encoding="utf-8"))
        self.assertNotIn(" quiet", cmdline.read_text(encoding="utf-8"))
        self.assertNotIn("ds=nocloud", cmdline.read_text(encoding="utf-8"))
        self.assertIn("plymouth.enable=0", cmdline.read_text(encoding="utf-8"))
        self.assertIn("SECRET_SETTING=preserved", monitor.read_text(encoding="utf-8"))
        self.assertIn("service-nginx,service-wayvnc,k3s-storage-growth", monitor.read_text(encoding="utf-8"))

        calls = self.systemctl_log.read_text(encoding="utf-8")
        self.assertIn("set-default multi-user.target", calls)
        self.assertIn("disable --now lightdm.service", calls)
        self.assertIn("disable --now rp5-runner-monitor.service", calls)
        self.assertIn("enable --now pi5-storage-growth.timer", calls)
        self.assertIn("restart k3s.service", calls)
        self.assertLess(calls.index("restart k3s.service"), calls.index("enable --now pi5-storage-growth.timer"))
        second_calls = self.systemctl_log.read_text(encoding="utf-8")[first_log_size:]
        self.assertNotIn("restart k3s.service", second_calls)
        self.assertNotIn("stop pi5-storage-growth.timer", second_calls)
        self.assertNotIn("enable --now pi5-storage-growth.timer", second_calls)
        self.assertNotRegex(calls, r"\\b(reboot|poweroff|halt)\\b")
        k3s_config = self.root / "etc" / "rancher" / "k3s" / "config.yaml.d" / "90-rp5-balanced-efficiency.yaml"
        self.assertIn("  - traefik", k3s_config.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "etc" / "cloud" / "cloud-init.disabled").is_file())

    def test_audit_is_read_only_and_finalize_changes_only_timer_override(self):
        boot = self.root / "boot" / "firmware" / "config.txt"
        before = boot.read_bytes()
        self.run_profile("audit")
        self.assertEqual(before, boot.read_bytes())
        self.run_profile("finalize")
        override = self.root / "etc" / "systemd" / "system" / "rp5-trend-sample.timer.d" / "20-balanced-efficiency.conf"
        self.assertIn("OnUnitActiveSec=15min", override.read_text(encoding="utf-8"))

    def test_changed_k3s_config_stops_storage_timer_when_k3s_is_inactive(self):
        stub = self.systemctl.read_text(encoding="utf-8").replace(
            "*' is-active --quiet k3s.service '*) exit 0 ;;",
            "*' is-active --quiet k3s.service '*) exit 3 ;;",
        )
        self.systemctl.write_text(stub, encoding="utf-8")

        result = self.run_profile("apply")

        calls = self.systemctl_log.read_text(encoding="utf-8")
        self.assertIn("stop pi5-storage-growth.timer pi5-storage-growth.service", calls)
        self.assertNotIn("restart k3s.service", calls)
        self.assertNotIn("enable --now pi5-storage-growth.timer", calls)
        self.assertIn("storage timer was not started", result.stderr)

    def test_verify_detects_cmdline_drift(self):
        self.run_profile("apply")
        self.run_profile("verify")
        cmdline = self.root / "boot" / "firmware" / "cmdline.txt"
        cmdline.write_text(cmdline.read_text(encoding="utf-8").strip() + " splash\n", encoding="utf-8")
        failed = self.run_profile("verify", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("headless cmdline", failed.stderr)


if __name__ == "__main__":
    unittest.main()
