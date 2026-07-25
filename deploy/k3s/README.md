# Pi4 Platform Manifest

This directory retains only the shared `homelab` namespace definition. GRID's
workload manifest is owned by the sibling `GRIDBrain` repository and expects
this namespace to exist.

The former synthetic smoke workload, local registry, and Uptime Kuma manifests
were retired as part of the balanced-performance profile. The existing
`/mnt/ssd/registry` and `/mnt/ssd/podman/uptime-kuma-data` host directories are
deliberately not deleted by this repository or the rollout script.

## Validation

Client-side parsing does not contact the cluster:

```sh
kubectl apply --dry-run=client -f deploy/k3s/rp4-homelab-namespace.yaml
```

Server-side validation uses the live Pi4 API without persisting changes:

```sh
ssh pi4@192.168.0.101 'sudo k3s kubectl apply --dry-run=server -f -' \
  < deploy/k3s/rp4-homelab-namespace.yaml
```

Apply the namespace only during an approved maintenance window. Workload
retirement is an explicit `kubectl delete` operation; applying this manifest
does not delete anything.
