# Compact Image SBOM Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish every rebuilt gpubox image with a package-complete SPDX SBOM that stays below BuildKit's attestation limit, while retaining `mode=max` provenance and immutable generator dependencies.

**Architecture:** A repository-owned BuildKit generator runs Docker's digest-pinned Syft scanner, then atomically compacts each in-toto SPDX statement by removing file objects and only their proven references. The workflow builds and pushes this generator first, captures its manifest digest, and uses that digest to attest the gpubox image.

**Tech Stack:** Go standard library, Docker/BuildKit scanner protocol, SPDX 2.x JSON wrapped in in-toto statements, GitHub Actions, Blacksmith's digest-pinned build actions, GHCR, Docker Hub.

**Spec:** `docs/superpowers/specs/2026-08-18-compact-image-sbom-generator-design.md`

## Global Constraints

- Preserve attached SPDX SBOM and `mode=max` provenance on published gpubox images.
- Compact output must be no larger than `32 << 20` bytes; BuildKit's external limit remains `40 << 20` bytes.
- Preserve all package JSON values and all relationships not proven to reference a removed file SPDX ID.
- Preserve JSON number lexemes by using `json.RawMessage`, not `map[string]any`.
- Fail closed on scanner, environment, discovery, JSON shape, write, synchronization, rename, or size errors.
- Use the Go standard library only; `go.mod` must contain no third-party requirements and no `go.sum` should be generated.
- Pin the upstream scanner to `docker/buildkit-syft-scanner@sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68`.
- Keep all GitHub Actions pinned to full commit SHAs.
- All commits require `--signoff` and must retain the repository's default GPG signing.
- Do not bump chart/application versions and do not create a release tag or GitHub Release.
- Do not merge with failing required checks or use an administrative bypass.

---

### Task 1: SPDX Compaction Library

**Files:**

- Create: `sbom-generator/go.mod`
- Create: `sbom-generator/internal/compact/compact.go`
- Create: `sbom-generator/internal/compact/compact_test.go`

**Interfaces:**

- Produces: `const DefaultMaxBytes int64 = 32 << 20`
- Produces: `func File(path string, maxBytes int64) error`
- Produces: `func Statement(input []byte, maxBytes int64) ([]byte, error)`
- Consumes: an in-toto JSON statement whose `predicateType` is `https://spdx.dev/Document`
- Guarantees: exact preservation of untouched JSON values, removal only of proven file references, compact JSON plus one newline, and no partial replacement on failure

- [ ] **Step 1: Create the standard-library-only Go module**

Create `sbom-generator/go.mod`:

```go
module github.com/donadiosolutions/gpubox/sbom-generator

go 1.26
```

Do not run `go get` or add any `require` directive.

- [ ] **Step 2: Write failing statement-compaction tests**

Create `sbom-generator/internal/compact/compact_test.go` with table-driven
tests that construct JSON using raw literals and assert:

```go
func TestStatementRemovesOnlyProvenFileData(t *testing.T)
func TestStatementPreservesUnknownHasFilesAndRelationships(t *testing.T)
func TestStatementPreservesJSONNumberLexemes(t *testing.T)
func TestStatementAcceptsAbsentOptionalArrays(t *testing.T)
func TestStatementRejectsInvalidShapes(t *testing.T)
func TestStatementRejectsNonSPDXPredicate(t *testing.T)
func TestStatementRejectsDuplicateFileIDsWithConflictingContent(t *testing.T)
func TestStatementEnforcesMaximumSize(t *testing.T)
func TestFileAtomicallyReplacesValidStatement(t *testing.T)
func TestFileLeavesOriginalOnInvalidStatement(t *testing.T)
func TestFilePreservesMode(t *testing.T)
```

