# gpubox

`gpubox` is a small, opinionated setup for running a **remote, privileged
Kubernetes Pod for AI/ML development**. It is designed for GPU nodes and a
“remote editor” workflow (VS Code CLI tunnel), while still giving you full
access to the node when that’s required (privileged container, optional host
mounts).

This repository contains two main pieces:

- `vscode/Containerfile`: builds an Ubuntu-based devbox image with common
  development/debug tooling and the VS Code CLI[^vscode-license].
- `charts/gpubox`: a Helm chart that deploys the image as a privileged
  `StatefulSet` with persistent storage and optional GPU scheduling constraints.

## What you get

- **Privileged devbox**: `containerSecurityContext.privileged=true` by default
  (intentionally).
- **Optional host PID namespace**: `pod.hostPID=false` by default; set
  `pod.hostPID=true` if you need to see host processes.
- **Persistent home**: a PVC mounted at `/home/gpubox` so your configuration,
  extensions, caches, and repos survive restarts.
- **Optional transfer volume**: a second PVC (often RWX) mounted at `/transfer`
  for moving datasets/models in/out.
- **VS Code tunnel + SSH workflow**: the default container entrypoint starts
  `sshd` (with persisted host keys under the home volume and a startup readiness
  check) and runs `code tunnel ...` so you can attach from local VS Code and
  still have standard SSH access on port `22`.
- **Rootless Podman ready**: Podman is preinstalled with `uidmap`,
  `fuse-overlayfs`, and `slirp4netns`, with system + per-user config for
  rootless runtime directories and Podman defaults.
- **Rootless Podman + CUDA ready**: when NVIDIA GPU Operator injects runtime
  artifacts, startup syncs NVIDIA CDI specs (and OCI hook fallback), adjusts
  rootless-compatible NVIDIA runtime cgroup behavior (when config is present),
  and grants `gpubox` access to GPU device groups. The image also includes
  `nvidia-container-toolkit`, providing the `nvidia-ctk` CLI for local CDI
  generation.
- **On-demand kernel package install**: run `instkheaders` inside the container
  to install kernel-version-matched `linux-headers`/`linux-tools` packages for
  the current `uname -r` only when needed.
- **GPU scheduling**: configure `resources.limits.nvidia.com/gpu` and node
  selection to land on GPU nodes.

## Security note (read this)

This is meant for trusted, operator-controlled clusters only. The defaults are
powerful and dangerous:

- **Privileged containers** can fully control the host.
- **hostPID** (if enabled) allows visibility into host processes.
- **hostPath** mounts (if enabled) can expose the host filesystem.

Use dedicated namespaces, tight RBAC, and (if applicable) Pod Security Admission
labels appropriate for privileged workloads.

## Build the image

From the repo root:

```bash
podman build -f ./vscode/Containerfile -t ghcr.io/donadiosolutions/gpubox:dev ./vscode
```

### SBOM + provenance notes

This project publishes chart/package SBOM assets with releases.

## Deploy to Kubernetes (Helm)

```bash
helm upgrade --install gpubox ./charts/gpubox \
  --namespace gpubox \
  --create-namespace
```

### Install from the Helm repo

```bash
helm repo add gpubox https://donadiosolutions.github.io/gpubox
helm repo update

helm upgrade --install gpubox gpubox/gpubox \
  --namespace gpubox \
  --create-namespace
```

To pin a specific chart version:

```bash
helm upgrade --install gpubox gpubox/gpubox \
  --version <chart-version> \
  --namespace gpubox \
  --create-namespace
```

List available versions with `helm search repo gpubox/gpubox --versions`.

Release shipping sets `image.tag` to the image tag that ships with that chart
version, and sets `image.digest` when the chart intentionally reuses an
existing immutable image. If you clear `image.tag`, the chart falls back to
`v<chart-version>` (for example, chart `1.0.0` would use
`ghcr.io/donadiosolutions/gpubox:v1.0.0`).

### Provide SSH authorized keys (recommended)

The image does **not** bake an `authorized_keys` file. The chart can inject keys
into the mounted home volume via an initContainer.

Create a values file (recommended), for example `values.ssh.yaml`:

```yaml
ssh:
  authorizedKeys:
    - ssh-ed25519 AAAA... you@laptop
```

Then:

```bash
helm upgrade --install gpubox ./charts/gpubox \
  --namespace gpubox \
  -f values.ssh.yaml
```

### Typical GPU pinning (example)

```yaml
resources:
  limits:
    nvidia.com/gpu: 1

nodeSelector:
  nvidia.com/gpu.present: "true"
```

### Rootless Podman CUDA (nested containers)

With NVIDIA GPU Operator/runtime injection enabled on the node, nested rootless
Podman containers can use GPUs via CDI:

```bash
podman run --rm --device nvidia.com/gpu=all \
  docker.io/nvidia/cuda:12.9.0-base-ubuntu24.04 nvidia-smi
```

## Contributing

Repository development and release-maintainer guidance lives in
`CONTRIBUTING.md`.

## License

The code in this repository is licensed under the MIT License. See `LICENSE`.

[^vscode-license]: Visual Studio Code / VS Code CLI are redistributed under
    their own license terms and are not covered by this repository’s MIT
    license. See [Microsoft’s Visual Studio Code license terms](https://code.visualstudio.com/license).
