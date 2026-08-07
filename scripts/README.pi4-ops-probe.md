# Pi4 read-only operations probe

This probe is the only Pi4-local interface needed by the Pi3 operations scheduler. It accepts four literal SSH commands:

```text
probe five-minute
probe hourly
probe morning
probe nightly
```

Each successful request returns one JSON document with exactly four top-level fields: `schemaVersion`, `cadence`, `generatedAt`, and `checks`. Every check has exactly `id`, `label`, `host`, `status`, `message`, and `durationMs`; internal metrics never cross the SSH boundary. A failing health check is represented in JSON while SSH still exits successfully. Invalid commands exit `64`; an internal probe failure exits `70`.

The probe is read-only. Its commands are limited to health/status reads, fixed loopback HTTP GETs, k3s `get`, filesystem metadata and hashes, journal reads, and standby-safe SMART inspection. It never accepts a URL, path, unit, workload, device, or shell fragment from the caller.

## Security boundary

- Dedicated locked `pi4probe` account with public-key-only authentication.
- Key restricted to source address `192.168.0.142` and OpenSSH `restrict` controls.
- sshd `ForceCommand` wrapper with exact string matching; no `eval` or shell interpolation.
- Forwarding, PTY, user rc, and interactive authentication disabled.
- Four exact sudo command lines; the root-owned probe has no general command mode.
- Pi3 uses `StrictHostKeyChecking yes`, a dedicated known-hosts file, and an Ed25519 host-key pin.

## Install on Pi4

Generate a dedicated Ed25519 key on Pi3, store it under `/etc/pi3-ops`, and securely copy only its `.pub` file to Pi4. From this repository on Pi4:

```sh
sudo ./scripts/install-pi4-ops-probe.sh /path/to/pi4-probe-ed25519.pub
```

The installer validates the key, sudoers, and full sshd configuration before reloading SSH. It stages and backs up the probe authorized-key file and sshd drop-in, restoring both and reloading the prior configuration if validation or reload fails. It prints the Pi4 Ed25519 host-key fingerprint. Verify that fingerprint through the Pi4 console or another already-trusted session.

Create `/etc/pi3-ops/pi4-known-hosts` on Pi3 from the independently verified Pi4 public host key:

```text
192.168.0.101 ssh-ed25519 <exact key blob from /etc/ssh/ssh_host_ed25519_key.pub>
```

Install `pi3-pi4-ops-probe.ssh_config` as `/etc/pi3-ops/pi4-probe-ssh.conf`. The unprivileged `rp3status` worker needs read access but must not own or modify the credentials: use a root-owned `/etc/pi3-ops` directory at mode `0750`, then install the private key, known-hosts file, and SSH config as `root:rp3status` at mode `0640`.

## Verify before scheduler integration

From Pi3, all four allowed requests must return valid schema-v1 JSON:

```sh
ssh -F /etc/pi3-ops/pi4-probe-ssh.conf pi4-ops-probe 'probe five-minute'
ssh -F /etc/pi3-ops/pi4-probe-ssh.conf pi4-ops-probe 'probe hourly'
ssh -F /etc/pi3-ops/pi4-probe-ssh.conf pi4-ops-probe 'probe morning'
ssh -F /etc/pi3-ops/pi4-probe-ssh.conf pi4-ops-probe 'probe nightly'
```

Confirm that representative rejected requests fail with exit `64`, including an empty command, `probe`, `probe five-minute `, `probe five-minute; id`, an unknown cadence, and an arbitrary command. Confirm forwarding and PTY requests are rejected. Do not disable Pi4 NOC until Pi3 has consumed each cadence document successfully.

The nightly cadence requires both the root finalizer's verifier timestamp (maximum 26 hours) and the manifest's original backup-creation timestamp (maximum 48 hours). It also inspects the full Pi3 backup subtree for sticky/root-only zone permissions, links or special files, byte/file quotas, and verified-retention pressure.