The primary fixture must contain:

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "predicateType": "https://spdx.dev/Document",
  "subject": [{"name":"example","digest":{"sha256":"abc"}}],
  "predicate": {
    "SPDXID": "SPDXRef-DOCUMENT",
    "documentNamespace": "https://example.invalid/123",
    "files": [
      {"SPDXID":"SPDXRef-File-A","fileName":"/bin/a"},
      {"SPDXID":"SPDXRef-File-B","fileName":"/bin/b"}
    ],
    "packages": [
      {
        "SPDXID":"SPDXRef-Package-A",
        "name":"package-a",
        "versionInfo":"1.0.0",
        "packageFileName":"archive-9007199254740993.tar",
        "hasFiles":["SPDXRef-File-A","SPDXRef-Unknown"]
      }
    ],
    "relationships": [
      {"spdxElementId":"SPDXRef-DOCUMENT","relationshipType":"DESCRIBES","relatedSpdxElement":"SPDXRef-Package-A"},
      {"spdxElementId":"SPDXRef-Package-A","relationshipType":"CONTAINS","relatedSpdxElement":"SPDXRef-File-A"},
      {"spdxElementId":"SPDXRef-Package-A","relationshipType":"DEPENDS_ON","relatedSpdxElement":"SPDXRef-Package-B"},
      {"spdxElementId":"SPDXRef-Package-A","relationshipType":"OTHER","relatedSpdxElement":"SPDXRef-FileNamedButUnknown"}
    ],
    "exactInteger": 9007199254740993
  }
}
```

Assertions must prove:

- `files` is absent;
- `SPDXRef-File-A` is removed from `hasFiles`, while `SPDXRef-Unknown` remains;
- only the relationship to `SPDXRef-File-A` is removed;
- package, package relationship, and unknown endpoint JSON remain;
- `9007199254740993` remains lexically exact in output;
- output ends with exactly one newline;
- output larger than a supplied small limit returns an error containing `exceeds`.

The file tests must use `t.TempDir()`, compare the original bytes after the
invalid-input call, and assert that the valid replacement retains the original
mode. The named production defects are direct truncation, rename before
validation, and omitted mode preservation.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
cd sbom-generator
go test ./internal/compact -v
```

Expected: compilation fails because `Statement` and `File` do not exist. This
is the required RED observation; do not create production code before
recording it.

- [ ] **Step 4: Implement minimal raw-JSON compaction**

Create `sbom-generator/internal/compact/compact.go` with:

```go
package compact

const DefaultMaxBytes int64 = 32 << 20

func Statement(input []byte, maxBytes int64) ([]byte, error)
func File(path string, maxBytes int64) error
```

`Statement` must decode the top level and predicate into
`map[string]json.RawMessage`. Define narrow helpers:

```go
func decodeObject(raw []byte, label string) (map[string]json.RawMessage, error)
func decodeObjectArray(raw json.RawMessage, label string) ([]map[string]json.RawMessage, error)
func stringField(obj map[string]json.RawMessage, field, label string) (string, error)
func collectFileIDs(files []map[string]json.RawMessage) (map[string]json.RawMessage, error)
func filterHasFiles(packages []map[string]json.RawMessage, fileIDs map[string]json.RawMessage) error
func filterRelationships(relationships []map[string]json.RawMessage, fileIDs map[string]json.RawMessage) ([]map[string]json.RawMessage, error)
```

Use `json.Decoder` followed by a second decode expecting `io.EOF` so trailing JSON is rejected. For duplicate file IDs, compare their compacted `json.RawMessage` values with `bytes.Equal`; reject differing values. Preserve missing optional arrays. Reject non-array values when an optional array key is present.

Marshal the statement with `json.Marshal`, append `\n`, then enforce
`int64(len(output)) <= maxBytes`. Reject nonpositive `maxBytes`.

`File` must:

1. read the original;
2. call `Statement`;
3. create a temporary file in the same directory with `os.CreateTemp`;
4. apply the original file mode;
5. write, `Sync`, and `Close` the temporary file;
6. rename it over the original;
7. close and remove the temporary file on every pre-rename error.

Do not log or include document contents in errors.

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
cd sbom-generator
go test ./internal/compact -v
go test ./...
```

Expected: all statement and file tests pass, no `go.sum` exists, and
`go list -m all` prints only the module itself.

- [ ] **Step 6: Commit the compaction library**

```bash
git add sbom-generator/go.mod \
  sbom-generator/internal/compact/compact.go \
  sbom-generator/internal/compact/compact_test.go
