# Compact Image SBOM Generator Design

## Status

Approved in conversation on 2026-08-18. This document records the approved
architecture before implementation planning.

## Problem

The `v2.7.0` image build completed its container layers and generated an SPDX
SBOM, but BuildKit rejected the SBOM while exporting the image:

```text
/tmp/buildkit-mount819190719/sbom.spdx.json exceeds 41943040 bytes
```

BuildKit 0.32.2 limits each frontend-provided attestation document to 40 MiB.
The limit is a security boundary against unbounded attestation reads, so the
project must not remove it by downgrading or forking BuildKit.

The failure is caused by the size of the SPDX document, not by the image
layers, registry, GitHub artifact service, or provenance attestation. The
current gpubox root filesystem produces the following measured results:

| Generator | Bytes | Packages | Files | Relationships |
| --- | ---: | ---: | ---: | ---: |
| BuildKit Syft 1.11.0 | 51,291,569 | 4,444 | 68,580 | 87,617 |
| BuildKit Syft 1.12.0 | 51,269,178 | 4,445 | 68,582 | 87,645 |
| Docker Scout 1.24.0 | 49,285,128 | 3,804 | 68,547 | 118,991 |

Changing from Syft to Scout is therefore insufficient: both supported
off-the-shelf generators exceed BuildKit's limit on the actual image.

## Goals

- Preserve an attached SPDX SBOM and `mode=max` provenance on every published
  gpubox image rebuild.
- Preserve the full Syft package inventory, including package identifiers,
  versions, PURLs, licenses, metadata, and package-to-package relationships.
- Produce an individual in-toto SPDX statement smaller than 32 MiB, leaving at
  least 8 MiB of headroom below BuildKit's 40 MiB limit.
- Pin every workflow action and container image dependency to an immutable
  commit or image digest.
- Fail closed if scanning, parsing, compaction, validation, or publication does
  not complete correctly.
- Ensure changes to the generator trigger a real gpubox image rebuild.
- Verify the exact trusted branch build and push path before merge, then verify
  the post-merge `main` build.

## Non-goals

- Do not increase, bypass, or remove BuildKit's attestation size limit.
- Do not downgrade BuildKit.
- Do not replace the attached image SBOM with only a workflow artifact or
  GitHub Release asset.
- Do not change the gpubox runtime filesystem solely to make the scanner output
  smaller.
- Do not change chart or application versions and do not create a release tag
  as part of this issue.
- Do not split or redesign the existing image dependency sets.

## Chosen Architecture

Add a repository-owned BuildKit SBOM generator in `sbom-generator/`. The
generator is a narrow adapter around Docker's official BuildKit Syft scanner:

1. A small Go executable invokes the pinned upstream `/bin/syft-scanner` with
   the BuildKit scanner-protocol environment unchanged.
2. After the upstream scanner exits successfully, the adapter finds every
   `*.spdx.json` file in `BUILDKIT_SCAN_DESTINATION`.
3. It parses each in-toto statement and validates that its predicate type is an
   SPDX document.
4. It removes file-level SPDX objects and references while preserving package
   and non-file relationship data.
5. It writes the compact statement atomically and validates the final byte
   count against a 32 MiB internal maximum.

The current 51.29 MB Syft statement becomes approximately 10.03 MB after this
transformation while preserving all 4,444 discovered packages and 14,128
non-file relationships.

## Compaction Contract

For every generated in-toto SPDX statement, the adapter must:

- require the top-level JSON value to be exactly one object;
- require `predicateType` to be `https://spdx.dev/Document`;
- require `predicate` to be a JSON object;
- collect every non-empty `SPDXID` from `predicate.files`;
- remove `predicate.files`;
- remove collected file SPDX IDs from each package's `hasFiles` array and
  remove the array when no entries remain;
- remove only relationships whose `spdxElementId` or
  `relatedSpdxElement` matches a removed file SPDX ID;
- preserve all other top-level statement fields, predicate fields, packages,
  and relationships without converting JSON numbers through floating point;
- serialize compact JSON with one trailing newline;
- reject output larger than `32 << 20` bytes;
- replace the original only after the compact output has been fully written,
  synchronized, closed, and validated.

If `predicate.files` is absent or empty, the generator preserves package
`hasFiles` arrays because it has no collected file IDs to remove. It must never
silently discard a package file reference or relationship when it cannot prove
that the referenced endpoint is a removed file.

## Failure Behavior

The adapter exits nonzero and leaves no partially replaced output when any of
these conditions occurs:

- `BUILDKIT_SCAN_DESTINATION` is unset, empty, missing, or not a directory;
- the upstream scanner cannot start or exits nonzero;
- no `*.spdx.json` output is produced;
- an output cannot be read as one JSON object;
- the in-toto predicate type is not SPDX;
- `predicate`, `files`, `packages`, or `relationships` has an incompatible
  JSON type when present;
- a file SPDX ID is empty or duplicated with conflicting content;
- temporary output cannot be written, synchronized, closed, or renamed;
- compacted output exceeds 32 MiB.

Errors must name the affected phase and file without dumping the SBOM or
environment, because either may contain sensitive path or package metadata.

## Generator Image

The generator image is built from a dedicated
`sbom-generator/Containerfile`:

- the upstream `docker/buildkit-syft-scanner` base is referenced by immutable
  manifest digest;
- the Go adapter is compiled before the image build using the Go toolchain
  already supplied by the GitHub runner;
