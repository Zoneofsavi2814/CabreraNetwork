# Restricted Pi3 backup target on Pi4

The Pi3 backup identity can only upload through `rrsync -wo -no-del -munge` into `/mnt/ssd/backups/pi3/incoming`. It cannot download, delete older archives, open a shell, forward connections, or select another destination. Sender, append, in-place, link, hard-link, device, special-file, and privilege-preservation modes are rejected. The hard ingress limits are exactly 2,147,483,648 bytes per file through `prlimit --fsize`, one serialized upload at a time, and a 20-minute session timeout. Use a dedicated Ed25519 key separate from the operations-probe key.

The receiver checks the incoming aggregate before each session; the root claim helper checks it again before processing; and the nightly operations probe reports file, byte, special-file, and retention drift. These are refusal and monitoring controls, not a filesystem-enforced aggregate quota. One compromised authorized session can upload multiple files of at most 2 GiB each and exceed the aggregate byte or inode threshold before the 20-minute timeout. A hard aggregate/inode ceiling requires enabling a filesystem or project quota on the external HDD, which this installer deliberately does not change. If the threshold is exceeded, claims are refused until an operator removes the explicitly identified incoming files.

Install on Pi4 after the external HDD is mounted:

```sh
sudo ./scripts/install-pi3-backup-target.sh /path/to/pi3-backup-ed25519.pub
```

The installer creates locked `pi3backup` and `pi3verify` accounts, restricts uploads to source address `192.168.0.142`, validates sshd, enables a boot-time reconciliation service, installs a nightly local restore-check timer for 02:10 (after Pi3's 01:50 upload and before Pi4's own backup window), and prints the Pi4 Ed25519 host-key fingerprint. Existing authorized-key and sshd drop-in files are staged and restored automatically if validation or SSH reload fails. Independently verify and pin the host key on Pi3. Install `pi3-pi4-backup-target.ssh_config` as `/etc/pi3-backup/pi4-backup-ssh.conf`; its private key is used by a root-run Pi3 backup service and stays `root:root` mode `0600`.

The storage trust zones are:

- `incoming`: root-owned, sticky, uploader-writable staging.
- `processing`: pairs atomically renamed and converted to root-owned, verifier-read-only files.
- `verified`: successful immutable pairs, root-only.
- `rejected`: failed claims, root-only and count-bounded.
- `restore-check`: temporary extraction space writable only by the unprivileged verifier group.

## Backup wire format

Upload a uniquely named pair in one rsync invocation, with the archive before its checksum. The sender must disable links, devices, specials, and hard links rather than using archive mode:

```sh
rsync -rt --no-links --no-devices --no-specials --no-hard-links \
  -e 'ssh -F /etc/pi3-backup/pi4-backup-ssh.conf' \
  pi3-backup-YYYYMMDDTHHMMSSZ.tgz pi3-backup-YYYYMMDDTHHMMSSZ.tgz.sha256 \
  pi4-backup-target:/
```

The resulting filenames are:

```text
pi3-backup-YYYYMMDDTHHMMSSZ.tgz
pi3-backup-YYYYMMDDTHHMMSSZ.tgz.sha256
```

The checksum file contains the archive's lowercase SHA-256 and basename in standard `sha256sum` format. The archive and checksum must be regular, single-link files owned by the upload account. The archive has exactly one top-level directory matching the archive basename without `.tgz`. Its root contains `manifest.json`:

```json
{
  "schemaVersion": 1,
  "backupId": "pi3-backup-YYYYMMDDTHHMMSSZ",
  "createdAt": "2026-07-22T23:55:00Z",
  "components": [
    "rp3-status",
    "operations-state",
    "notification-state",
    "alert-outbox",
    "nginx-config",
    "wedding-site",
    "work-website",
    "release-manifests",
    "deployment-metadata"
  ]
}
```

The matching required payload paths are:

```text
services/rp3-status/
state/operations/
state/notifications/
state/alert-relay/outbox.sqlite3
config/nginx/
sites/wedding/
sites/work-website/
manifests/
deployment/
```

The Pi3 sender must create consistent copies of the RP3 Status, operations-history, and alert-outbox SQLite databases with SQLite's backup API and pass `PRAGMA quick_check` before archiving them. The verifier repeats `PRAGMA quick_check` on `state/operations/status.db`, `state/operations/operations.db`, and `state/alert-relay/outbox.sqlite3`.

`manifests/SHA256SUMS` must exactly cover every regular payload file except itself. The payload must also include exact site-only checksum lists at `manifests/wedding-SHA256SUMS` and `manifests/work-website-SHA256SUMS`, release pointers named `{site}-release.json`, and the corresponding reviewed manifests at `manifests/{site}/{treeSha256}.json`. A reviewed manifest records every static file's relative path, byte size, and SHA-256; its release name is the SHA-256 of that canonical inventory. This makes a rehashed but unreviewed website payload fail verification.

`createdAt` must include a timezone, be no more than 48 hours old, and agree with the timestamp encoded in `backupId` within one hour. Re-running verification can refresh verifier time but can never make a stale backup creation time healthy.

## Isolated restore check

Run immediately after the first upload; this requires no soak:

```sh
sudo systemctl start pi3-backup-verify.service
sudo systemctl status pi3-backup-verify.service --no-pager
sudo cat /var/lib/pi3-backup/last-restore-check.json
```

The root claim helper uses `lstat`, `O_NOFOLLOW`, owner checks, single-link checks, size limits, and inode comparisons before and after atomic renames. It never parses the archive. The archive parser then runs as locked user `pi3verify` with no capabilities, no network, protected `/proc`, bounded memory/CPU/tasks/files, syscall restrictions, and a 15-minute service timeout. It rejects traversal, links, devices, specials, duplicates, excessive file counts, excessive expanded size, stale manifests, unexpected roots, incomplete checksum coverage, and release payloads that differ from either reviewed site manifest. It validates every component and all three SQLite databases inside a random isolated directory without changing live services.

At boot and on every timer run, the service's root preparation condition either claims a complete incoming pair, reconstructs the metadata and active document for exactly one complete and identity-valid orphan processing claim, validates and resumes an existing claim, reconciles an already-moved verified/rejected claim, or skips cleanly when no work exists. A safe but incomplete orphan and its matching incoming remainder are moved to the fixed rejected area; unexpected, duplicate, or conflicting entries fail closed. Resuming does not require either file to remain in `incoming`.

The unprivileged verifier publishes its result through a same-directory temporary file, fsyncs the file, atomically replaces the final path, and fsyncs the results directory. A retry removes only safe, regular, verifier-owned temporary files under that claim's exact prefix. Root JSON replacements, processing-to-final/quarantine renames, and result/active-document removals fsync the affected parent directories before the next transition. Orphan recovery likewise removes only root-owned, single-link regular `claim.json` temporaries matching the exact `mkstemp` prefix and fails closed on lookalikes. If power is lost after result publication or after the root finalizer moves a claim but before it removes the active document, the next run completes the fixed final state without overwriting or duplicating the claim.

A minimal root finalizer consumes the fixed result, moves successful claims to root-only `verified`, moves failures to root-only `rejected`, publishes `/var/lib/pi3-backup/last-restore-check.json`, and applies retention. Verified storage keeps at most 14 backups and 30 days while preserving at least the two newest; rejected storage keeps at most eight claims. The nightly Pi4 operations probe separately checks verifier freshness, original backup-creation freshness, root-only/sticky permissions, subtree file/byte limits, and retention counts.