git commit --signoff -m "feat(sbom): compact file-level SPDX data"
```

---

### Task 2: BuildKit Scanner Protocol Runner

**Files:**

- Create: `sbom-generator/internal/runner/runner.go`
- Create: `sbom-generator/internal/runner/runner_test.go`
- Create: `sbom-generator/cmd/gpubox-sbom-generator/main.go`

**Interfaces:**

- Consumes: `compact.File(path string, maxBytes int64) error` from Task 1
- Produces: `type Config struct { ScannerPath string; Destination string; MaxBytes int64 }`
- Produces: `func Run(ctx context.Context, cfg Config) error`
- Produces: executable `/bin/gpubox-sbom-generator` behavior: run scanner, discover all top-level `*.spdx.json`, compact every output, emit errors only to stderr, and exit nonzero on any failure

- [ ] **Step 1: Write failing runner tests**

Create `runner_test.go` using temporary executable shell fixtures on Unix. Tests:

```go
func TestRunPropagatesScannerFailure(t *testing.T)
func TestRunRejectsMissingDestination(t *testing.T)
func TestRunRejectsNonDirectoryDestination(t *testing.T)
func TestRunRejectsZeroOutputs(t *testing.T)
func TestRunCompactsEverySPDXOutput(t *testing.T)
func TestRunDoesNotModifyOutputWhenAnyDocumentIsInvalid(t *testing.T)
```

The scanner fixture receives `BUILDKIT_SCAN_DESTINATION` from the test process and writes one or more known statements. For multi-file atomicity, `Run` must validate and compact every document in memory before replacing any file; the invalid second document must leave the valid first document unchanged.

- [ ] **Step 2: Run the runner tests and verify RED**

Run:

```bash
cd sbom-generator
go test ./internal/runner -v
```

Expected: compilation fails because `Config` and `Run` do not exist.

- [ ] **Step 3: Implement the fail-closed runner**

Create `runner.go`. Validate `ScannerPath`, `Destination`, and `MaxBytes`; confirm the destination with `os.Stat`; run:

```go
cmd := exec.CommandContext(ctx, cfg.ScannerPath)
cmd.Stdout = os.Stdout
cmd.Stderr = os.Stderr
cmd.Env = os.Environ()
```

After success, use `os.ReadDir(cfg.Destination)`, select only non-directory
entries whose names end in `.spdx.json`, and sort paths lexically. Reject zero
matches.

To guarantee multi-document atomicity, add this Task 1 interface before using
it:

```go
type Prepared struct {
    Path string
    Mode fs.FileMode
    Data []byte
}

func PrepareFile(path string, maxBytes int64) (Prepared, error)
func CommitFiles(prepared []Prepared) error
```

Refactor `compact.File` to call `PrepareFile` and `CommitFiles` for a
single-element slice. `Run` prepares every output first and calls
`CommitFiles` only if all preparations pass. `CommitFiles` writes and closes
all temporary files before performing any rename; if a rename fails, return a
clear error and remove every unrenamed temporary file. Document that filesystem
failure during a sequence of renames cannot be transactionally rolled back,
but no semantic validation failure may cause a partial replacement.

- [ ] **Step 4: Add the executable entrypoint**

Create `cmd/gpubox-sbom-generator/main.go`:

```go
package main

func main() {
    destination := os.Getenv("BUILDKIT_SCAN_DESTINATION")
    err := runner.Run(context.Background(), runner.Config{
        ScannerPath: "/bin/syft-scanner",
        Destination: destination,
        MaxBytes:    compact.DefaultMaxBytes,
    })
    if err != nil {
        fmt.Fprintf(os.Stderr, "gpubox SBOM generator: %v\n", err)
        os.Exit(1)
    }
}
```

Do not print environment values or document contents.

- [ ] **Step 5: Run tests and static build GREEN**

Run:

```bash
cd sbom-generator
go test ./...
CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' \
  -o /tmp/gpubox-sbom-generator ./cmd/gpubox-sbom-generator
file /tmp/gpubox-sbom-generator
```

Expected: all tests pass and `file` reports a statically linked Go executable.

- [ ] **Step 6: Commit the protocol runner**

```bash
git add sbom-generator/internal/compact \
  sbom-generator/internal/runner \
  sbom-generator/cmd/gpubox-sbom-generator
