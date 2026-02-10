# Repo guide for agents

This repository contains a small “remote devbox” stack:

- A container image (Ubuntu + common tooling + VS Code CLI tunnel)
- A Helm chart that deploys the image as a privileged `StatefulSet`

## Key paths

- `vscode/Containerfile`: container image build (VS Code CLI download + OCI labels).
- `vscode/entrypoint.sh`: container entrypoint (starts the VS Code tunnel workflow).
- `charts/gpubox/`: Helm chart sources (`Chart.yaml`, `values.yaml`, `values.schema.json`).
- `.github/workflows/build.yml`: CI that builds/pushes the image and packages the chart.
- `.github/workflows/release.yml`: reusable CI workflow that creates/updates GitHub Releases.

## Common commands

- Build image locally (Podman):
  - `podman build -f ./vscode/Containerfile -t ghcr.io/donadiosolutions/gpubox:dev ./vscode`
- Lint chart:
  - `helm lint charts/gpubox`
- Render chart (sanity check templates):
  - `helm template gpubox charts/gpubox --namespace gpubox`
- Package chart (matches CI behavior):
  - `mkdir -p dist && helm package charts/gpubox --destination dist`

## CI + release expectations

- GitHub Actions `uses:` entries are pinned to immutable SHAs; keep that pattern.
- The chart packaging job uploads a `helm-chart` workflow artifact containing:
  - `dist/gpubox-*.tgz`
  - `dist/*.spdx.json` (chart SBOM)
- The chart packaging job also publishes a Helm chart repository to the `gh-pages`
  branch (GitHub Pages), including:
  - `index.yaml`
  - `gpubox-*.tgz`

## Releases

- `build.yml` runs on `main` pushes and `v*` tags, and is responsible for building/pushing:
  - the container image to GHCR
  - the Helm chart package + SBOM, and publishing the Helm repo to `gh-pages`
- For `v*` tag pushes, `build.yml` calls `release.yml` after successful build jobs.
- `release.yml` creates/updates a GitHub Release for that tag.
  - It downloads the `helm-chart` artifact from the same workflow run.
  - It creates a release body with a stable install template plus GitHub-generated notes.
  - It uploads `dist/*.tgz` and `dist/*.spdx.json` to the GitHub Release as assets.

### Cut a release

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

### Fix a failed release

Re-run the `build` workflow for that tag in the GitHub Actions UI (or re-run only the
`release` job from the same run).
This is safe and idempotent because the release job updates existing releases.

## Version bump checklist

- Chart release version:
  - Keep `charts/gpubox/Chart.yaml:version` aligned with release tag and image tag (`vX.Y.Z`).
- VS Code CLI:
  - Update `VSCODE_CLI_VERSION` and `VSCODE_CLI_SHA256` in `vscode/Containerfile`.
  - Keep `charts/gpubox/Chart.yaml:appVersion` aligned with the container runtime stack versioning scheme.
- Ubuntu base image:
  - If you change `UBUNTU_VERSION`, also update `UBUNTU_DIGEST` to keep builds reproducible.

## Security posture

The chart defaults are intentionally powerful (privileged + host PID, optional host mounts).
When changing defaults, update the docs and be explicit about security impact.
