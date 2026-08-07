#!/usr/bin/python3
"""Minimal root boundary for claiming and finalizing untrusted Pi3 backups."""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
ROOT_UID = 0
ROOT_GID = 0
BASE_ROOT = Path("/mnt/ssd/backups/pi3")
INCOMING_ROOT = BASE_ROOT / "incoming"
PROCESSING_ROOT = BASE_ROOT / "processing"
VERIFIED_ROOT = BASE_ROOT / "verified"
REJECTED_ROOT = BASE_ROOT / "rejected"
RESTORE_ROOT = BASE_ROOT / "restore-check"
STATE_ROOT = Path("/var/lib/pi3-backup")
RESULT_ROOT = STATE_ROOT / "results"
ACTIVE_FILE = STATE_ROOT / "active-claim.json"
STATE_FILE = STATE_ROOT / "last-restore-check.json"
STORAGE_STATE_FILE = STATE_ROOT / "storage-state.json"
LOCK_FILE = Path("/run/lock/pi3-backup-claim.lock")

ARCHIVE_RE = re.compile(r"^(pi3-backup-(\d{8}T\d{6}Z))\.tgz$")
CLAIM_RE = re.compile(r"^(pi3-backup-\d{8}T\d{6}Z)\.claim-([0-9a-f]{16})$")
CLAIM_JSON_TEMP_RE = re.compile(r"^\.claim\.json-[a-z0-9_]{8}$")
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_CHECKSUM_BYTES = 4096
MAX_INCOMING_FILES = 12
MAX_INCOMING_BYTES = 4 * 1024 * 1024 * 1024
MAX_RESULTS_FILES = 8
VERIFIED_KEEP_COUNT = 14
VERIFIED_KEEP_DAYS = 30
REJECTED_KEEP_COUNT = 8


class ClaimError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.public_message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def account_ids(name: str) -> tuple[int, int]:
    record = pwd.getpwnam(name)
    return record.pw_uid, record.pw_gid


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_rename_parents(source_parent: Path, destination_parent: Path) -> None:
    fsync_directory(destination_parent)
    if source_parent != destination_parent:
        fsync_directory(source_parent)


def atomic_json(path: Path, payload: dict[str, object], mode: int, uid: int, gid: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        mode_descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fchmod(mode_descriptor, mode)
        finally:
            os.close(mode_descriptor)
        os.chown(temporary, uid, gid, follow_symlinks=False)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def secure_regular_stat_owners(path: Path, expected_uids: set[int], max_bytes: int) -> os.stat_result:
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid not in expected_uids:
        raise ClaimError("upload_type", "uploaded backup entries must be single-link regular files")
    if before.st_size <= 0 or before.st_size > max_bytes:
        raise ClaimError("upload_size", "uploaded backup entry exceeds its size limit")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size)
    ):
        raise ClaimError("upload_race", "uploaded backup entry changed during claim")
    return opened


def secure_regular_stat(path: Path, expected_uid: int, max_bytes: int) -> os.stat_result:
    return secure_regular_stat_owners(path, {expected_uid}, max_bytes)


def path_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def incoming_inventory(expected_uid: int) -> tuple[list[tuple[Path, os.stat_result]], int]:
    entries: list[tuple[Path, os.stat_result]] = []
    total_bytes = 0
    with os.scandir(INCOMING_ROOT) as iterator:
        for entry in iterator:
            path = Path(entry.path)
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != expected_uid:
                raise ClaimError("incoming_unsafe", "incoming backup subtree contains an unsafe entry")
            entries.append((path, metadata))
            total_bytes += metadata.st_size
    if len(entries) > MAX_INCOMING_FILES or total_bytes > MAX_INCOMING_BYTES:
        raise ClaimError("incoming_quota", "incoming backup subtree exceeds its file or byte quota")
    return entries, total_bytes