git commit --signoff -m "feat(sbom): add BuildKit generator runner"
```

---

### Task 3: Immutable Generator Image and Protocol Test

**Files:**

- Modify: `.gitignore`
- Create: `sbom-generator/.dockerignore`
- Create: `sbom-generator/Containerfile`
- Create: `sbom-generator/tests/protocol.sh`

**Interfaces:**

- Consumes: static `sbom-generator/bin/gpubox-sbom-generator` built by Go
- Produces: OCI generator image whose entrypoint is `/bin/gpubox-sbom-generator`
- Produces: `sbom-generator/tests/protocol.sh GENERATOR_IMAGE SUBJECT_IMAGE`

- [ ] **Step 1: Write the failing container-contract test commands**

Before creating the container definition, run:

```bash
cd sbom-generator
mkdir -p bin
CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' \
  -o bin/gpubox-sbom-generator ./cmd/gpubox-sbom-generator
docker build -f Containerfile -t gpubox-sbom-generator:test .
```

Expected RED: Docker reports that `Containerfile` does not exist.

- [ ] **Step 2: Add immutable container context**

Append to root `.gitignore`:

```gitignore
sbom-generator/bin/
```

Create `sbom-generator/.dockerignore`:

```dockerignore
**
!Containerfile
!bin/
!bin/gpubox-sbom-generator
```

Create `sbom-generator/Containerfile`:

```dockerfile
FROM docker/buildkit-syft-scanner@sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68 AS upstream

FROM scratch
COPY --from=upstream /bin/syft-scanner /bin/syft-scanner
COPY bin/gpubox-sbom-generator /bin/gpubox-sbom-generator
ENTRYPOINT ["/bin/gpubox-sbom-generator"]
```

Do not add a mutable tag, package installation, or network step.

- [ ] **Step 3: Build and inspect the generator image GREEN**

Run:

```bash
cd sbom-generator
docker build -f Containerfile -t gpubox-sbom-generator:test .
docker image inspect gpubox-sbom-generator:test \
  --format '{{json .Config.Entrypoint}} {{json .Config.Cmd}}'
docker history --no-trunc gpubox-sbom-generator:test
```

Expected: entrypoint is `["/bin/gpubox-sbom-generator"]`, command is empty,
and final history contains only the two copied binaries and entrypoint.

- [ ] **Step 4: Write the reusable protocol test**

Create executable `sbom-generator/tests/protocol.sh`. It must:

1. require exactly two positional image references: generator image first and
   subject image second;
2. create temporary rootfs, baseline-output, and compact-output directories
   with `mktemp -d`;
3. create and export the subject container into the rootfs;
4. always remove the temporary subject container and directories in a trap;
5. run the pinned upstream scanner from the generator image by overriding its
   entrypoint to `/bin/syft-scanner`, writing to the baseline-output directory;
6. run the compact generator normally, writing to the compact-output
   directory, with:

```bash
-e BUILDKIT_SCAN_DESTINATION=/run/out
-e BUILDKIT_SCAN_SOURCE=/run/src/core/sbom
-v "${rootfs}:/run/src/core/sbom:ro,z"
-v "${output}:/run/out:z"
```

7. validate both `sbom.spdx.json` files with Python's standard library,
   asserting:
   - `predicateType == "https://spdx.dev/Document"`;
   - the baseline contains at least one package and at least one file;
   - compact package JSON values equal baseline package JSON values except for
     filtered `hasFiles` entries;
   - the compact output has no `files` field;
   - every relationship retained from the baseline has neither endpoint in
     the baseline file-ID set;
   - every baseline relationship with neither endpoint in the baseline
     file-ID set remains in the compact output;
   - compact file size is no larger than `32 << 20`.

The script must use `set -euo pipefail`, quote all expansions, and print only
the final byte/package/relationship counts.

- [ ] **Step 5: Run protocol test against the exact diagnostic image**

Use the already built current image if present; otherwise build it:

```bash
docker image inspect gpubox:sbom-current >/dev/null 2>&1 || \
  docker build -f vscode/Containerfile -t gpubox:sbom-current vscode
sbom-generator/tests/protocol.sh \
  gpubox-sbom-generator:test \
  gpubox:sbom-current
```

Expected: output is approximately 10 MB, reports at least 4,400 packages for
the current dependency set, reports no file array, and exits zero.

- [ ] **Step 6: Commit image and protocol test**

```bash
git add .gitignore sbom-generator/.dockerignore \
  sbom-generator/Containerfile sbom-generator/tests/protocol.sh
