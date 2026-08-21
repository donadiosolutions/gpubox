# gpubox Helm chart

Deploys a privileged GPU devbox as a `StatefulSet`, running the container built from `vscode/Containerfile`.

## Install

```bash
helm upgrade --install gpubox ./charts/gpubox --namespace gpubox --create-namespace
```

If your cluster enforces Kubernetes Pod Security Admission (PSA), you likely need privileged labels on the namespace.
You can have the chart apply them:

```bash
helm upgrade --install gpubox ./charts/gpubox \
  --namespace gpubox \
  --set namespace.create=true
```

## Values highlights

- `containerSecurityContext.privileged=true` (default) is required for the `hostPath: /` mount.
- `pod.hostPID=false` by default; set `pod.hostPID=true` if you need host process visibility.
- `resources.limits.nvidia.com/gpu` controls GPU allocation.
- `sharedMemory.sizeLimit=64Gi` replaces the container runtime's default
  `/dev/shm` with a memory-backed volume sized for tensor-parallel workloads;
  set `sharedMemory.enabled=false` to keep the runtime default. Memory used by
  the volume counts against the pod and node memory budgets. An
  enabled persistence or `extraVolumeMounts` entry at `/dev/shm`, or an
  `extraVolumes` entry named `dshm`, takes precedence to preserve existing
  custom shared-memory configurations.
- release shipping sets `image.tag` to the image tag that ships with the
  chart, and sets `image.digest` when the chart intentionally reuses an
  existing immutable image; if `image.tag` is cleared, the chart falls back to
  `v<Chart.Version>`.
- `persistence.home`, `persistence.transfer`, and `persistence.tmp` configure PVC sizes and storage classes.
- `ssh.authorizedKeys` injects `authorized_keys` into the mounted home volume via an initContainer.
- `tailscale.enabled=true` adds a privileged, kernel-mode native sidecar with
  AuthKey-only authentication and PVC-backed node state.
- `tolerations`, `affinity`, `nodeSelector` allow pinning to GPU nodes.
- `extraResources` appends additional Kubernetes manifests to the release.

## Extra resources

`extraResources` accepts a list of resources where each item is either:

- A YAML object.
- A YAML string snippet.

Each item is rendered through `tpl`, so templates can reference release/chart values.

```yaml
extraResources:
  - apiVersion: v1
    kind: ConfigMap
    metadata:
      name: "{{ include \"gpubox.fullname\" . }}-extras"
      namespace: "{{ .Release.Namespace }}"
    data:
      mode: "enabled"
  - |
    apiVersion: v1
    kind: Secret
    metadata:
      name: {{ include "gpubox.fullname" . }}-credentials
    stringData:
      token: change-me
```

Validation and metadata behavior:

- Each `extraResources` item must render to exactly one YAML object.
- Required fields are validated at render time: `apiVersion`, `kind`, and `metadata.name`.
- If `metadata.namespace` is omitted, the chart injects `.Release.Namespace` for namespaced resources.
- Namespace injection is skipped for known cluster-scoped kinds (for example, `Namespace`, `ClusterRole`, `ClusterRoleBinding`, `CustomResourceDefinition`).
- Standard chart labels are added when missing; existing user-provided label values are preserved.

Security note:

- `extraResources` can create privileged or cluster-scoped objects (for example, RBAC and CRDs). Review and control supplied manifests carefully.

## SSH authorized keys

The container image does not bake an `authorized_keys` file. To provision keys, set them in values:

```yaml
ssh:
  authorizedKeys:
    - ssh-ed25519 AAAA... user@laptop
```

This writes to `<persistence.home.mountPath>/.ssh/authorized_keys` before the main container starts.

## Tailscale sidecar

Set `tailscale.enabled=true` to give the singleton gpubox Pod its own Tailnet
identity. Containers in the Pod share a network namespace, so Tailscale kernel
mode makes the gpubox SSH listener reachable from permitted Tailnet devices and
lets gpubox reach Tailnet devices and accepted subnet routes without SOCKS or
HTTP proxy configuration. The VS Code tunnel remains enabled and independent.

### Prerequisites

