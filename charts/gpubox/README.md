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
- `image.digest` can pin an immutable container digest (`sha256:...`) while still setting `image.tag`.
- `persistence.home`, `persistence.transfer`, and `persistence.tmp` configure PVC sizes and storage classes.
- `ssh.authorizedKeys` injects `authorized_keys` into the mounted home volume via an initContainer.
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