git commit --signoff -m "build(sbom): package the compact generator"
```

---

### Task 4: CI Tests, Change Detection, and Digest-Chained Publication

**Files:**

- Modify: `.github/workflows/pre-commit.yml`
- Modify: `.github/workflows/build.yml`

**Interfaces:**

- Consumes: `sbom-generator/` source and immutable upstream scanner digest
- Produces: required CI test coverage for `go test ./...` and static build
- Produces: generator image digest in `steps.sbom_generator.outputs.digest`
- Produces: gpubox SBOM generator reference using the captured GHCR digest
- Preserves: `containerfile_changed`, `image_tag`, and `image_digest` job outputs

- [ ] **Step 1: Write a failing workflow contract check**

Before editing workflows, run:

```bash
python3 - <<'PY'
from pathlib import Path

build = Path('.github/workflows/build.yml').read_text()
precommit = Path('.github/workflows/pre-commit.yml').read_text()
required = [
    'gpubox-sbom-generator',
    'sbom-generator/Containerfile',
    'steps.sbom_generator.outputs.digest',
    'generator=ghcr.io/${{ github.repository_owner }}/gpubox-sbom-generator@${{ steps.sbom_generator.outputs.digest }}',
]
missing = [item for item in required if item not in build]
if 'go test ./...' not in precommit:
    missing.append('pre-commit Go tests')
if not missing:
    raise SystemExit('workflow unexpectedly already satisfies the new contract')
raise SystemExit('missing expected workflow contract: ' + ', '.join(missing))
PY
```

Expected RED: the command exits nonzero and lists the absent generator
integration.

- [ ] **Step 2: Add generator tests to the pre-commit workflow**

After checkout and before release-script validation, add:

```yaml
- name: Test SBOM generator
  shell: bash
  run: |
    set -euo pipefail
    cd sbom-generator
    go test ./...
    CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' \
      -o /tmp/gpubox-sbom-generator ./cmd/gpubox-sbom-generator
```

Do not download a separate Go toolchain; use the runner-provided toolchain.

- [ ] **Step 3: Extend rebuild detection**

In `Detect image context changes`:

- add `changed_generator_files=()`;
- when a comparison base exists, collect `git diff --name-only -z` under
  `sbom-generator/` separately from `vscode/`;
- set `changed=true` when either `effective_vscode_files` or
  `changed_generator_files` is nonempty;
- add a `Generator changes (trigger rebuild)` summary section;
- retain the existing ignore handling only for `vscode/`;
- keep the legacy `containerfile_changed` output name.

For `workflow_dispatch`, compare the dispatched head with
`git merge-base origin/main HEAD` rather than only `HEAD^`. This is required so
a trusted dispatch of a multi-commit feature branch sees generator changes
made before the final workflow commit. Label that base source as
`workflow-dispatch merge base (origin/main)` in the job summary.

For a missing comparison base, continue rebuilding defensively.

- [ ] **Step 4: Add generator metadata, compilation, and image build**

After builder setup and authentication, add these rebuild-gated steps:

```yaml
- name: Test and compile compact SBOM generator
  if: steps.changes.outputs.containerfile_changed == 'true'
  shell: bash
  run: |
    set -euo pipefail
    cd sbom-generator
    go test ./...
    mkdir -p bin
    CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' \
      -o bin/gpubox-sbom-generator ./cmd/gpubox-sbom-generator

- name: SBOM generator metadata
  id: sbom_generator_meta
  if: steps.changes.outputs.containerfile_changed == 'true'
  uses: docker/metadata-action@dc802804100637a589fabce1cb79ff13a1411302 # v6
  with:
    images: ghcr.io/${{ github.repository_owner }}/gpubox-sbom-generator
    tags: |
      type=ref,event=branch
      type=ref,event=tag
      type=sha,format=short
      type=raw,value=latest,enable={{is_default_branch}}
```

Then build the generator with the already pinned Blacksmith action:

```yaml
- name: Build compact SBOM generator
  id: sbom_generator
  if: steps.changes.outputs.containerfile_changed == 'true'
  uses: useblacksmith/build-push-action@9b0579bbec7a6cad2f171596c57e7ac1e7658850 # v2
  with:
    context: ./sbom-generator
    file: ./sbom-generator/Containerfile
    platforms: linux/amd64
    push: ${{ github.event_name != 'pull_request' }}
    tags: ${{ steps.sbom_generator_meta.outputs.tags }}
    sbom: generator=docker/buildkit-syft-scanner@sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68
    provenance: mode=max