def newest_pair(entries: list[tuple[Path, os.stat_result]]) -> tuple[str, Path, Path]:
    by_name = {path.name: path for path, _metadata in entries}
    candidates: list[tuple[str, Path, Path]] = []
    for name, archive in by_name.items():
        match = ARCHIVE_RE.fullmatch(name)
        if not match:
            continue
        checksum = by_name.get(f"{name}.sha256")
        if checksum is not None:
            candidates.append((match.group(1), archive, checksum))
    if not candidates:
        raise ClaimError("backup_missing", "no complete Pi3 backup pair was found")
    return max(candidates, key=lambda item: item[0])


def ensure_layout() -> tuple[int, int, int, int]:
    uploader_uid, uploader_gid = account_ids("pi3backup")
    verifier_uid, verifier_gid = account_ids("pi3verify")
    for path in (PROCESSING_ROOT, VERIFIED_ROOT, REJECTED_ROOT, RESTORE_ROOT, STATE_ROOT, RESULT_ROOT):
        if path.is_symlink():
            raise ClaimError("layout_unsafe", "backup processing layout contains a symbolic link")
    return uploader_uid, uploader_gid, verifier_uid, verifier_gid


def validate_claim_identity(active: dict[str, object]) -> tuple[str, str, str, str]:
    claim_id = str(active.get("claimId") or "")
    backup_id = str(active.get("backupId") or "")
    archive_name = str(active.get("archiveName") or "")
    checksum_name = str(active.get("checksumName") or "")
    match = CLAIM_RE.fullmatch(claim_id)
    if (
        active.get("schemaVersion") != SCHEMA_VERSION
        or match is None
        or match.group(1) != backup_id
        or archive_name != f"{backup_id}.tgz"
        or checksum_name != f"{backup_id}.tgz.sha256"
    ):
        raise ClaimError("claim_invalid", "active Pi3 backup claim identity is invalid")
    return claim_id, backup_id, archive_name, checksum_name


def validate_claim_directory(claim_path: Path, active: dict[str, object]) -> None:
    claim_id, _backup_id, archive_name, checksum_name = validate_claim_identity(active)
    metadata = os.lstat(claim_path)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != ROOT_UID:
        raise ClaimError("claim_invalid", "Pi3 backup claim directory is invalid")
    with os.scandir(claim_path) as iterator:
        names = {entry.name for entry in iterator}
    if names != {archive_name, checksum_name, "claim.json"}:
        raise ClaimError("claim_ambiguous", "Pi3 backup claim directory contents are incomplete or ambiguous")
    claim_document = read_json_regular(claim_path / "claim.json", ROOT_UID)
    for key in ("schemaVersion", "claimId", "backupId", "archiveName", "checksumName", "archiveBytes"):
        if claim_document.get(key) != active.get(key):
            raise ClaimError("claim_invalid", "Pi3 backup claim metadata does not agree")
    if claim_document.get("claimId") != claim_id:
        raise ClaimError("claim_invalid", "Pi3 backup claim directory identity does not agree")
    archive = secure_regular_stat(claim_path / archive_name, ROOT_UID, MAX_ARCHIVE_BYTES)
    secure_regular_stat(claim_path / checksum_name, ROOT_UID, MAX_CHECKSUM_BYTES)
    if archive.st_size != active.get("archiveBytes"):
        raise ClaimError("claim_invalid", "Pi3 backup claim archive size does not agree")


def processing_claim_directories() -> list[Path]:
    claims: list[Path] = []
    with os.scandir(PROCESSING_ROOT) as iterator:
        for entry in iterator:
            path = Path(entry.path)
            metadata = os.lstat(path)
            if not CLAIM_RE.fullmatch(entry.name) or not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != ROOT_UID:
                raise ClaimError("claim_ambiguous", "processing contains an unexpected or unsafe claim entry")
            claims.append(path)
    claims.sort(key=lambda item: item.name)
    return claims


