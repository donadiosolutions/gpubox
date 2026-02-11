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
- `scripts/release/`: release helper scripts (`render_release_body.py`, `verify_draft.sh`, `publish.sh`).

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

## CI runtime expectations

- As of 2026-02-11, successful `push` runs show `container-image` durations around:
  - median: ~140s (~2m20s)
  - p95: ~442s (~7m22s)
  - max observed: ~462s (~7m42s)
- Expected duration for `container-image`: roughly 2 to 8 minutes.
- Healthy margin before escalation: up to 12 minutes total.
- Agents must keep active monitoring for the entire `container-image` step
  (`gh run watch` and/or periodic `gh run view` polling).
- Do not assume a hang before the healthy margin unless logs and step status stop
  progressing.
- If the step exceeds 12 minutes with no progression, escalate with focused log
  inspection and rerun/cancel strategy.

## Releases

- `build.yml` runs on `main` pushes and `v*` tags, and is responsible for building/pushing:
  - the container image to GHCR
  - the Helm chart package + SBOM, and publishing the Helm repo to `gh-pages`
- For `v*` tag pushes, `build.yml` calls `release.yml` after successful build jobs.
- `release.yml` creates/updates a **draft** GitHub Release for that tag.
  - It downloads the `helm-chart` artifact from the same workflow run.
  - It renders a deterministic release body (`Highlights`, `Install`, `Full changelog`) from repo scripts.
  - It uploads `dist/*.tgz` and `dist/*.spdx.json` to the GitHub Release as assets.
  - It verifies draft state, body headings, and required assets after update.

### Cut a release

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

### Publish a prepared draft release

```bash
scripts/release/publish.sh vX.Y.Z
```

Notes:
- Tag CI intentionally prepares draft releases first; publishing is a separate explicit action.
- `scripts/release/publish.sh` requires `gh` and `jq` on `PATH`.
- GitHub may temporarily show an `untagged-*` URL while updates propagate. Treat
  `/releases/tag/vX.Y.Z` as the canonical URL after publish.

### Fix a failed release

Re-run the `build` workflow for that tag in the GitHub Actions UI (or re-run only the
`release` job from the same run), then re-run:

```bash
scripts/release/publish.sh vX.Y.Z
```

This is safe and idempotent because the release job updates existing draft releases and
the publish script verifies readiness before publishing.

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