```

On trusted events, immediately validate the digest:

```bash
[[ "${{ steps.sbom_generator.outputs.digest }}" =~ ^sha256:[0-9a-f]{64}$ ]]
```

Do not publish the generator to Docker Hub.

- [ ] **Step 5: Chain the generator digest into the gpubox SBOM**

Replace the main pushed image's `sbom: true` with:

```yaml
sbom: generator=ghcr.io/${{ github.repository_owner }}/gpubox-sbom-generator@${{ steps.sbom_generator.outputs.digest }}
```

Retain:

```yaml
provenance: mode=max
```

Do not use the generator tag produced by metadata. The captured digest is the
only allowed reference.

- [ ] **Step 6: Add generator publication summary**

After a trusted generator push, write a job-summary section containing:

- the generator manifest digest;
- the immutable GHCR reference;
- the upstream Syft scanner digest;
- a statement that the generator's own SBOM uses the upstream scanner and the
  gpubox SBOM uses the newly published compact generator.

Never print registry credentials or GitHub tokens.

- [ ] **Step 7: Run the workflow contract check GREEN**

Rerun the Step 1 Python check, changing its terminal logic to exit zero only
when `missing` is empty. Also run:

```bash
git diff --check
actionlint .github/workflows/build.yml .github/workflows/pre-commit.yml
cd sbom-generator && go test ./...
```

If `actionlint` is not installed, use the repository's existing pinned
pre-commit/CI validation and record local unavailability; do not download an
unpinned binary.

- [ ] **Step 8: Commit workflow integration**

```bash
git add .github/workflows/build.yml .github/workflows/pre-commit.yml
git commit --signoff -m "ci(sbom): publish compact image attestations"
```

---

### Task 5: Full Local Verification and Exact-Head Review

**Files:**

- Modify only files required to correct failures found by the specified gates
- Do not change chart versions, `appVersion`, or image release tags

**Interfaces:**

- Consumes: Tasks 1-4 exact head
- Produces: frozen, locally verified PR candidate

- [ ] **Step 1: Run the complete local validation matrix**

Run:

```bash
git diff --check origin/main...HEAD

cd sbom-generator
go test ./...
test ! -e go.sum
test "$(go list -m all | wc -l)" -eq 1
mkdir -p bin
CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' \
  -o bin/gpubox-sbom-generator ./cmd/gpubox-sbom-generator
cd ..

docker build -f sbom-generator/Containerfile \
  -t gpubox-sbom-generator:test sbom-generator

sbom-generator/tests/protocol.sh \
  gpubox-sbom-generator:test \
  gpubox:sbom-current

bash scripts/release/tests/run.sh
helm lint charts/gpubox
helm template gpubox charts/gpubox --namespace gpubox >/tmp/gpubox-rendered.yaml
```

Expected: every command exits zero; protocol output is below 32 MiB with the
full current package inventory; chart and release checks remain unchanged.

- [ ] **Step 2: Run repository pre-commit if available**

Run:

```bash
pre-commit run --all-files --show-diff-on-failure
```

If the host pre-commit installation is broken or absent, record the exact
error and rely on the required GitHub `pre-commit` check after independently
running the Go, release, Helm, and diff checks above. Do not alter the host
environment as part of this repository change.

- [ ] **Step 3: Review exact scope and dependency pins**

Run:

```bash
git status --short
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- \
  .gitignore sbom-generator .github/workflows/build.yml \
  .github/workflows/pre-commit.yml docs/superpowers
rg -n 'buildkit-syft-scanner|uses:' \
  sbom-generator .github/workflows/build.yml \
  .github/workflows/pre-commit.yml
```

Verify every new image reference uses the approved digest, every action uses a
full SHA, no generated binary is tracked, and no unrelated file is changed.

- [ ] **Step 4: Dispatch context-isolated reviews**

Request two read-only reviews of the exact head:

1. correctness review of JSON preservation, atomicity, and failure behavior;
2. CI/security review of digest chaining, event permissions, push conditions,
   change detection, and attestation recursion.

Only P0-P2 correctness, security, or required-gate findings block delivery.
Batch fixes into one candidate, add a regression test before each behavior fix,
commit with `--signoff`, rerun the full relevant matrix, then re-review the new
exact head once.

- [ ] **Step 5: Freeze the candidate**

When all local gates pass and exact-head review has no P0-P2 findings, do not
make opportunistic edits. Record:

```bash
git rev-parse HEAD
git status --short
```

Expected: clean worktree and a single frozen head SHA ready for publication.

---

### Task 6: PR, Trusted Branch Build, Merge, and Main Verification

**Files:**

- No planned source changes; failure corrections must follow Task 5's TDD and
  exact-head review loop

**Interfaces:**

- Consumes: frozen candidate from Task 5
- Produces: merged PR and successful post-merge `main` image rebuild

- [ ] **Step 1: Push branch and open the PR**

```bash
git push -u origin bcdonadio/fix-sbom-attestation
gh pr create \
  --title "fix(ci): compact oversized image SBOM attestations" \
  --body-file /tmp/gpubox-sbom-pr-body.md