def cleanup_orphan_claim_temps(claim_path: Path) -> int:
    removed = 0
    with os.scandir(claim_path) as iterator:
        for entry in iterator:
            if not entry.name.startswith(".claim.json-"):
                continue
            metadata = os.lstat(entry.path)
            if (
                not CLAIM_JSON_TEMP_RE.fullmatch(entry.name)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != ROOT_UID
            ):
                raise ClaimError("claim_ambiguous", "orphan claim metadata temporary file is unsafe")
            os.unlink(entry.path)
            removed += 1
    if removed:
        fsync_directory(claim_path)
    return removed


def active_document() -> dict[str, object]:
    active = read_json_regular(ACTIVE_FILE, ROOT_UID)
    validate_claim_identity(active)
    return active


def locate_active_claim(active: dict[str, object]) -> tuple[str, Path]:
    claim_id, backup_id, _archive_name, _checksum_name = validate_claim_identity(active)
    processing = PROCESSING_ROOT / claim_id
    verified = VERIFIED_ROOT / backup_id
    rejected = REJECTED_ROOT / f"{claim_id}.rejected"
    locations = [
        (name, path)
        for name, path in (("processing", processing), ("verified", verified), ("rejected", rejected))
        if path_exists(path)
    ]
    if len(locations) != 1:
        raise ClaimError("claim_ambiguous", "active Pi3 backup claim has an ambiguous or missing storage location")
    processing_entries = processing_claim_directories()
    expected_processing = [processing] if locations[0][0] == "processing" else []
    if processing_entries != expected_processing:
        raise ClaimError("claim_ambiguous", "processing contains a claim that does not match the active document")
    validate_claim_directory(locations[0][1], active)
    return locations[0]


def active_claim() -> dict[str, object]:
    active = active_document()
    location, _path = locate_active_claim(active)
    if location != "processing":
        raise ClaimError("claim_finalized", "active Pi3 backup claim has already left processing")
    return active


def claim_pair() -> dict[str, object]:
    uploader_uid, _uploader_gid, _verifier_uid, verifier_gid = ensure_layout()
    if path_exists(ACTIVE_FILE):
        raise ClaimError("claim_active", "a Pi3 backup claim is already active")
    entries, incoming_bytes = incoming_inventory(uploader_uid)
    backup_id, archive, checksum = newest_pair(entries)
    archive_stat = secure_regular_stat(archive, uploader_uid, MAX_ARCHIVE_BYTES)
    checksum_stat = secure_regular_stat(checksum, uploader_uid, MAX_CHECKSUM_BYTES)

    claim_id = f"{backup_id}.claim-{uuid.uuid4().hex[:16]}"
    claim_path = PROCESSING_ROOT / claim_id
    claim_path.mkdir(mode=0o750)
    os.chown(claim_path, ROOT_UID, verifier_gid)
    claimed_archive = claim_path / archive.name
    claimed_checksum = claim_path / checksum.name
    moved: list[Path] = []
    try:
        os.rename(archive, claimed_archive)
        moved.append(claimed_archive)
        after_archive = secure_regular_stat(claimed_archive, uploader_uid, MAX_ARCHIVE_BYTES)
        if (after_archive.st_dev, after_archive.st_ino) != (archive_stat.st_dev, archive_stat.st_ino):
            raise ClaimError("upload_race", "uploaded archive changed during claim")
        os.rename(checksum, claimed_checksum)
        moved.append(claimed_checksum)
        after_checksum = secure_regular_stat(claimed_checksum, uploader_uid, MAX_CHECKSUM_BYTES)
        if (after_checksum.st_dev, after_checksum.st_ino) != (checksum_stat.st_dev, checksum_stat.st_ino):
            raise ClaimError("upload_race", "uploaded checksum changed during claim")
        for path in moved:
            os.chown(path, ROOT_UID, verifier_gid, follow_symlinks=False)
            os.chmod(path, 0o440, follow_symlinks=False)
        claim_document = {
            "schemaVersion": SCHEMA_VERSION,
            "claimId": claim_id,
            "backupId": backup_id,
            "archiveName": claimed_archive.name,
            "checksumName": claimed_checksum.name,
            "archiveBytes": archive_stat.st_size,
            "claimedAt": utc_now(),
        }
        atomic_json(claim_path / "claim.json", claim_document, 0o440, ROOT_UID, verifier_gid)
        atomic_json(ACTIVE_FILE, claim_document, 0o440, ROOT_UID, verifier_gid)
    except Exception:
        quarantine = REJECTED_ROOT / f"{claim_id}.claim-failed"
        if claim_path.exists() and not claim_path.is_symlink():
            make_root_only(claim_path)
            os.rename(claim_path, quarantine)
        raise
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "claimed",
        "claimId": claim_id,
        "backupId": backup_id,
        "archiveBytes": archive_stat.st_size,
        "incomingFiles": len(entries),
        "incomingBytes": incoming_bytes,
    }


