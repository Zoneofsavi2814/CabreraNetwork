#!/usr/bin/python3
"""Unprivileged parser for one root-claimed Pi3 backup pair."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO


SCHEMA_VERSION = 1
ROOT_UID = 0
PROCESSING_ROOT = Path("/mnt/ssd/backups/pi3/processing")
RESTORE_ROOT = Path("/mnt/ssd/backups/pi3/restore-check")
STATE_ROOT = Path("/var/lib/pi3-backup")
RESULT_ROOT = STATE_ROOT / "results"
ACTIVE_FILE = STATE_ROOT / "active-claim.json"
ARCHIVE_RE = re.compile(r"^(pi3-backup-(\d{8}T\d{6}Z))\.tgz$")
CLAIM_RE = re.compile(r"^(pi3-backup-\d{8}T\d{6}Z)\.claim-([0-9a-f]{16})$")
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_CHECKSUM_BYTES = 4096
MAX_MEMBERS = 50_000
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_BACKUP_AGE_HOURS = 48
MAX_FUTURE_SKEW_SECONDS = 3600
REQUIRED_COMPONENTS = {
    "rp3-status",
    "operations-state",
    "notification-state",
    "alert-outbox",
    "nginx-config",
    "wedding-site",
    "work-website",
    "release-manifests",
    "deployment-metadata",
}
REQUIRED_PATHS = {
    "services/rp3-status": "directory",
    "state/operations": "directory",
    "state/notifications": "directory",
    "state/alert-relay/outbox.sqlite3": "file",
    "config/nginx": "directory",
    "sites/wedding": "directory",
    "sites/work-website": "directory",
    "manifests": "directory",
    "deployment": "directory",
}


class VerificationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.public_message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def regular_open(path: Path, expected_uid: int, max_bytes: int) -> tuple[BinaryIO, os.stat_result]:
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != expected_uid
        or before.st_size <= 0
        or before.st_size > max_bytes
    ):
        raise VerificationError("claimed_file_invalid", "claimed backup entry is not a bounded root-owned regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size)
    ):
        os.close(descriptor)
        raise VerificationError("claimed_file_race", "claimed backup entry changed during open")
    return os.fdopen(descriptor, "rb", closefd=True), opened


def read_json_regular(path: Path, expected_uid: int, max_bytes: int = 65536) -> dict[str, object]:
    handle, _metadata = regular_open(path, expected_uid, max_bytes)
    try:
        raw = handle.read(max_bytes + 1)
    finally:
        handle.close()
    try:
        payload = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError):
        raise VerificationError("metadata_invalid", "claimed backup metadata is invalid") from None
    if not isinstance(payload, dict):
        raise VerificationError("metadata_invalid", "claimed backup metadata is invalid")
    return payload


def active_claim() -> dict[str, object]:
    payload = read_json_regular(ACTIVE_FILE, ROOT_UID)
    claim_id = str(payload.get("claimId") or "")
    backup_id = str(payload.get("backupId") or "")
    match = CLAIM_RE.fullmatch(claim_id)
    if (
        payload.get("schemaVersion") != 1
        or not match
        or match.group(1) != backup_id
        or not ARCHIVE_RE.fullmatch(f"{backup_id}.tgz")
    ):
        raise VerificationError("claim_invalid", "active backup claim identity is invalid")
    return payload


def sha256_handle(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(archive: Path, checksum: Path) -> tuple[str, int]:
    checksum_handle, _checksum_stat = regular_open(checksum, ROOT_UID, MAX_CHECKSUM_BYTES)
    try:
        raw = checksum_handle.read(MAX_CHECKSUM_BYTES + 1)
    finally:
        checksum_handle.close()
    try:
        line = raw.decode("utf-8", "strict").splitlines()[0].strip()
    except (UnicodeError, IndexError):
        raise VerificationError("checksum_invalid", "Pi3 backup checksum file is invalid") from None
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+[ *]?([^/\s]+)", line)
    if not match or match.group(2) != archive.name:
        raise VerificationError("checksum_invalid", "Pi3 backup checksum declaration is invalid")
    archive_handle, archive_stat = regular_open(archive, ROOT_UID, MAX_ARCHIVE_BYTES)
    try:
        actual = sha256_handle(archive_handle)
    finally:
        archive_handle.close()
    if actual != match.group(1).lower():
        raise VerificationError("checksum_mismatch", "Pi3 backup checksum does not match")
    return actual, archive_stat.st_size


def safe_member_path(member: tarfile.TarInfo, expected_root: str) -> PurePosixPath:
    path = PurePosixPath(member.name)
    if path.is_absolute() or not path.parts or path.parts[0] != expected_root or ".." in path.parts:
        raise VerificationError("archive_unsafe", "Pi3 backup contains an unsafe path")
    if not (member.isdir() or member.isreg()):
        raise VerificationError("archive_unsafe", "Pi3 backup contains a link or special entry")
    if member.size < 0:
        raise VerificationError("archive_unsafe", "Pi3 backup contains an invalid entry size")
    return path


def extract_isolated(archive: Path, destination: Path) -> tuple[Path, int, int]:
    expected_root = archive.name.removesuffix(".tgz")
    seen: set[str] = set()
    member_count = 0
    expanded_bytes = 0
    archive_handle, _archive_stat = regular_open(archive, ROOT_UID, MAX_ARCHIVE_BYTES)
    try:
        try:
            bundle = tarfile.open(fileobj=archive_handle, mode="r:gz")
        except (OSError, tarfile.TarError):
            raise VerificationError("archive_invalid", "Pi3 backup archive is unreadable") from None
        with bundle:
            for member in bundle:
                member_count += 1
                if member_count > MAX_MEMBERS:
                    raise VerificationError("archive_limit", "Pi3 backup contains too many entries")
                path = safe_member_path(member, expected_root)
                normalized = str(path)
                if normalized in seen:
                    raise VerificationError("archive_unsafe", "Pi3 backup contains duplicate paths")
                seen.add(normalized)
                expanded_bytes += member.size
                if expanded_bytes > MAX_EXPANDED_BYTES:
                    raise VerificationError("archive_limit", "Pi3 backup exceeds the expanded-size limit")
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise VerificationError("archive_invalid", "Pi3 backup contains an unreadable file")
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
                with source, os.fdopen(descriptor, "wb", closefd=True) as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    finally:
        archive_handle.close()
    if member_count == 0:
        raise VerificationError("archive_invalid", "Pi3 backup archive is empty")
    return destination / expected_root, member_count, expanded_bytes


def parse_created_at(value: object) -> tuple[str, float]:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise VerificationError("manifest_invalid", "Pi3 backup manifest timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise VerificationError("manifest_invalid", "Pi3 backup manifest timestamp must include a timezone")
    timestamp = parsed.timestamp()
    age_seconds = time.time() - timestamp
    if age_seconds < -MAX_FUTURE_SKEW_SECONDS or age_seconds > MAX_BACKUP_AGE_HOURS * 3600:
        raise VerificationError("backup_stale", "Pi3 backup creation timestamp is stale or implausible")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), timestamp


def parse_manifest(payload_root: Path, backup_id: str) -> tuple[dict[str, object], str]:
    manifest_path = payload_root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise VerificationError("manifest_missing", "Pi3 backup manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise VerificationError("manifest_invalid", "Pi3 backup manifest is invalid") from None
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1 or manifest.get("backupId") != backup_id:
        raise VerificationError("manifest_invalid", "Pi3 backup manifest identity is invalid")
    components = manifest.get("components")
    component_set = {str(item) for item in components} if isinstance(components, list) else set()
    if not isinstance(components, list) or len(components) != len(REQUIRED_COMPONENTS) or component_set != REQUIRED_COMPONENTS:
        raise VerificationError("manifest_incomplete", "Pi3 backup manifest components do not exactly match the required set")
    created_at, _timestamp = parse_created_at(manifest.get("createdAt"))
    id_match = ARCHIVE_RE.fullmatch(f"{backup_id}.tgz")
    if id_match is None:
        raise VerificationError("manifest_invalid", "Pi3 backup identifier is invalid")
    id_timestamp = datetime.strptime(id_match.group(2), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
    if abs(_timestamp - id_timestamp) > 3600:
        raise VerificationError("manifest_invalid", "Pi3 backup identifier and creation timestamp do not agree")
    return manifest, created_at


def require_payload_paths(payload_root: Path) -> None:
    for relative, expected_type in REQUIRED_PATHS.items():
        target = payload_root / relative
        exists = target.is_file() if expected_type == "file" else target.is_dir()
        if not exists or target.is_symlink():
            raise VerificationError("payload_incomplete", "Pi3 backup payload is missing required data")
        if expected_type == "directory" and not any(target.rglob("*")):
            raise VerificationError("payload_incomplete", "Pi3 backup payload contains an empty required component")


def verify_alert_outbox(payload_root: Path) -> None:
    for relative in (
        "state/operations/status.db",
        "state/operations/operations.db",
        "state/alert-relay/outbox.sqlite3",
    ):
        database = payload_root / relative
        if not database.is_file() or database.is_symlink():
            raise VerificationError("sqlite_invalid", "Pi3 backup is missing a required SQLite database")
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
            try:
                rows = connection.execute("PRAGMA quick_check").fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            raise VerificationError("sqlite_invalid", "Pi3 backup contains an unreadable SQLite database") from None
        if rows != [("ok",)]:
            raise VerificationError("sqlite_invalid", "Pi3 backup SQLite quick_check failed")


def sha256_path(path: Path) -> str:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise VerificationError("payload_checksum", "payload checksum target is not a regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        return sha256_handle(handle)


def regular_inventory(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode):
            raise VerificationError("payload_unsafe", "payload contains a symbolic link")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise VerificationError("payload_unsafe", "payload contains a special or multi-link file")
        files.append(path)
    return files


def verify_checksum_file(scope_root: Path, checksum_file: Path, expected_files: set[str]) -> None:
    if not checksum_file.is_file() or checksum_file.is_symlink():
        raise VerificationError("payload_checksum", "required payload checksum manifest is missing")
    try:
        lines = checksum_file.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError):
        raise VerificationError("payload_checksum", "payload checksum manifest is unreadable") from None
    if not lines or len(lines) > MAX_MEMBERS:
        raise VerificationError("payload_checksum", "payload checksum manifest has an invalid entry count")
    declared: set[str] = set()
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (\./[^\x00\r\n]+)", line)
        if not match:
            raise VerificationError("payload_checksum", "payload checksum declaration is malformed")
        relative_text = match.group(2)[2:]
        relative = PurePosixPath(relative_text)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts or relative_text in declared:
            raise VerificationError("payload_checksum", "payload checksum declaration is unsafe or duplicated")
        target = scope_root.joinpath(*relative.parts)
        if not target.is_file() or target.is_symlink() or sha256_path(target) != match.group(1):
            raise VerificationError("payload_checksum", "payload checksum validation failed")
        declared.add(relative_text)
    if declared != expected_files:
        raise VerificationError("payload_checksum", "payload checksum manifest does not exactly cover its scope")


def verify_payload_checksums(payload_root: Path) -> None:
    checksum_file = payload_root / "manifests/SHA256SUMS"
    expected = {
        path.relative_to(payload_root).as_posix()
        for path in regular_inventory(payload_root)
        if path != checksum_file
    }
    verify_checksum_file(payload_root, checksum_file, expected)


def static_inventory(root: Path) -> list[dict[str, object]]:
    inventory = []
    for path in regular_inventory(root):
        relative = path.relative_to(root).as_posix()
        if any(character in relative for character in ("\n", "\r", "\t", "\x00")):
            raise VerificationError("release_manifest", "static release contains an unsafe filename")
        inventory.append({"path": relative, "sha256": sha256_path(path), "size": path.stat().st_size})
    if not inventory:
        raise VerificationError("release_manifest", "static release is empty")
    return inventory


def verify_static_release(payload_root: Path, site: str) -> None:
    metadata_path = payload_root / f"manifests/{site}-release.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise VerificationError("release_manifest", "static release metadata is invalid") from None
    release_name = str(metadata.get("releaseName") or "") if isinstance(metadata, dict) else ""
    if (
        not isinstance(metadata, dict)
        or metadata.get("schemaVersion") != 1
        or metadata.get("site") != site
        or not re.fullmatch(r"[0-9a-f]{64}", release_name)
    ):
        raise VerificationError("release_manifest", "static release identity is invalid")
    reviewed_path = payload_root / f"manifests/{site}/{release_name}.json"
    try:
        reviewed = json.loads(reviewed_path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise VerificationError("release_manifest", "reviewed static release manifest is invalid") from None
    actual = static_inventory(payload_root / f"sites/{site}")
    canonical = "".join(f'{item["sha256"]}  {item["size"]}  {item["path"]}\n' for item in actual)
    tree_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if (
        not isinstance(reviewed, dict)
        or reviewed.get("schemaVersion") != 1
        or reviewed.get("algorithm") != "sha256"
        or reviewed.get("files") != actual
        or reviewed.get("treeSha256") != tree_digest
        or release_name != tree_digest
    ):
        raise VerificationError("release_manifest", "reviewed static release manifest does not match its payload")
    site_checksum = payload_root / f"manifests/{site}-SHA256SUMS"
    expected = {path.relative_to(payload_root / f"sites/{site}").as_posix() for path in regular_inventory(payload_root / f"sites/{site}")}
    verify_checksum_file(payload_root / f"sites/{site}", site_checksum, expected)


def verify_claimed(active: dict[str, object]) -> dict[str, object]:
    claim_id = str(active["claimId"])
    backup_id = str(active["backupId"])
    claim_root = PROCESSING_ROOT / claim_id
    claim_metadata = os.lstat(claim_root)
    if not stat.S_ISDIR(claim_metadata.st_mode) or stat.S_ISLNK(claim_metadata.st_mode) or claim_metadata.st_uid != ROOT_UID:
        raise VerificationError("claim_invalid", "claimed backup directory is invalid")
    claim_document = read_json_regular(claim_root / "claim.json", ROOT_UID)
    if claim_document.get("claimId") != claim_id or claim_document.get("backupId") != backup_id:
        raise VerificationError("claim_invalid", "claimed backup metadata identity is invalid")
    archive = claim_root / str(active.get("archiveName") or "")
    checksum = claim_root / str(active.get("checksumName") or "")
    if archive.name != f"{backup_id}.tgz" or checksum.name != f"{backup_id}.tgz.sha256":
        raise VerificationError("claim_invalid", "claimed backup filenames are invalid")
    digest, archive_bytes = verify_checksum(archive, checksum)
    work = Path(tempfile.mkdtemp(prefix=".isolated-", dir=RESTORE_ROOT))
    try:
        payload_root, member_count, expanded_bytes = extract_isolated(archive, work)
        _manifest, backup_created_at = parse_manifest(payload_root, backup_id)
        require_payload_paths(payload_root)
        verify_payload_checksums(payload_root)
        verify_alert_outbox(payload_root)
        verify_static_release(payload_root, "wedding")
        verify_static_release(payload_root, "work-website")
    finally:
        shutil.rmtree(work)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "ok",
        "checkedAt": utc_now(),
        "backupId": backup_id,
        "claimId": claim_id,
        "backupCreatedAt": backup_created_at,
        "sha256": digest,
        "archiveBytes": archive_bytes,
        "memberCount": member_count,
        "expandedBytes": expanded_bytes,
        "componentsVerified": len(REQUIRED_COMPONENTS),
        "alertOutboxQuickCheck": "ok",
        "liveServicesChanged": False,
    }


def write_result(claim_id: str, document: dict[str, object]) -> None:
    if not CLAIM_RE.fullmatch(claim_id):
        raise VerificationError("claim_invalid", "result claim identifier is invalid")
    cleanup_result_temps(claim_id)
    path = RESULT_ROOT / f"{claim_id}.json"
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise VerificationError("result_exists", "backup verification result already exists")
    temporary = RESULT_ROOT / f".{claim_id}.json.tmp-{uuid.uuid4().hex[:16]}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(document, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_result_directory()
    finally:
        temporary.unlink(missing_ok=True)


def fsync_result_directory() -> None:
    descriptor = os.open(RESULT_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cleanup_result_temps(claim_id: str) -> int:
    if not CLAIM_RE.fullmatch(claim_id):
        raise VerificationError("claim_invalid", "result claim identifier is invalid")
    prefix = f".{claim_id}.json.tmp-"
    pattern = re.compile(rf"^{re.escape(prefix)}[0-9a-f]{{16}}$")
    removed = 0
    with os.scandir(RESULT_ROOT) as iterator:
        for entry in iterator:
            if not entry.name.startswith(prefix):
                continue
            metadata = os.lstat(entry.path)
            if (
                not pattern.fullmatch(entry.name)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
            ):
                raise VerificationError("result_temp_unsafe", "partial backup verification result is unsafe")
            os.unlink(entry.path)
            removed += 1
    if removed:
        fsync_result_directory()
    return removed


def existing_result(active: dict[str, object]) -> dict[str, object] | None:
    claim_id = str(active["claimId"])
    backup_id = str(active["backupId"])
    cleanup_result_temps(claim_id)
    path = RESULT_ROOT / f"{claim_id}.json"
    if not path.exists():
        return None
    document = read_json_regular(path, os.geteuid())
    if (
        document.get("schemaVersion") != SCHEMA_VERSION
        or document.get("claimId") != claim_id
        or document.get("backupId") != backup_id
        or document.get("status") not in {"ok", "fail"}
        or document.get("liveServicesChanged") is not False
    ):
        raise VerificationError("result_invalid", "existing backup verification result is invalid")
    return document


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments != ["--claimed"]:
        print(json.dumps({"schemaVersion": 1, "status": "fail", "error": {"code": "invalid_request", "message": "exactly --claimed is required"}}, separators=(",", ":")))
        return 64
    try:
        active = active_claim()
    except (VerificationError, OSError):
        print(json.dumps({"schemaVersion": 1, "status": "fail", "error": {"code": "claim_unavailable", "message": "active root claim is unavailable"}}, separators=(",", ":")))
        return 1
    claim_id = str(active["claimId"])
    backup_id = str(active["backupId"])
    try:
        previous = existing_result(active)
    except (VerificationError, OSError):
        print(json.dumps({"schemaVersion": 1, "status": "fail", "error": {"code": "result_invalid", "message": "existing backup verification result is invalid"}}, separators=(",", ":")))
        return 1
    if previous is not None:
        print(json.dumps(previous, separators=(",", ":"), sort_keys=True))
        return 0 if previous.get("status") == "ok" else 1
    try:
        document = verify_claimed(active)
    except VerificationError as exc:
        document = {
            "schemaVersion": 1,
            "status": "fail",
            "checkedAt": utc_now(),
            "backupId": backup_id,
            "claimId": claim_id,
            "error": {"code": exc.code, "message": exc.public_message},
            "liveServicesChanged": False,
        }
    except Exception:
        document = {
            "schemaVersion": 1,
            "status": "fail",
            "checkedAt": utc_now(),
            "backupId": backup_id,
            "claimId": claim_id,
            "error": {"code": "verification_failure", "message": "Pi3 backup restore-check could not complete"},
            "liveServicesChanged": False,
        }
    try:
        write_result(claim_id, document)
    except (OSError, VerificationError):
        print(json.dumps({"schemaVersion": 1, "status": "fail", "error": {"code": "result_write_failed", "message": "backup verification result could not be recorded"}}, separators=(",", ":")))
        return 1
    print(json.dumps(document, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