```

The PR body must include:

- the 51,291,569-byte Syft reproduction and 49,285,128-byte Scout result;
- the approximately 10,032,106-byte compact result;
- the package/file/relationship counts;
- why BuildKit downgrade, limit bypass, and detached-only SBOM were rejected;
- immutable upstream and custom generator digest flow;
- local unit, protocol, release, Helm, and workflow validation;
- the Socket HTTP 500 result without claiming a successful score;
- risk: file-level SPDX records are intentionally omitted while package data
  is preserved;
- rollback: signed revert that retains SBOM enforcement.

- [ ] **Step 2: Wait for all required PR checks**

Run:

```bash
gh pr checks --watch
```

Do not proceed while any required check is pending or failing. Inspect and fix
failures through the TDD loop; every new fix gets a signed-off commit and an
exact-head re-review.

- [ ] **Step 3: Manually dispatch the trusted feature-branch build**

After PR checks pass, run:

```bash
gh workflow run build.yml --ref bcdonadio/fix-sbom-attestation
```

Resolve the new run ID and actively monitor the entire `container-image` job:

```bash
run_id="$(gh run list --workflow build.yml \
  --branch bcdonadio/fix-sbom-attestation \
  --event workflow_dispatch --limit 1 \
  --json databaseId --jq '.[0].databaseId')"
test -n "${run_id}"
gh run watch "${run_id}" --exit-status
```

Healthy container-image duration is approximately 2-8 minutes; monitor for up
to 12 minutes before treating lack of progress as abnormal.

- [ ] **Step 4: Verify trusted-branch image and attestations**

From the successful run, verify:

- generator build/push succeeded and exposed a valid digest;
- gpubox build/push succeeded without `exceeds 41943040 bytes`;
- GHCR and Docker Hub received the feature-branch/SHA gpubox tags;
- `docker buildx imagetools inspect` finds both SPDX and provenance
  attestations;
- extracted SPDX is below 32 MiB, contains the expected package inventory,
  contains no `files` array, and has no dangling removed-file relationships;
- provenance predicate remains `mode=max` scope.

Record the run URL and immutable gpubox/generator digests in the PR.

- [ ] **Step 5: Perform final merge gate**

Confirm:

```bash
gh pr view --json number,state,isDraft,mergeStateStatus,reviewDecision,statusCheckRollup
git fetch origin --prune
git merge-base --is-ancestor origin/main HEAD
```

If `origin/main` advanced, rebase with signed commits, rerun required checks and
trusted integration at the new exact head. Merge only when the PR is clean,
all required checks pass, exact-head reviews are clear, and the trusted branch
build proves the fix.

- [ ] **Step 6: Merge through an allowed method**

Prefer repository-supported squash merge, otherwise merge commit, otherwise
rebase merge. Do not use `--admin`:

```bash
gh pr merge --squash --delete-branch
```

- [ ] **Step 7: Verify canonical main and monitor post-merge build**

```bash
git fetch origin --prune
git switch main
git pull --ff-only origin main
git status --short --branch
```

Find the merge-triggered `build` run and actively monitor it through the full
`container-image` step and overall workflow completion. Verify the final
`latest` and SHA image digest, attached compact SPDX statement, `mode=max`
provenance, generator digest, and successful chart job.

- [ ] **Step 8: Final inventory**

Verify:

```bash
git status --short --branch
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
gh pr list --state open --head bcdonadio/fix-sbom-attestation --json number
```

Report the merged PR, merge commit, feature-branch and main workflow URLs,
generator and gpubox digests, measured compact SBOM size/counts, all validation
results, and the explicit fact that no release tag was created.