def recover_orphan_claim() -> dict[str, object] | None:
    uploader_uid, _uploader_gid = account_ids("pi3backup")
    _verifier_uid, verifier_gid = account_ids("pi3verify")
    claims = processing_claim_directories()
    if not claims:
        return None
    if len(claims) != 1:
        raise ClaimError("claim_ambiguous", "more than one orphan Pi3 backup processing claim exists")
    claim_path = claims[0]
    match = CLAIM_RE.fullmatch(claim_path.name)
    if match is None:
        raise ClaimError("claim_invalid", "orphan Pi3 backup claim path is invalid")
    claim_id, backup_id = claim_path.name, match.group(1)
    archive_name = f"{backup_id}.tgz"
    checksum_name = f"{archive_name}.sha256"
    cleanup_orphan_claim_temps(claim_path)
    expected_names = {archive_name, checksum_name, "claim.json"}
    with os.scandir(claim_path) as iterator:
        actual_names = {entry.name for entry in iterator}
    if not actual_names.issubset(expected_names):
        raise ClaimError("claim_ambiguous", "orphan Pi3 backup claim contains unexpected entries")
    if path_exists(VERIFIED_ROOT / backup_id) or path_exists(REJECTED_ROOT / f"{claim_id}.rejected"):
        raise ClaimError("claim_ambiguous", "orphan Pi3 backup claim also exists in finalized storage")

    limits = {archive_name: MAX_ARCHIVE_BYTES, checksum_name: MAX_CHECKSUM_BYTES}
    for name, limit in limits.items():
        processing_file = claim_path / name
        incoming_file = INCOMING_ROOT / name
        in_processing = path_exists(processing_file)
        in_incoming = path_exists(incoming_file)
        if in_processing and in_incoming:
            raise ClaimError("claim_ambiguous", "orphan claim file exists in both processing and incoming")
        if in_processing:
            secure_regular_stat_owners(processing_file, {ROOT_UID, uploader_uid}, limit)
        elif in_incoming:
            secure_regular_stat(incoming_file, uploader_uid, limit)

    complete = all(path_exists(claim_path / name) for name in limits)
    if not complete:
        for name, limit in limits.items():
            incoming_file = INCOMING_ROOT / name
            if not path_exists(claim_path / name) and path_exists(incoming_file):
                before = secure_regular_stat(incoming_file, uploader_uid, limit)
                destination = claim_path / name
                os.rename(incoming_file, destination)
                after = secure_regular_stat(destination, uploader_uid, limit)
                if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
                    raise ClaimError("upload_race", "orphan backup entry changed during quarantine")
        quarantine = REJECTED_ROOT / f"{claim_id}.claim-failed"
        if path_exists(quarantine):
            raise ClaimError("claim_ambiguous", "orphan claim quarantine destination already exists")
        make_root_only(claim_path)
        os.rename(claim_path, quarantine)
        fsync_rename_parents(PROCESSING_ROOT, REJECTED_ROOT)
        atomic_json(
            STATE_FILE,
            {
                "schemaVersion": SCHEMA_VERSION,
                "status": "fail",
                "checkedAt": utc_now(),
                "backupId": backup_id,
                "error": {
                    "code": "orphan_incomplete",
                    "message": "interrupted Pi3 backup claim was incomplete and quarantined",
                },
                "liveServicesChanged": False,
            },
            0o600,
            ROOT_UID,
            ROOT_GID,
        )
        return {
            "schemaVersion": SCHEMA_VERSION,
            "status": "quarantined",
            "claimId": claim_id,
            "backupId": backup_id,
        }

    for name in limits:
        path = claim_path / name
        os.chown(path, ROOT_UID, verifier_gid, follow_symlinks=False)
        os.chmod(path, 0o440, follow_symlinks=False)
    archive_bytes = secure_regular_stat(claim_path / archive_name, ROOT_UID, MAX_ARCHIVE_BYTES).st_size
    claim_document_path = claim_path / "claim.json"
    if path_exists(claim_document_path):
        claim_document = read_json_regular(claim_document_path, ROOT_UID)
    else:
        claim_document = {
            "schemaVersion": SCHEMA_VERSION,
            "claimId": claim_id,
            "backupId": backup_id,
            "archiveName": archive_name,
            "checksumName": checksum_name,
            "archiveBytes": archive_bytes,
            "claimedAt": utc_now(),
        }
        atomic_json(claim_document_path, claim_document, 0o440, ROOT_UID, verifier_gid)
    if claim_document.get("claimId") != claim_id:
        raise ClaimError("claim_invalid", "orphan Pi3 backup claim path and metadata do not agree")
    validate_claim_directory(claim_path, claim_document)
    atomic_json(ACTIVE_FILE, claim_document, 0o440, ROOT_UID, verifier_gid)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "recovered",
        "claimId": claim_id,
        "backupId": backup_id,
    }


