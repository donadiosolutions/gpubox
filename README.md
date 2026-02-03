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

- **Privileged devbox**: `securityContext.privileged=true` and `hostPID=true` by
  default (intentionally).
- **Persistent home**: a PVC mounted at `/home/gpubox` so your configuration,
  extensions, caches, and repos survive restarts.
- **Optional transfer volume**: a second PVC (often RWX) mounted at `/transfer`
  for moving datasets/models in/out.
- **VS Code tunnel workflow**: the default container entrypoint runs `code
  tunnel ...` so you can attach from your local VS Code without exposing inbound
  ports.
- **GPU scheduling**: configure `resources.limits.nvidia.com/gpu` and node
  selection to land on GPU nodes.

## Security note (read this)

This is meant for trusted, operator-controlled clusters only. The defaults are
powerful and dangerous:

- **Privileged containers** can fully control the host.
- **hostPID** allows visibility into host processes.
- **hostPath** mounts (if enabled) can expose the host filesystem.

Use dedicated namespaces, tight RBAC, and (if applicable) Pod Security Admission
labels appropriate for privileged workloads.

## Build the image

From the repo root:

```bash
podman build -f ./vscode/Containerfile -t ghcr.io/donadiosolutions/gpubox:dev ./vscode
```

### SBOM + provenance notes

This project adds OCI metadata labels in `vscode/Containerfile` (source,
revision, created, etc.). Supply them at build time in CI to make your
SBOM/provenance more useful.

- **Docker buildx** can emit SBOM + provenance attestations during build.
- **Podman** can generate an SBOM during build (`podman build --sbom ...`), but
  provenance attestations are generally attached after the fact using a signing
  tool (for example, `cosign`).

## Deploy to Kubernetes (Helm)

```bash
helm upgrade --install gpubox ./charts/gpubox \
  --namespace gpubox \
  --create-namespace
```

### Install from the Helm repo (GitHub Pages / `gh-pages`)

CI publishes a Helm chart repository to GitHub Pages (served from the `gh-pages`
branch). Once GitHub Pages is enabled for the repo, install with:

```bash
helm repo add gpubox https://donadiosolutions.github.io/gpubox
helm repo update

helm upgrade --install gpubox gpubox/gpubox \
  --namespace gpubox \
  --create-namespace
```

If you’re installing from a fork, replace `donadiosolutions` with your GitHub
org/user. If GitHub Pages isn’t enabled yet, go to **Settings → Pages** and set
the source to the `gh-pages` branch (root).

To pin a specific chart version:

```bash
helm upgrade --install gpubox gpubox/gpubox \
  --version <chart-version> \
  --namespace gpubox \
  --create-namespace
```

List available versions with `helm search repo gpubox/gpubox --versions`.

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

## Development (pre-commit)

This repo uses `pre-commit` locally and in CI, including `gitleaks` secret
scanning, plus basic YAML + Markdown linting.

### Install pre-commit

Recommended (via `pipx`):

```bash
pipx install pre-commit
```

Alternative (user install):

```bash
python3 -m pip install --user pre-commit
# Ensure ~/.local/bin is on your PATH.
```

### Install gitleaks

This repo uses the `gitleaks-system` pre-commit hook, which expects `gitleaks`
to be available on your `PATH`.

- macOS (Homebrew):

```bash
brew install gitleaks
```

- Linux (official release binary, example for x86_64):

```bash
GITLEAKS_VERSION=8.30.0
curl -fsSL -o gitleaks.tar.gz \
  "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
tar -xzf gitleaks.tar.gz gitleaks
sudo install -m 0755 gitleaks /usr/local/bin/gitleaks
rm -f gitleaks.tar.gz gitleaks
```

- Windows:
  - Download the `gitleaks_<version>_windows_x64.zip` asset from the GitHub
    Releases page.
  - Put `gitleaks.exe` somewhere on your `PATH`.

### Enable the hook

```bash
pre-commit install
```

### Run on demand

```bash
pre-commit run --all-files
```

### Troubleshooting

- If `pre-commit` fails with `gitleaks: command not found`, install `gitleaks`
  and retry.
- Emergency bypass (discouraged): `SKIP=gitleaks-system git commit ...` or
  `SKIP=gitleaks-system pre-commit run --all-files`.

## License

The code in this repository is licensed under the MIT License. See `LICENSE`.

[^vscode-license]: Visual Studio Code / VS Code CLI are redistributed under
    their own license terms and are not covered by this repository’s MIT
    license. See [Microsoft’s Visual Studio Code license terms](https://code.visualstudio.com/license).