- the final image is `scratch` and contains only the upstream static scanner
  binary and the static adapter binary;
- the adapter is the image entrypoint;
- no shell, package manager, network client, or writable application data is
  added to the final image.

The build context must exclude generated test artifacts and include only the
container definition plus the compiled adapter binary. The binary is a
workflow-generated artifact and must remain ignored by Git.

## Workflow Integration

The `container-image` job remains the owner of both generator and gpubox image
publication.

When an image rebuild is required, it will:

1. compile and test the Go adapter;
2. build the generator image;
3. on trusted non-pull-request events, push the generator to GHCR with its own
   SBOM and `mode=max` provenance;
4. capture the generator manifest digest directly from the build action;
5. build gpubox with the generator's captured immutable manifest digest rather
   than a tag;
6. retain `mode=max` provenance for gpubox;
7. publish the existing gpubox image tag and digest outputs only after the
   attested image push succeeds.

The generator's own SBOM uses the official Syft generator pinned by digest.
Because the generator image contains only two static binaries, its SBOM stays
well below the 40 MiB boundary and does not recurse through the custom
generator.

Pull-request events build the generator and gpubox without pushing. The Go
tests provide deterministic compaction coverage. Before merge, a trusted
manual workflow dispatch against the feature branch exercises the complete
generator push and digest-addressed gpubox attestation path.

## Change Detection

The rebuild detector must consider both of these image-affecting contexts:

- effective, non-ignored changes under `vscode/`;
- effective changes under `sbom-generator/`.

Generator changes affect the published image's attestation even though they do
not change its runtime layers, so they must force a rebuild rather than reuse a
previous image digest. Existing behavior for ignored `vscode/` files remains
unchanged.

The job summary must distinguish runtime-context changes from generator
changes and state the final rebuild decision.

## Dependency Integrity

- Continue pinning GitHub Actions to full commit SHAs.
- Pin the upstream BuildKit Syft scanner to its exact multi-platform manifest
  digest in both the generator `Containerfile` and the generator-image SBOM
  input.
- Use the runner-provided Go compiler and standard library only; do not add Go
  module dependencies.
- Record the required Socket score attempts. The Socket service returned HTTP
  500 for exact Docker image references during design validation, so the PR
  must report that unavailable result and the manual review of the existing
  official Docker-published dependency. It must not claim a successful Socket
  score.

## Testing Strategy

Testing follows red-green-refactor.

### Unit fixtures

Tests must first fail against the absent adapter, then cover:

- a representative oversized statement compacts below the configured limit;
- packages, PURLs, licenses, and package relationships remain byte-equivalent
  as JSON values;
- file objects, package `hasFiles`, and file relationships are removed;
- non-file relationships that happen to contain the word `File` are retained;
- absent optional arrays are accepted;
- malformed statement and predicate shapes fail closed;
- a non-SPDX predicate type fails closed;
- scanner failure is propagated;
- zero scanner outputs fail closed;
- multiple scanner outputs are all compacted;
- an output above the internal limit fails closed;
- JSON integers retain their exact lexical representation.

### Protocol test

Run the built generator with BuildKit scanner-protocol environment variables
against a representative extracted root filesystem. Validate that it emits a
parseable in-toto SPDX statement, contains packages, contains no file records
or dangling file relationships, and remains below 32 MiB.

### Repository checks

- `go test ./...` in `sbom-generator/`;
- build the static adapter with `CGO_ENABLED=0`;
- build the generator container image;
- existing release fixture tests;
- existing pre-commit hooks;
- `helm lint charts/gpubox` and default `helm template` as unchanged regression
  checks;
- workflow YAML linting through the repository's existing CI gates.

### Trusted integration proof

Dispatch the feature branch's `build` workflow manually. The run must:

- build and push the generator image;
- report a valid immutable generator digest;
- rebuild and push the gpubox image;
- complete SBOM and provenance export without the 40 MiB error;
- expose a non-empty gpubox image digest;
- allow the attached SPDX statement to be inspected and shown below 32 MiB;
- retain the expected package inventory.

After required PR checks and exact-head review pass, merge and monitor the
entire `container-image` job on `main` through completion.

## Rollout and Rollback

This change affects build metadata, not runtime filesystem contents or chart
behavior. Rollout occurs when the trusted workflow publishes a new generator
digest and then a gpubox image attested by that digest.

Rollback is a normal signed revert of the workflow and generator changes. A
rollback must not republish an image without an SBOM merely to restore green
CI. If upstream later provides a package-complete generator that stays safely
below the BuildKit limit, the custom adapter can be removed in a separate
change after equivalent measurements and attached-attestation verification.

## Acceptance Criteria

- Unit and protocol tests prove the compaction contract and fail-closed cases.
- The generator image is built from immutable dependencies and referenced by
  its build output digest.
- The exact current gpubox image rebuild completes with attached SPDX SBOM and
  `mode=max` provenance on both GHCR and Docker Hub publication paths.
- The attached SPDX statement is no larger than 32 MiB and contains the full
  expected package inventory.
- Generator changes trigger an image rebuild.
- Required PR checks pass, exact-head review has no P0-P2 findings, and the PR
  merges without an administrative bypass.
- The post-merge `main` workflow succeeds and is actively monitored through
  the complete `container-image` step.
- No chart version, application version, release tag, or GitHub Release is
  created by this change.