def prepare_claim() -> dict[str, object]:
    ensure_layout()
    if path_exists(ACTIVE_FILE):
        active = active_document()
        location, _claim_path = locate_active_claim(active)
        if location == "processing":
            return {
                "schemaVersion": SCHEMA_VERSION,
                "status": "resumed",
                "claimId": str(active["claimId"]),
                "backupId": str(active["backupId"]),
            }
        finalize_claim()
    recovered = recover_orphan_claim()
    if recovered is not None and recovered.get("status") == "recovered":
        return recovered
    try:
        return claim_pair()
    except ClaimError as exc:
        if exc.code != "backup_missing":
            raise
        write_storage_state()
        idle = {"schemaVersion": SCHEMA_VERSION, "status": "idle"}
        if recovered is not None:
            idle["reconciled"] = str(recovered.get("status") or "quarantined")
        return idle


def read_json_regular(path: Path, expected_uid: int, max_bytes: int = 65536) -> dict[str, object]:
    metadata = secure_regular_stat(path, expected_uid, max_bytes)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        raw = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError):
        raise ClaimError("result_invalid", "backup verification result is invalid") from None
    if not isinstance(payload, dict):
        raise ClaimError("result_invalid", "backup verification result is invalid")
    return payload


def make_root_only(root: Path) -> None:
    metadata = os.lstat(root)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ClaimError("storage_unsafe", "backup storage contains an unsafe directory")
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            item = os.lstat(path)
            if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
                raise ClaimError("storage_unsafe", "backup storage contains an unsafe file")
            os.chown(path, ROOT_UID, ROOT_GID, follow_symlinks=False)
            os.chmod(path, 0o600, follow_symlinks=False)
        for name in directories:
            path = current_path / name
            item = os.lstat(path)
            if not stat.S_ISDIR(item.st_mode) or stat.S_ISLNK(item.st_mode):
                raise ClaimError("storage_unsafe", "backup storage contains an unsafe directory")
            os.chown(path, ROOT_UID, ROOT_GID, follow_symlinks=False)
            os.chmod(path, 0o700, follow_symlinks=False)
    os.chown(root, ROOT_UID, ROOT_GID, follow_symlinks=False)
    os.chmod(root, 0o700, follow_symlinks=False)


