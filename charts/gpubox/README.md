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

- Kubernetes 1.34 or newer for the default managed DNS sidecars.
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

### Automatic MagicDNS

`dns.enabled=true` is the default, even when Tailscale is disabled. A non-root
Python controller captures the Kubernetes resolver, materializes a static
application resolver file, and starts CoreDNS before Tailscale and applications.
The controller uses `/usr/bin/python3` from the selected gpubox image; custom
images must provide it. No extra DNS values, API credentials, Kubernetes API
permissions, or Tailnet restricted-DNS rule are required.

`tailscale.acceptDNS=true` enables discovery of the active MagicDNS suffix.
This changed in chart 2.9.0: the chart always sets `TS_ACCEPT_DNS=false`, and
local CoreDNS owns forwarding instead of letting Tailscale rewrite the Pod
resolver. The controller reads Tailscale status through its Unix socket as a
non-root user; Tailscale denies that user settings mutations. The controller
never receives the Tailscale state PVC or operator authority.

Names equal to or below the active suffix go to MagicDNS first. Everything
else, including cluster services, site-local names, other tailnets, and PTR
queries, goes to the original Kubernetes nameservers in their original order.
MagicDNS transport failures, SERVFAIL, and REFUSED fall back to those cluster
nameservers. Valid NXDOMAIN and NOERROR/NODATA answers are preserved. The
forwarder's health-check name also stays inside the discovered suffix.

The controller verifies the node's own A/AAAA record against its Tailscale
addresses before activating the route. IPv6-only Tailscale nodes use
`fd7a:115c:a1e0::53`; other nodes use `100.100.100.100`. Missing, disabled,
invalid, or unavailable MagicDNS selects cluster-only routing. It reconciles
every five seconds; the healthy-runtime configuration transition target is
12 seconds, including bounded probing and `reload 2s 1s`. A configuration
write is not proof of active routing; verify actual answers and CoreDNS logs.

Search domains and resolver options are preserved. The chart adds no MagicDNS
search suffix. Routing uses the absolute name on the DNS wire: an inherited
search suffix can still cause a client's short name to expand into a tailnet
name. CoreDNS cannot infer the original application input.

The application resolver file is created once per Pod and mounted read-only.
Only the directory-mounted Corefile changes dynamically. Controller restarts
reuse the original resolver snapshot and application file. CoreDNS listens on
loopback UDP/TCP 53, with Pod-reachable health/readiness on 8080/8181. Neither
DNS nor health ports are published by a Service. A CoreDNS process failure can
briefly interrupt DNS until Kubernetes restarts it; no secondary application
nameserver bypasses the managed routing.

The default controller and CoreDNS resource requests are each 10m CPU and
32Mi memory, with a 128Mi memory limit and no CPU limit. Override them using
`dns.controller.resources` and `dns.resources`. The controller image remains
the application image; `dns.image` controls the digest-pinned CoreDNS image.
The setup container initializes only the dedicated ephemeral DNS volume and
does not change existing PVC ownership through a Pod-wide `fsGroup`.

To retain the original application resolver instead:

```yaml
dns:
  enabled: false
tailscale:
  acceptDNS: false
```

Managed DNS rejects host networking, Kubernetes below 1.34, and conflicts with
its container/volume names, resolver mounts, or reserved ports. User init
containers and sidecars receive the managed resolver automatically; unrelated
fields are preserved. Enabling Tailscale DNS acceptance with managed DNS
disabled is rejected. Port conflicts not declared in the chart surface as
startup failures. Existing Tailscale authentication startup gates still apply;
MagicDNS availability does not determine controller liveness.

### Upgrade and rollback

The default StatefulSet update strategy is `OnDelete`. A successful Helm
upgrade changes the template but does not replace the running Pod.

1. Retain any existing restricted Tailnet DNS rule while the old Pod runs.
2. Upgrade the chart to 2.9.0.
3. Explicitly recreate the Pod during an appropriate interruption window.
4. Confirm `TS_ACCEPT_DNS=false` in the new Tailscale container, then verify
   cluster DNS, site-local DNS, a MagicDNS FQDN, failure fallback, and retained
   Tailscale node identity.
5. Only then remove the old restricted-DNS rule if no other workload needs it.

Before rollback and recreation of an old Pod, restore the Tailnet DNS rule
required by the old chart, or explicitly disable its Tailscale DNS takeover.
Do not remove a shared rule merely because this workload no longer needs it.

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
exists. There is intentionally no Tailscale liveness probe: transient Tailnet
health loss must not create a restart/re-registration loop.