- Kubernetes 1.29 or newer with the `SidecarContainers` feature enabled.
- A namespace that permits privileged containers. Both the forwarding init
  container and Tailscale sidecar run privileged.
- `replicaCount: 1` and `pod.hostNetwork: false`; the chart rejects other
  combinations when Tailscale is enabled.
- A non-ephemeral Tailscale AuthKey. Prefer a tagged key, and make it
  pre-approved when Tailnet device approval is enabled. A one-off key is enough
  for the persisted singleton identity; replacing or losing the state PVC then
  requires a fresh key.
- Tailnet grants that permit the intended inbound and outbound traffic.

### AuthKey configuration

The recommended path is a pre-created Secret in the release namespace:

```bash
kubectl -n gpubox create secret generic gpubox-tailscale-auth \
  --from-literal=TS_AUTHKEY='tskey-auth-...'
```

```yaml
tailscale:
  enabled: true
  authKey:
    existingSecret: gpubox-tailscale-auth
    secretKey: TS_AUTHKEY
```

For controlled development environments, the chart can create the Secret:

```yaml
tailscale:
  enabled: true
  authKey:
    value: tskey-auth-...
```

**Warning:** an inline AuthKey is retained in Helm values and release history,
even though the StatefulSet receives only a Secret reference. Prefer an
existing Secret. Exactly one AuthKey source is required. OAuth, workload
identity, interactive login, and Kubernetes Secret-backed Tailscale state are
not supported by this integration.

### Persistent identity

The chart creates `<fullname>-tailscale-state` and mounts it only in the
Tailscale sidecar at `/var/lib/tailscale`. `TS_AUTH_ONCE=true` and
`TS_KUBE_SECRET=""` preserve the node identity across container and Pod
restarts without giving the Pod Secret-write RBAC or a ServiceAccount token.

Use an externally managed claim when the identity must have an independent
lifecycle:

```yaml
tailscale:
  enabled: true
  authKey:
    existingSecret: gpubox-tailscale-auth
  state:
    existingClaim: durable-tailscale-state
```

The claim must be writable and must not be shared with another tailscaled
process.

### Split MagicDNS

`tailscale.acceptDNS=true` lets the Pod use MagicDNS, but Tailscale then points
the Pod's resolver at `100.100.100.100`. Configure restricted DNS before
starting the sidecar so Kubernetes service names still reach CoreDNS.

Discover the resolver:

```bash
kubectl get svc -n kube-system kube-dns \
  -o jsonpath='{.spec.clusterIP}'
```

In the Tailscale DNS admin page, add:

```text
Domain:     svc.cluster.local
Nameserver: <CoreDNS ClusterIP>
```

For a custom Kubernetes cluster domain, use `svc.<cluster-domain>`. A single
restricted entry cannot represent multiple clusters that use the same cluster
domain with different CoreDNS Service IPs. The chart cannot edit or verify
Tailnet DNS with an AuthKey.

### Accepted-route safety

`tailscale.acceptRoutes=true` is the default. Tailscale installs advertised
routes without detecting overlaps with the Kubernetes network. Before enabling
route acceptance, compare Tailnet routes with the cluster Pod and Service
CIDRs:

```bash
tailscale status --json | jq \
  '.Peer[] | select(.PrimaryRoutes) | {name: .HostName, routes: .PrimaryRoutes}'
kubectl cluster-info dump | \
  grep -m 2 -E 'service-cluster-ip-range|cluster-cidr'
```

Remove overlapping Tailnet advertisements or set
`tailscale.acceptRoutes=false`; otherwise Pod-to-Pod traffic, ClusterIP
services, and DNS can be routed through the Tailscale tunnel and fail.

### Validation

After deployment:

```bash
kubectl -n gpubox exec gpubox-0 -c tailscale -- tailscale status
kubectl -n gpubox exec gpubox-0 -c gpubox -- \
  getent hosts kubernetes.default.svc
```

Also resolve a MagicDNS name and reach a Tailnet service from the gpubox
container, then connect to port 22 from a permitted Tailnet device. Recreating
the Pod should restore the same Tailscale identity while the state claim
exists. There is intentionally no liveness probe: transient Tailnet health
loss must not create a restart/re-registration loop.