def remove_root_tree(path: Path, expected_parent: Path) -> None:
    resolved_parent = expected_parent.resolve(strict=True)
    if path.parent.resolve(strict=True) != resolved_parent:
        raise ClaimError("retention_unsafe", "retention target escaped its fixed root")
    metadata = os.lstat(path)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != ROOT_UID:
        raise ClaimError("retention_unsafe", "retention target is not a root-owned directory")
    shutil.rmtree(path)


def apply_retention(root: Path, pattern: re.Pattern[str], keep_count: int, keep_days: int | None = None) -> int:
    now = time.time()
    rows: list[tuple[str, Path, os.stat_result]] = []
    with os.scandir(root) as iterator:
        for entry in iterator:
            if not pattern.fullmatch(entry.name):
                continue
            metadata = os.lstat(entry.path)
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and metadata.st_uid == ROOT_UID:
                rows.append((entry.name, Path(entry.path), metadata))
    rows.sort(key=lambda item: item[0], reverse=True)
    removed = 0
    for index, (_name, path, metadata) in enumerate(rows):
        too_many = index >= keep_count
        too_old = keep_days is not None and index >= 2 and now - metadata.st_mtime > keep_days * 86400
        if too_many or too_old:
            remove_root_tree(path, root)
            removed += 1
    return removed


def subtree_stats(root: Path) -> tuple[int, int, int]:
    files = 0
    directories = 0
    total_bytes = 0
    for current, names, filenames in os.walk(root, followlinks=False):
        directories += len(names)
        for name in filenames:
            path = Path(current) / name
            metadata = os.lstat(path)
            if stat.S_ISREG(metadata.st_mode):
                files += 1
                total_bytes += metadata.st_size
    return files, directories, total_bytes


def write_storage_state() -> None:
    payload: dict[str, object] = {"schemaVersion": 1, "checkedAt": utc_now()}
    for name, root in (
        ("incoming", INCOMING_ROOT),
        ("processing", PROCESSING_ROOT),
        ("verified", VERIFIED_ROOT),
        ("rejected", REJECTED_ROOT),
        ("restoreCheck", RESTORE_ROOT),
        ("results", RESULT_ROOT),
    ):
        files, directories, total_bytes = subtree_stats(root)
        payload[name] = {"files": files, "directories": directories, "bytes": total_bytes}
    atomic_json(STORAGE_STATE_FILE, payload, 0o600, ROOT_UID, ROOT_GID)


def cleanup_stale_restore_dirs() -> int:
    removed = 0
    cutoff = time.time() - 86400
    with os.scandir(RESTORE_ROOT) as iterator:
        for entry in iterator:
            if not entry.name.startswith(".isolated-"):
                continue
            metadata = os.lstat(entry.path)
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and metadata.st_mtime < cutoff:
                # Restore trees belong to the unprivileged verifier, not root.
                shutil.rmtree(entry.path)
                removed += 1
    return removed


def verification_result(active: dict[str, object], verifier_uid: int) -> tuple[Path, dict[str, object] | None]:
    claim_id, backup_id, _archive_name, _checksum_name = validate_claim_identity(active)
    result_path = RESULT_ROOT / f"{claim_id}.json"
    if not path_exists(result_path):
        return result_path, None
    result = read_json_regular(result_path, verifier_uid)
    if (
        result.get("schemaVersion") != SCHEMA_VERSION
        or result.get("claimId") != claim_id
        or result.get("backupId") != backup_id
        or result.get("status") not in {"ok", "fail"}
        or result.get("liveServicesChanged") is not False
    ):
        raise ClaimError("result_invalid", "backup verification result identity or status is invalid")
    return result_path, result


