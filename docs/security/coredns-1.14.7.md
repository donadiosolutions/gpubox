# CoreDNS 1.14.7 image evidence

This record covers the default DNS sidecar image:

```text
registry.k8s.io/coredns/coredns:v1.14.7@sha256:7efd3c635b03efd68c4e8398fc45f0d993d0e9ab016f72c1cefb0fd6d01aa286
```

It records evidence collected on 2026-09-09. The digest, manifests, extracted
binary, generated SBOM, and vulnerability scan all refer to the same
`linux/amd64` selection. Scanner results are a dated input to review rather
than a claim that the image is vulnerability-free.

## Digest and binary chain

Fetching the pinned reference as a raw manifest produced bytes whose SHA-256
was the pinned index digest. That index selects manifest
`sha256:2329f5f0e7e79fbe56dcdf11ecc4337ee2476bb251095bc275fe9461cb88a55b`
for `linux/amd64`. Hashing that raw manifest reproduced the selected digest;
its config digest is
`sha256:97efff2babf85310d6f87bd969ac64d7006bbbadb147c90579d68d57634750f4`.
Docker resolved both the `registry.k8s.io` reference and the prepared Docker
Hub alias to this config and the same local image.

The `/coredns` executable extracted from that image has SHA-256
`67d7387acb5d58df2c884cd2f0a4a4815f6a6bb29b865a6ea5efa4e782d76faa`.
It reports `CoreDNS-1.14.7`, `linux/amd64`, Go `1.26.6`, and commit
`427fc80`. `go version -m` expands that to unmodified VCS revision
`427fc80ed9ca47f354585eb30a3f1332950856c4` and module pseudo-version
`v0.0.0-20260819003913-427fc80ed9ca`.

The GitHub v1.14.7 release targets the same full commit. Its published
`coredns_1.14.7_linux_amd64.tgz` checksum is
`9a09356438c6d9591cb80011a2a7a52f87fc3a113f6690e41f243f98b98faac9`.
The downloaded archive passed the adjacent upstream checksum file, and its
executable was byte-for-byte identical to `/coredns` extracted from the
image. Compact machine-readable values are in
[`coredns-1.14.7-provenance.json`](coredns-1.14.7-provenance.json).

## Registry provenance and SBOM availability

The registry returned no OCI 1.1 referrers for the pinned index. Its legacy
cosign naming convention exposes a matching `.sig` manifest with eight
simple-signing entries and certificates for
`krel-trust@k8s-releng-prod.iam.gserviceaccount.com`. No matching `.att` or
`.sbom` tag was present.

This collection did not have a pinned `cosign` binary or the Kubernetes
release identity policy needed to validate the signatures, certificate chain,
and transparency-log bundles as one policy decision. The signature envelope
is therefore discovery evidence, not verified provenance. No upstream build
provenance statement or upstream SBOM was found for this exact index digest.

Docker Scout 1.18.1 generated a fresh SPDX 2.3 inventory directly from the
pinned image without installing another tool. It indexed 198 software
packages; the SPDX document has 199 package records when its container subject
is included and 1,343 relationships. A compact inventory record, including
the exact hash of the transient full SPDX document, is in
[`coredns-1.14.7-sbom.json`](coredns-1.14.7-sbom.json). This locally generated
inventory closes package discovery for this review, but it is not an upstream
SBOM attestation.

## Vulnerability scan

Docker Scout 1.18.1 scanned the same pinned image on 2026-09-09. It reported
25 records across five package identities: 17 high, seven medium, and one
unspecified-severity record. The complete compact finding list is in
[`coredns-1.14.7-scan.tsv`](coredns-1.14.7-scan.tsv).

The scan is meaningful image-level evidence, but its raw count requires
adjudication. Scout identifies CoreDNS by its Go pseudo-version. Several
CoreDNS records claim fixes in releases older than 1.14.7, showing that the
scanner compared the pseudo-version incorrectly against release semver. Those
records must not be counted as confirmed vulnerabilities in 1.14.7 without
source-level validation. Conversely, the scan confirms that the binary
contains current flagged modules, including gRPC 1.83.0, etcd client 3.6.13,
`x/crypto` 0.54.0, and `x/mod` 0.37.0; those findings must not be dismissed
solely because some CoreDNS-version matches are false positives.

For the chart's exact Corefile, gRPC CVE-2026-84303, CVE-2026-84304, and
CVE-2026-84445 are present in the compiled dependency inventory but their
affected gRPC/xDS server paths are not instantiated. The configuration enables
ordinary UDP/TCP DNS forwarding plus HTTP health and readiness handlers; it
does not configure `grpc` or `grpc_server`. This reachability decision is
limited to that configuration and must be revisited if those plugins or
transports are enabled.

The other newly reported current-module findings are also outside the runtime
paths instantiated by this Corefile:

