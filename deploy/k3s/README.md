# Pi4 Platform Manifests

These files source-control the small RP4 platform workloads that previously
existed only in live Kubernetes objects or one-off task files. They do not
contain credentials, notification settings, registry authentication, or
Kubernetes Secrets.

## Files And Targets

- `rp4-smoke-registry.yaml` targets the RP4 cluster and owns the `homelab`
  smoke Deployment/Service and local registry Deployment/Service. The host
  directory `/mnt/ssd/registry` must already exist; the manifest fails closed
  if the data HDD is absent instead of creating registry data on the root card.
- `rp5-uptime-kuma.yaml` targets the separate RP5 cluster. It creates the
  `observability` namespace and runs Kuma on node `raspberrypi5`, host port
  `3001`, with data at `/var/lib/uptime-kuma`.
- `rp4-uptime-kuma-rollback.yaml` restores the current RP4 Kuma placement after
  an unsuccessful migration. Never run RP4 and RP5 Kuma simultaneously from
  copies of the same active database.

The immutable third-party artifacts verified on RP4 are:

| Workload | Version | Pinned image |
| --- | --- | --- |
| Registry | 2.8.3 | `docker.io/library/registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373` |
| Uptime Kuma | 1.23.17 | `docker.io/louislam/uptime-kuma@sha256:e4d212bee31351791b96feca913f3dff73e4bfd42a1821a39308d67077c6eb15` |

Smoke remains a cache-only local image because no pullable registry artifact
exists yet. Its Deployment records the verified containerd target digest
`sha256:9834000db9dff07b7ee301ce14ab43b7d249ec11d5d4ad83e3bd39d0751e4446`
and uses `imagePullPolicy: Never`. Export and validate that artifact off-host
before rebooting or upgrading k3s. Replace the local reference with a protected
registry digest after it is published.

## Read-Only Validation

Client-side parsing does not contact either cluster:

```sh
kubectl apply --dry-run=client -f deploy/k3s/rp4-smoke-registry.yaml
kubectl apply --dry-run=client -f deploy/k3s/rp5-uptime-kuma.yaml
kubectl apply --dry-run=client -f deploy/k3s/rp4-uptime-kuma-rollback.yaml
```

Server-side dry-run invokes API validation without persisting resources:

```sh
ssh pi4@192.168.0.101 'sudo k3s kubectl apply --dry-run=server -f -' \
  < deploy/k3s/rp4-smoke-registry.yaml

ssh pi4@192.168.0.101 'sudo k3s kubectl apply --dry-run=server -f -' \
  < deploy/k3s/rp4-uptime-kuma-rollback.yaml
```

The RP5 `observability` namespace does not exist yet. A server dry-run does not
persist its dry-run Namespace long enough for later objects in the same stream,
so validate the Namespace exactly and validate the remaining objects against
the existing `default` namespace without changing the manifest:

```sh
head -n 4 deploy/k3s/rp5-uptime-kuma.yaml | \
  ssh pi5@192.168.0.94 'sudo k3s kubectl apply --dry-run=server -f -'

tail -n +6 deploy/k3s/rp5-uptime-kuma.yaml | \
  sed 's/namespace: observability/namespace: default/g' | \
  ssh pi5@192.168.0.94 'sudo k3s kubectl apply --dry-run=server -f -'
```

After an approved operation creates the namespace, the complete RP5 file can
be server-dry-run without the validation-only substitution.

Do not substitute `kubectl apply` without `--dry-run=server` during review.

## Kuma Migration Gate

Before a future approved migration window:

1. Verify the latest RP4 and off-host backups, including Kuma's database and
   WAL files.
2. Stop writes to the RP4 Kuma instance before the final data copy; do not copy
   a live SQLite database piecemeal.
3. Prepare `/var/lib/uptime-kuma` on RP5 with ownership and permissions tested
   against the pinned image. The manifest deliberately requires the directory
   to exist instead of silently creating an empty deployment.
4. Copy the consistent data set, start RP5 Kuma, and verify its database,
   monitor inventory, history, notification configuration, UI, and host-port
   reachability at `http://192.168.0.94:3001/`.
5. Keep the RP4 data untouched through the stability window. Use the rollback
   manifest only after ensuring the RP5 instance is stopped.

The security contexts intentionally retain the upstream registry and Kuma root
users for compatibility with their existing root-owned data, while disabling
privilege escalation, dropping Linux capabilities, disabling ServiceAccount
token mounts, and enabling the runtime-default seccomp profile. Smoke is
stateless and runs with a read-only root filesystem as UID/GID 65534.