def canonical_from_result(active: dict[str, object], result: dict[str, object]) -> tuple[bool, dict[str, object]]:
    _claim_id, backup_id, _archive_name, _checksum_name = validate_claim_identity(active)
    success = (
        result.get("status") == "ok"
        and result.get("alertOutboxQuickCheck") == "ok"
        and isinstance(result.get("backupCreatedAt"), str)
    )
    if success:
        return True, {
            "schemaVersion": SCHEMA_VERSION,
            "status": "ok",
            "checkedAt": str(result.get("checkedAt") or utc_now()),
            "backupId": backup_id,
            "backupCreatedAt": str(result["backupCreatedAt"]),
            "sha256": str(result.get("sha256") or ""),
            "archiveBytes": int(result.get("archiveBytes") or 0),
            "componentsVerified": int(result.get("componentsVerified") or 0),
            "alertOutboxQuickCheck": "ok",
            "liveServicesChanged": False,
        }
    if result.get("status") != "fail":
        raise ClaimError("result_invalid", "successful backup verification result is incomplete")
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    return False, {
        "schemaVersion": SCHEMA_VERSION,
        "status": "fail",
        "checkedAt": str(result.get("checkedAt") or utc_now()),
        "backupId": backup_id,
        "error": {
            "code": str(error.get("code") or "verification_failed")[:64],
            "message": str(error.get("message") or "Pi3 backup verification failed")[:240],
        },
        "liveServicesChanged": False,
    }


def canonical_from_state(active: dict[str, object], location: str) -> tuple[bool, dict[str, object]]:
    _claim_id, backup_id, _archive_name, _checksum_name = validate_claim_identity(active)
    state = read_json_regular(STATE_FILE, ROOT_UID)
    expected_status = "ok" if location == "verified" else "fail"
    if (
        state.get("schemaVersion") != SCHEMA_VERSION
        or state.get("status") != expected_status
        or state.get("backupId") != backup_id
        or state.get("liveServicesChanged") is not False
    ):
        raise ClaimError("claim_ambiguous", "finalized claim and published restore state do not agree")
    if expected_status == "ok":
        if not isinstance(state.get("backupCreatedAt"), str) or state.get("alertOutboxQuickCheck") != "ok":
            raise ClaimError("claim_ambiguous", "published successful restore state is incomplete")
        canonical = {
            "schemaVersion": SCHEMA_VERSION,
            "status": "ok",
            "checkedAt": str(state.get("checkedAt") or utc_now()),
            "backupId": backup_id,
            "backupCreatedAt": str(state["backupCreatedAt"]),
            "sha256": str(state.get("sha256") or ""),
            "archiveBytes": int(state.get("archiveBytes") or 0),
            "componentsVerified": int(state.get("componentsVerified") or 0),
            "alertOutboxQuickCheck": "ok",
            "liveServicesChanged": False,
        }
        return True, canonical
    error = state.get("error") if isinstance(state.get("error"), dict) else None
    if error is None:
        raise ClaimError("claim_ambiguous", "published failed restore state is incomplete")
    canonical = {
        "schemaVersion": SCHEMA_VERSION,
        "status": "fail",
        "checkedAt": str(state.get("checkedAt") or utc_now()),
        "backupId": backup_id,
        "error": {
            "code": str(error.get("code") or "verification_failed")[:64],
            "message": str(error.get("message") or "Pi3 backup verification failed")[:240],
        },
        "liveServicesChanged": False,
    }
    return False, canonical


def canonical_without_result(backup_id: str) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "fail",
        "checkedAt": utc_now(),
        "backupId": backup_id,
        "error": {
            "code": "verifier_no_result",
            "message": "unprivileged backup verifier produced no result",
        },
        "liveServicesChanged": False,
    }