- CVE-2026-56854, CVE-2026-56855, and CVE-2026-78662 affect
  `golang.org/x/crypto/ssh` connection setup or channel processing. CoreDNS is
  not configured as an SSH client or server. The chart exposes only DNS over
  UDP/TCP and the two HTTP probe handlers.
- GO-2026-5932 applies to the deprecated `x/crypto/openpgp` packages. The
  image inventory contains the parent `x/crypto` module, but this Corefile
  does not instantiate an OpenPGP consumer.
- CVE-2026-56864 and CVE-2026-56865 affect
  `golang.org/x/mod/sumdb.Client.Lookup` and Go module download verification.
  The immutable executable does not download or verify Go modules at runtime.
  Its module versions and sums are already embedded in `go version -m`.
- CVE-2026-73500 affects the etcd transport package's `NewListener`,
  `NewListenerWithOpts`, `NewTLSListener`, and `NewTimeoutListener`. The
  Corefile does not enable the compiled `etcd` plugin and does not create or
  expose an etcd TLS listener. CoreDNS's etcd plugin is a client of an external
  etcd service rather than an etcd server listener.

These are bounded reachability decisions from the affected symbols published
by the Go vulnerability database and the effective Corefile. They do not
assert that every function in each linked module was proved unreachable.
Changing the Corefile, adding SSH/OpenPGP behavior, adding runtime Go module
downloads, or exposing an etcd listener requires fresh review.

Primary affected-symbol records:

- [CVE-2026-56854 / GO-2026-6303](https://pkg.go.dev/vuln/GO-2026-6303)
- [CVE-2026-56855 / GO-2026-6355](https://pkg.go.dev/vuln/GO-2026-6355)
- [CVE-2026-78662 / GO-2026-6354](https://pkg.go.dev/vuln/GO-2026-6354)
- [GO-2026-5932](https://pkg.go.dev/vuln/GO-2026-5932)
- [CVE-2026-56864 / GO-2026-6180](https://pkg.go.dev/vuln/GO-2026-6180)
- [CVE-2026-56865 / GO-2026-6179](https://pkg.go.dev/vuln/GO-2026-6179)
- [CVE-2026-73500 / GO-2026-6107](https://pkg.go.dev/vuln/GO-2026-6107)

## Socket package assessment

The repository's required pre-adoption command was run for the exact source
version:

```text
socket package score 'pkg:golang/github.com/coredns/coredns@v1.14.7' --json
```

Socket reported 100 for every direct-module score dimension. Its 209-package
transitive graph had minimum scores of 50 overall, 77 vulnerability, 70 supply
chain, 50 maintenance, 60 license, and 95 quality. The direct score is not an
image attestation and does not supersede the Docker Scout image scan.

Socket's high `obfuscatedFile` alert for
`go.uber.org/automaxprocs@v1.6.0` points to
`internal/cgroups/cgroup.go`. Socket's own shallow analysis describes it as a
straightforward, non-malicious cgroup reader and makes path traversal
conditional on untrusted control of its cgroup path or parameter inputs. In
this image, `automaxprocs` reads kernel/container cgroup data during process
initialization; DNS packets and HTTP probe requests do not provide those path
components. Treat this alert as a heuristic mismatch for the reviewed runtime,
while keeping the container's configuration and cgroup/proc mounts outside
client control.

Adoption therefore means accepting a digest-pinned, narrowly configured image
with a recorded scanner baseline and the bounded reachability decisions above.
It does not mean that all 25 scanner records were disproved or that no
vulnerabilities exist. Renovate should continue to propose exact
tag-and-digest updates, and a new CoreDNS build should receive the same
manifest, binary, SBOM, and scan checks before promotion.

## Reproduction commands

The evidence was collected with existing `skopeo`, Docker, Docker Scout,
`oras`, `jq`, `sha256sum`, `cmp`, and Go tooling. No scanner was installed.
The important read-only checks were:

```bash
skopeo inspect --raw \
  docker://registry.k8s.io/coredns/coredns@sha256:7efd3c635b03efd68c4e8398fc45f0d993d0e9ab016f72c1cefb0fd6d01aa286
skopeo inspect --raw \
  docker://registry.k8s.io/coredns/coredns@sha256:2329f5f0e7e79fbe56dcdf11ecc4337ee2476bb251095bc275fe9461cb88a55b
go version -m ./coredns
sha256sum ./coredns
docker scout sbom --format spdx \
  registry.k8s.io/coredns/coredns@sha256:7efd3c635b03efd68c4e8398fc45f0d993d0e9ab016f72c1cefb0fd6d01aa286
docker scout cves --format sarif \
  registry.k8s.io/coredns/coredns@sha256:7efd3c635b03efd68c4e8398fc45f0d993d0e9ab016f72c1cefb0fd6d01aa286
oras discover --format json \
  registry.k8s.io/coredns/coredns@sha256:7efd3c635b03efd68c4e8398fc45f0d993d0e9ab016f72c1cefb0fd6d01aa286
```
