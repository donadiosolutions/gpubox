# Contributing

This document covers repository development, release operations, and local
quality checks.

## Build metadata and provenance

The image build adds OCI metadata labels in `vscode/Containerfile` (source,
revision, created, etc.). Supply these at build time in CI to improve SBOM and
provenance usefulness.

- **Docker buildx** can emit SBOM + provenance attestations during build.
- **Podman** can generate an SBOM during build (`podman build --sbom ...`), but
  provenance attestations are typically attached afterwards with a signing tool
  (for example, `cosign`).

## Release lifecycle

Tag pushes (`vX.Y.Z`) run release automation that prepares a **draft** GitHub
release with:

- `gpubox-<version>.tgz`
- `gpubox-chart.sbom.spdx.json`
- container image metadata:
  - if `vscode/Containerfile` changed since the previous release tag, CI rebuilds and pushes an image for the current tag
  - otherwise, CI reuses the latest published image tag + digest in release install instructions
- chart image defaults:
  - before merging a release bump PR, update `charts/gpubox/values.yaml` `image.tag` and `image.digest` to the image ref that will actually ship
  - if no effective `vscode/` context changes are present, set those fields to the reused published image tag + digest instead of the new chart version tag
- release notes with:
  - `## Highlights`: optional AI-generated bullets (via pinned
    `openai/codex-action`) when `OPENAI_API_KEY` is configured; deterministic
    fallback otherwise
  - `## Install`: deterministic
  - `## Full changelog`: deterministic and full-fidelity

After draft validation, publish explicitly:

```bash
scripts/release/publish.sh vX.Y.Z
```

This command verifies draft state, required headings, and required assets
before publishing as latest.

Preview AI highlights locally for recent releases (uses your local `gh` auth
and `OPENAI_API_KEY`):

```bash
scripts/release/preview_codex_highlights.sh 5
```

Notes:

- `scripts/release/publish.sh` expects `gh` and `jq` to be available on your
  `PATH`.
- Draft URLs may briefly appear as `untagged-*` while GitHub updates metadata.
- The canonical published URL format is `/releases/tag/vX.Y.Z`.

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

- If `pre-commit` fails with `gitleaks: command not found`, install
  `gitleaks` and retry.
- Emergency bypass (discouraged): `SKIP=gitleaks-system git commit ...` or
  `SKIP=gitleaks-system pre-commit run --all-files`.