def finalize_claim() -> dict[str, object]:
    _uploader_uid, _uploader_gid, verifier_uid, _verifier_gid = ensure_layout()
    if not path_exists(ACTIVE_FILE):
        write_storage_state()
        return {"schemaVersion": 1, "status": "idle"}
    active = active_document()
    claim_id, backup_id, _archive_name, _checksum_name = validate_claim_identity(active)
    location, claim_path = locate_active_claim(active)
    result_path, result = verification_result(active, verifier_uid)
    if location == "processing":
        if result is None:
            result = {
                "schemaVersion": SCHEMA_VERSION,
                "status": "fail",
                "checkedAt": utc_now(),
                "backupId": backup_id,
                "claimId": claim_id,
                "error": canonical_without_result(backup_id)["error"],
                "liveServicesChanged": False,
            }
        success, canonical = canonical_from_result(active, result)
        destination = VERIFIED_ROOT / backup_id if success else REJECTED_ROOT / f"{claim_id}.rejected"
        if path_exists(destination):
            raise ClaimError("claim_ambiguous", "backup finalization destination already exists")
        make_root_only(claim_path)
        os.rename(claim_path, destination)
        fsync_rename_parents(PROCESSING_ROOT, destination.parent)
        location = "verified" if success else "rejected"
    else:
        fsync_rename_parents(PROCESSING_ROOT, claim_path.parent)
    if location != "processing" and result is not None:
        success, canonical = canonical_from_result(active, result)
        if (location == "verified") != success:
            raise ClaimError("claim_ambiguous", "finalized claim location and verification result do not agree")
    elif location != "processing" and path_exists(STATE_FILE):
        prior_state = read_json_regular(STATE_FILE, ROOT_UID)
        if prior_state.get("backupId") == backup_id:
            success, canonical = canonical_from_state(active, location)
            if (location == "verified") != success:
                raise ClaimError("claim_ambiguous", "finalized claim location and restore state do not agree")
        elif location == "rejected":
            success, canonical = False, canonical_without_result(backup_id)
        else:
            raise ClaimError("claim_ambiguous", "verified claim has neither a verification result nor matching restore state")
    elif location == "rejected":
        success, canonical = False, canonical_without_result(backup_id)
    elif location != "processing":
        raise ClaimError("claim_ambiguous", "verified claim has neither a verification result nor matching restore state")
    atomic_json(STATE_FILE, canonical, 0o600, ROOT_UID, ROOT_GID)
    if path_exists(result_path):
        result_path.unlink()
        fsync_directory(RESULT_ROOT)
    ACTIVE_FILE.unlink()
    fsync_directory(STATE_ROOT)
    removed_verified = apply_retention(VERIFIED_ROOT, re.compile(r"^pi3-backup-\d{8}T\d{6}Z$"), VERIFIED_KEEP_COUNT, VERIFIED_KEEP_DAYS)
    removed_rejected = apply_retention(REJECTED_ROOT, re.compile(r"^pi3-backup-\d{8}T\d{6}Z\.claim-[0-9a-f]{16}\.(?:rejected|claim-failed)$"), REJECTED_KEEP_COUNT)
    cleanup_stale_restore_dirs()
    write_storage_state()
    return {
        "schemaVersion": 1,
        "status": canonical["status"],
        "backupId": backup_id,
        "verifiedRetentionRemoved": removed_verified,
        "rejectedRetentionRemoved": removed_rejected,
    }


def run_locked(action: str) -> dict[str, object]:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if action == "prepare":
            return prepare_claim()
        if action == "claim":
            result = claim_pair()
            write_storage_state()
            return result
        if action == "finalize":
            return finalize_claim()
    raise ClaimError("invalid_request", "claim helper action is not allowed")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2 or arguments[0] != "--action" or arguments[1] not in {"prepare", "claim", "finalize"}:
        print(json.dumps({"schemaVersion": 1, "status": "fail", "error": {"code": "invalid_request", "message": "exact claim helper action is required"}}, separators=(",", ":")))
        return 64
    try:
        result = run_locked(arguments[1])
    except (ClaimError, OSError, KeyError, ValueError) as exc:
        if isinstance(exc, ClaimError):
            code, message = exc.code, exc.public_message
        else:
            code, message = "claim_failure", "Pi3 backup claim helper could not complete"
        print(json.dumps({"schemaVersion": 1, "status": "fail", "error": {"code": code, "message": message}}, separators=(",", ":"), sort_keys=True))
        return 255 if arguments[1] == "prepare" else 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    if arguments[1] == "prepare" and result.get("status") == "idle":
        return 1
    return 1 if arguments[1] == "finalize" and result.get("status") == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
