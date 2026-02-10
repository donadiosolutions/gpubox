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
- `persistence.home` and `persistence.transfer` configure PVC sizes and storage classes.
- `ssh.authorizedKeys` injects `authorized_keys` into the mounted home volume via an initContainer.
- `tolerations`, `affinity`, `nodeSelector` allow pinning to GPU nodes.

## SSH authorized keys

The container image does not bake an `authorized_keys` file. To provision keys, set them in values:

```yaml
ssh:
  authorizedKeys:
    - ssh-ed25519 AAAA... user@laptop
```

This writes to `<persistence.home.mountPath>/.ssh/authorized_keys` before the main container starts.
