import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load_script(name, module_name):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


claim = load_script("pi3-backup-claim.py", "pi3_backup_claim")
verify = load_script("pi3-backup-verify.py", "pi3_backup_verify")


class Pi3BackupTargetTests(unittest.TestCase):
    @staticmethod
    def write_checksum_manifest(scope: Path, destination: Path, files: list[Path]) -> None:
        lines = []
        for path in sorted(files, key=lambda item: item.relative_to(scope).as_posix()):
            relative = path.relative_to(scope).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  ./{relative}\n")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("".join(lines), encoding="utf-8")

    @staticmethod
    def write_static_release(payload: Path, site: str) -> None:
        site_root = payload / "sites" / site
        files = sorted(
            (path for path in site_root.rglob("*") if path.is_file()),
            key=lambda item: item.relative_to(site_root).as_posix(),
        )
        inventory = [
            {
                "path": path.relative_to(site_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
            for path in files
        ]
        canonical = "".join(
            f'{item["sha256"]}  {item["size"]}  {item["path"]}\n'
            for item in inventory
        )
        tree_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        reviewed = {
            "schemaVersion": 1,
            "algorithm": "sha256",
            "treeSha256": tree_digest,
            "files": inventory,
        }
        reviewed_path = payload / "manifests" / site / f"{tree_digest}.json"
        reviewed_path.parent.mkdir(parents=True, exist_ok=True)
        reviewed_path.write_text(json.dumps(reviewed), encoding="utf-8")
        release = {
            "schemaVersion": 1,
            "site": site,
            "releaseName": tree_digest,
        }
        (payload / "manifests" / f"{site}-release.json").write_text(
            json.dumps(release),
            encoding="utf-8",
        )
        Pi3BackupTargetTests.write_checksum_manifest(
            site_root,
            payload / "manifests" / f"{site}-SHA256SUMS",
            files,
        )

    def build_backup(self, root: Path, created_at=None):
        created = created_at or datetime.now(timezone.utc).replace(microsecond=0)
        backup_id = created.strftime("pi3-backup-%Y%m%dT%H%M%SZ")
        payload = root / "payload" / backup_id
        required = [
            "services/rp3-status/status.txt",
            "state/operations/state.json",
            "state/notifications/state.json",
            "config/nginx/nginx.conf",
            "sites/wedding/index.html",
            "sites/work-website/index.html",
            "manifests/releases.json",
            "deployment/metadata.json",
        ]
        for relative in required:
            target = payload / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture")
        for relative, table in (
            ("state/operations/status.db", "status"),
            ("state/operations/operations.db", "operations"),
            ("state/alert-relay/outbox.sqlite3", "outbox"),
        ):
            database = payload / relative
            database.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(database)
            connection.execute(f"create table {table} (id integer primary key, body text)")
            connection.commit()
            connection.close()
        self.write_static_release(payload, "wedding")
        self.write_static_release(payload, "work-website")
        manifest = {
            "schemaVersion": 1,
            "backupId": backup_id,
            "createdAt": created.isoformat().replace("+00:00", "Z"),
            "components": sorted(verify.REQUIRED_COMPONENTS),
        }
        (payload / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        global_checksum = payload / "manifests/SHA256SUMS"
        self.write_checksum_manifest(
            payload,
            global_checksum,
            [path for path in payload.rglob("*") if path.is_file() and path != global_checksum],
        )
        archive = root / f"{backup_id}.tgz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(payload, arcname=backup_id)
        shutil.rmtree(payload.parent)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = root / f"{archive.name}.sha256"
        checksum.write_text(f"{digest}  {archive.name}\n")
        return backup_id, archive, checksum

    def extract_backup(self, archive: Path, destination: Path) -> Path:
        with patch.object(verify, "ROOT_UID", os.getuid()):
            payload_root, _members, _bytes = verify.extract_isolated(archive, destination)
        return payload_root

    def layout(self, root: Path):
        base = root / "backups/pi3"
        state = root / "state"
        paths = {
            "base": base,
            "incoming": base / "incoming",
            "processing": base / "processing",
            "verified": base / "verified",
            "rejected": base / "rejected",
            "restore": base / "restore-check",
            "state": state,
            "results": state / "results",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths

    def patches(self, paths):
        uid, gid = os.getuid(), os.getgid()
        return [
            patch.object(claim, "ROOT_UID", uid),
            patch.object(claim, "ROOT_GID", gid),
            patch.object(claim, "BASE_ROOT", paths["base"]),
            patch.object(claim, "INCOMING_ROOT", paths["incoming"]),
            patch.object(claim, "PROCESSING_ROOT", paths["processing"]),
            patch.object(claim, "VERIFIED_ROOT", paths["verified"]),
            patch.object(claim, "REJECTED_ROOT", paths["rejected"]),
            patch.object(claim, "RESTORE_ROOT", paths["restore"]),
            patch.object(claim, "STATE_ROOT", paths["state"]),
            patch.object(claim, "RESULT_ROOT", paths["results"]),
            patch.object(claim, "ACTIVE_FILE", paths["state"] / "active.json"),
            patch.object(claim, "STATE_FILE", paths["state"] / "last.json"),
            patch.object(claim, "STORAGE_STATE_FILE", paths["state"] / "storage.json"),
            patch.object(claim, "account_ids", return_value=(uid, gid)),
            patch.object(claim.os, "chown"),
            patch.object(verify, "ROOT_UID", uid),
            patch.object(verify, "PROCESSING_ROOT", paths["processing"]),
            patch.object(verify, "RESTORE_ROOT", paths["restore"]),
            patch.object(verify, "STATE_ROOT", paths["state"]),
            patch.object(verify, "RESULT_ROOT", paths["results"]),
            patch.object(verify, "ACTIVE_FILE", paths["state"] / "active.json"),
        ]

    def test_claim_verify_finalize_moves_pair_through_trust_zones(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.layout(Path(tmp))
            backup_id, archive, checksum = self.build_backup(paths["incoming"])
            stack = self.patches(paths)
            for item in stack:
                item.start()
            try:
                claimed = claim.claim_pair()
                self.assertEqual(claimed["status"], "claimed")
                self.assertFalse(archive.exists())
                self.assertFalse(checksum.exists())
                active = verify.active_claim()
                result = verify.verify_claimed(active)
                verify.write_result(str(active["claimId"]), result)
                with patch.object(claim, "fsync_directory", wraps=claim.fsync_directory) as synced:
                    finalized = claim.finalize_claim()
                synced_paths = {item.args[0] for item in synced.call_args_list}
            finally:
                for item in reversed(stack):
                    item.stop()
            self.assertEqual(finalized["status"], "ok")
            self.assertTrue((paths["verified"] / backup_id / f"{backup_id}.tgz").is_file())
            self.assertEqual(list(paths["processing"].iterdir()), [])
            state = json.loads((paths["state"] / "last.json").read_text())
            self.assertEqual(state["status"], "ok")
            self.assertIn("backupCreatedAt", state)
            self.assertFalse(state["liveServicesChanged"])
            self.assertTrue({paths["processing"], paths["verified"], paths["state"], paths["results"]}.issubset(synced_paths))

    def test_prepare_resumes_processing_claim_without_incoming_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.layout(Path(tmp))
            backup_id, _archive, _checksum = self.build_backup(paths["incoming"])
            stack = self.patches(paths)
            for item in stack:
                item.start()
            try:
                claimed = claim.claim_pair()
                self.assertEqual(list(paths["incoming"].iterdir()), [])
                resumed = claim.prepare_claim()
                active = verify.active_claim()
                result = verify.verify_claimed(active)
                verify.write_result(str(active["claimId"]), result)
                reused = verify.existing_result(active)
                with patch.object(verify, "verify_claimed", side_effect=AssertionError("must reuse result")):
                    with patch("builtins.print"):
                        retry_status = verify.main(["--claimed"])
                finalized = claim.finalize_claim()
            finally:
                for item in reversed(stack):
                    item.stop()
            self.assertEqual(resumed["status"], "resumed")
            self.assertEqual(resumed["claimId"], claimed["claimId"])
            self.assertEqual(reused, result)
            self.assertEqual(retry_status, 0)
            self.assertEqual(finalized["status"], "ok")
            self.assertTrue((paths["verified"] / backup_id).is_dir())

    def test_prepare_reconstructs_complete_orphan_before_claim_document_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.layout(Path(tmp))
            backup_id, _archive, _checksum = self.build_backup(paths["incoming"])
            stack = self.patches(paths)
            for item in stack:
                item.start()
            try:
                real_replace = claim.os.replace
                real_unlink = Path.unlink

                def interrupt_claim_document_replace(source, destination):
                    if Path(destination).name == "claim.json":
                        raise SystemExit("simulated power loss")
                    return real_replace(source, destination)

                def preserve_interrupted_temp(path, missing_ok=False):
                    if path.name.startswith(".claim.json-"):
                        return None
                    return real_unlink(path, missing_ok=missing_ok)

                with patch.object(claim.os, "replace", new=interrupt_claim_document_replace):
                    with patch.object(Path, "unlink", new=preserve_interrupted_temp):
                        with self.assertRaises(SystemExit):
                            claim.claim_pair()
                self.assertFalse(claim.ACTIVE_FILE.exists())
                self.assertEqual(len(list(paths["processing"].iterdir())), 1)
                orphan = next(paths["processing"].iterdir())
                self.assertFalse((orphan / "claim.json").exists())
                temporary_files = list(orphan.glob(".claim.json-*"))
                self.assertEqual(len(temporary_files), 1)
                with patch.object(claim, "fsync_directory", wraps=claim.fsync_directory) as synced:
                    recovered = claim.prepare_claim()
                synced_paths = {item.args[0] for item in synced.call_args_list}
                active = verify.active_claim()
                result = verify.verify_claimed(active)
                verify.write_result(str(active["claimId"]), result)
                finalized = claim.finalize_claim()
            finally:
                for item in reversed(stack):
                    item.stop()
            self.assertEqual(recovered["status"], "recovered")
            self.assertEqual(recovered["backupId"], backup_id)
            self.assertFalse(temporary_files[0].exists())
            self.assertIn(orphan, synced_paths)
            self.assertIn(paths["state"], synced_paths)
            self.assertEqual(finalized["status"], "ok")

    def test_prepare_quarantines_one_file_orphan_without_wedging(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.layout(Path(tmp))
            backup_id, _archive, _checksum = self.build_backup(paths["incoming"])
            stack = self.patches(paths)
            for item in stack:
                item.start()
            try:
                real_rename = claim.os.rename

                def interrupt_second_move(source, destination):
                    if Path(source).parent == paths["incoming"] and Path(source).name.endswith(".sha256"):
                        raise SystemExit("simulated power loss")
                    return real_rename(source, destination)

                with patch.object(claim.os, "rename", side_effect=interrupt_second_move):
                    with self.assertRaises(SystemExit):
                        claim.claim_pair()
                orphan = next(paths["processing"].iterdir())
                self.assertTrue((orphan / f"{backup_id}.tgz").is_file())
                self.assertTrue((paths["incoming"] / f"{backup_id}.tgz.sha256").is_file())
                with patch.object(claim, "fsync_directory", wraps=claim.fsync_directory) as synced:
                    reconciled = claim.prepare_claim()
                synced_paths = {item.args[0] for item in synced.call_args_list}
            finally:
                for item in reversed(stack):
                    item.stop()
            state = json.loads((paths["state"] / "last.json").read_text())
            rejected = list(paths["rejected"].iterdir())
            self.assertEqual(reconciled, {"schemaVersion": 1, "status": "idle", "reconciled": "quarantined"})
            self.assertEqual(list(paths["processing"].iterdir()), [])
            self.assertEqual(list(paths["incoming"].iterdir()), [])
            self.assertEqual(len(rejected), 1)
            self.assertTrue(rejected[0].name.endswith(".claim-failed"))
            self.assertEqual(state["error"]["code"], "orphan_incomplete")
            self.assertFalse((paths["state"] / "active.json").exists())
            self.assertTrue({paths["processing"], paths["rejected"], paths["state"]}.issubset(synced_paths))

    def test_result_publication_is_atomic_and_cleans_interrupted_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            claim_id = "pi3-backup-20260722T235500Z.claim-0123456789abcdef"
            target = results / f"{claim_id}.json"
            document = {
                "schemaVersion": 1,
                "status": "fail",
                "checkedAt": "2026-07-22T23:55:00Z",
                "backupId": "pi3-backup-20260722T235500Z",
                "claimId": claim_id,
                "error": {"code": "fixture", "message": "fixture"},
                "liveServicesChanged": False,
            }
            observed_temporary = []

            def interrupt_replace(source, destination):
                observed_temporary.append(Path(source))
                self.assertTrue(Path(source).is_file())
                self.assertEqual(Path(destination), target)
                self.assertFalse(target.exists())
                raise OSError("simulated power loss before rename")

            with patch.object(verify, "RESULT_ROOT", results):
                with patch.object(verify.os, "replace", side_effect=interrupt_replace):
                    with self.assertRaises(OSError):
                        verify.write_result(claim_id, document)
                self.assertFalse(target.exists())
                self.assertTrue(observed_temporary)
                self.assertFalse(observed_temporary[0].exists())
                stale = results / f".{claim_id}.json.tmp-deadbeefdeadbeef"
                stale.write_text('{"partial":', encoding="utf-8")
                verify.write_result(claim_id, document)
            self.assertFalse(stale.exists())
            self.assertEqual(json.loads(target.read_text()), document)
            self.assertEqual({path.name for path in results.iterdir()}, {target.name})

    def test_orphan_claim_temp_cleanup_fails_closed_on_non_regular_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            claim_path = Path(tmp) / "pi3-backup-20260722T235500Z.claim-0123456789abcdef"
            claim_path.mkdir()
            target = claim_path / "target"
            target.write_text("fixture", encoding="utf-8")
            unsafe = claim_path / ".claim.json-abcdefgh"
            unsafe.symlink_to(target)
            with patch.object(claim, "ROOT_UID", os.getuid()):
                with self.assertRaises(claim.ClaimError) as caught:
                    claim.cleanup_orphan_claim_temps(claim_path)
            self.assertEqual(caught.exception.code, "claim_ambiguous")
            self.assertTrue(unsafe.is_symlink())

    def test_prepare_completes_verified_move_before_active_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.layout(Path(tmp))
            backup_id, _archive, _checksum = self.build_backup(paths["incoming"])
            stack = self.patches(paths)
            for item in stack:
                item.start()
            try:
                claim.claim_pair()
                active = verify.active_claim()
                result = verify.verify_claimed(active)
                verify.write_result(str(active["claimId"]), result)
                processing = paths["processing"] / str(active["claimId"])
                destination = paths["verified"] / backup_id
                claim.make_root_only(processing)
                os.rename(processing, destination)
                reconciled = claim.prepare_claim()
            finally:
                for item in reversed(stack):
                    item.stop()
            state = json.loads((paths["state"] / "last.json").read_text())
            self.assertEqual(reconciled["status"], "idle")
            self.assertEqual(state["status"], "ok")
            self.assertFalse((paths["state"] / "active.json").exists())
            self.assertEqual(list(paths["results"].iterdir()), [])

    def test_finalize_completes_rejected_move_before_state_and_active_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.layout(Path(tmp))
            backup_id, _archive, _checksum = self.build_backup(paths["incoming"])
            stack = self.patches(paths)
            for item in stack:
                item.start()
            try:
                claimed = claim.claim_pair()
                processing = paths["processing"] / str(claimed["claimId"])
                destination = paths["rejected"] / f'{claimed["claimId"]}.rejected'
                claim.atomic_json(
                    paths["state"] / "last.json",
                    {
                        "schemaVersion": 1,
                        "status": "fail",
                        "backupId": "pi3-backup-20260721T235500Z",
                        "error": {"code": "prior", "message": "prior"},
                        "liveServicesChanged": False,
                    },
                    0o600,
                    claim.ROOT_UID,
                    claim.ROOT_GID,
                )
                claim.make_root_only(processing)
                os.rename(processing, destination)
                finalized = claim.finalize_claim()
            finally:
                for item in reversed(stack):
                    item.stop()
            state = json.loads((paths["state"] / "last.json").read_text())
            self.assertEqual(finalized["status"], "fail")
            self.assertEqual(state["backupId"], backup_id)
            self.assertEqual(state["error"]["code"], "verifier_no_result")
            self.assertFalse((paths["state"] / "active.json").exists())

    def test_incoming_rejects_links_and_multi_link_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = Path(tmp)
            target = incoming / "target"
            target.write_text("data")
            (incoming / "link").symlink_to(target)
            with patch.object(claim, "INCOMING_ROOT", incoming):
                with self.assertRaises(claim.ClaimError) as caught:
                    claim.incoming_inventory(os.getuid())
            self.assertEqual(caught.exception.code, "incoming_unsafe")
        with tempfile.TemporaryDirectory() as tmp:
            incoming = Path(tmp)
            original = incoming / "one"
            original.write_text("data")
            os.link(original, incoming / "two")
            with patch.object(claim, "INCOMING_ROOT", incoming):
                with self.assertRaises(claim.ClaimError) as caught:
                    claim.incoming_inventory(os.getuid())
            self.assertEqual(caught.exception.code, "incoming_unsafe")

    def test_claim_enforces_compressed_size_and_file_count_quotas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            large = root / "pi3-backup-20260722T235500Z.tgz"
            large.write_bytes(b"12345")
            with patch.object(claim, "MAX_ARCHIVE_BYTES", 4):
                with self.assertRaises(claim.ClaimError) as caught:
                    claim.secure_regular_stat(large, os.getuid(), claim.MAX_ARCHIVE_BYTES)
            self.assertEqual(caught.exception.code, "upload_size")
            for index in range(claim.MAX_INCOMING_FILES):
                (root / f"extra-{index}").write_text("x")
            with patch.object(claim, "INCOMING_ROOT", root):
                with self.assertRaises(claim.ClaimError) as caught:
                    claim.incoming_inventory(os.getuid())
            self.assertEqual(caught.exception.code, "incoming_quota")

    def test_manifest_rejects_stale_creation_even_if_checked_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            created = datetime.now(timezone.utc) - timedelta(days=3)
            backup_id, archive, _checksum = self.build_backup(Path(tmp), created)
            destination = Path(tmp) / "restore"
            destination.mkdir()
            with patch.object(verify, "ROOT_UID", os.getuid()):
                payload_root, _members, _bytes = verify.extract_isolated(archive, destination)
            with self.assertRaises(verify.VerificationError) as caught:
                verify.parse_manifest(payload_root, backup_id)
            self.assertEqual(caught.exception.code, "backup_stale")

    def test_internal_payload_checksum_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _backup_id, archive, _checksum = self.build_backup(root)
            destination = root / "restore"
            destination.mkdir()
            payload_root = self.extract_backup(archive, destination)
            (payload_root / "sites/wedding/index.html").write_text("tampered", encoding="utf-8")
            with self.assertRaises(verify.VerificationError) as caught:
                verify.verify_payload_checksums(payload_root)
            self.assertEqual(caught.exception.code, "payload_checksum")

    def test_reviewed_site_manifest_detects_rehashed_unreviewed_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _backup_id, archive, _checksum = self.build_backup(root)
            destination = root / "restore"
            destination.mkdir()
            payload_root = self.extract_backup(archive, destination)
            changed = payload_root / "sites/wedding/index.html"
            changed.write_text("unreviewed replacement", encoding="utf-8")
            global_checksum = payload_root / "manifests/SHA256SUMS"
            self.write_checksum_manifest(
                payload_root,
                global_checksum,
                [path for path in payload_root.rglob("*") if path.is_file() and path != global_checksum],
            )
            verify.verify_payload_checksums(payload_root)
            with self.assertRaises(verify.VerificationError) as caught:
                verify.verify_static_release(payload_root, "wedding")
            self.assertEqual(caught.exception.code, "release_manifest")

    def test_archive_rejects_traversal_links_and_special_files(self):
        members = [
            tarfile.TarInfo("../escape"),
            tarfile.TarInfo("pi3-backup-20260722T235500Z/link"),
            tarfile.TarInfo("pi3-backup-20260722T235500Z/fifo"),
        ]
        members[1].type = tarfile.SYMTYPE
        members[1].linkname = "/etc/passwd"
        members[2].type = tarfile.FIFOTYPE
        for member in members:
            with self.subTest(name=member.name):
                with self.assertRaises(verify.VerificationError):
                    verify.safe_member_path(member, "pi3-backup-20260722T235500Z")

    def test_verified_retention_is_root_scoped_and_count_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(6):
                path = root / f"pi3-backup-202607{10 + index:02d}T010101Z"
                path.mkdir()
                (path / "archive").write_text("x")
            with patch.object(claim, "ROOT_UID", os.getuid()):
                removed = claim.apply_retention(
                    root,
                    claim.re.compile(r"^pi3-backup-\d{8}T\d{6}Z$"),
                    keep_count=3,
                )
            self.assertEqual(removed, 3)
            self.assertEqual(len(list(root.iterdir())), 3)

    def test_receiver_service_and_installers_encode_security_boundaries(self):
        wrapper = (SCRIPTS / "pi3-backup-receiver-ssh").read_text()
        service = (SCRIPTS / "pi3-backup-verify.service").read_text()
        installer = (SCRIPTS / "install-pi3-backup-target.sh").read_text()
        probe_installer = (SCRIPTS / "install-pi4-ops-probe.sh").read_text()
        self.assertIn("/usr/bin/rrsync -wo -no-del -munge", wrapper)
        self.assertIn("--inplace", wrapper)
        self.assertIn("/usr/bin/prlimit --fsize=2147483648:2147483648 --nofile=64:64 --", wrapper)
        self.assertNotIn("ulimit -f", wrapper)
        self.assertIn("/usr/bin/timeout", wrapper)
        self.assertIn("/usr/bin/flock", wrapper)
        self.assertIn("User=pi3verify", service)
        self.assertIn("ExecCondition=+/usr/local/libexec/pi3-backup-claim --action prepare", service)
        self.assertNotIn("ConditionPathExistsGlob=", service)
        self.assertIn("ExecStopPost=+/usr/local/libexec/pi3-backup-claim --action finalize", service)
        self.assertIn("TimeoutStartSec=15m", service)
        self.assertIn("MemoryMax=512M", service)
        self.assertIn("ProtectProc=invisible", service)
        self.assertIn("-m 1770 /mnt/ssd/backups/pi3/incoming", installer)
        self.assertIn("/usr/bin/prlimit", installer)
        self.assertIn("enable pi3-backup-verify.service", installer)
        self.assertIn("rollback_ssh", installer)
        self.assertIn("authorized_keys.previous", installer)
        self.assertIn("sshd_dropin.previous", installer)
        self.assertIn("rollback_ssh", probe_installer)
        self.assertIn("authorized_keys.previous", probe_installer)
        self.assertIn("sshd_dropin.previous", probe_installer)


if __name__ == "__main__":
    unittest.main()
