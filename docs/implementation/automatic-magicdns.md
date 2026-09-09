# Automatic MagicDNS implementation record

The approved scope is chart 2.9.0, targeting Kubernetes 1.34 or newer and
acceptance testing on 1.34.11. Application image contents and appVersion stay
unchanged. Production deployment, release publication, shared tailnet DNS
changes, and Tailscale SSH are outside this change. Existing OpenSSH access
continues to use the tailnet network interface.

## Required behavior

The default-enabled pod-local CoreDNS instance forwards only the discovered,
validated active MagicDNS suffix to MagicDNS first. All other wire QNAMEs go
to the original Kubernetes resolvers, in order. MagicDNS transport errors,
SERVFAIL, and REFUSED fall back within the suffix forwarder; NXDOMAIN and
NOERROR/NODATA remain authoritative responses. No MagicDNS search suffix is
added, and existing search domains and options are retained.

The controller reads bounded LocalAPI status without peers as a non-root
user every five seconds. It requires Running state, enabled MagicDNS, valid
suffix/self names, and a successful direct self-address probe before enabling
the route. Disabled, absent, malformed, or unavailable state selects
cluster-only forwarding. Requests and probes have separate one-second
budgets. IPv6-only nodes use fd7a:115c:a1e0::53; other nodes use 100.100.100.100.

A static application resolv.conf is materialized before the initial Corefile
and never replaced after startup. It contains only the loopback nameserver.
The original snapshot survives controller restart. Application containers
mount the static file through subPath; CoreDNS directory-mounts its dynamic
configuration, updated with atomic rename and fsync. Controller, CoreDNS,
and Tailscale retain their runtime-provided resolver files.

Initialization order is DNS volume setup, controller native sidecar, CoreDNS
native sidecar, existing Tailscale initialization/sidecar, authorized-key and
user initialization, then applications. Startup probes enforce file creation
and ready DNS before applications. DNS listens only on loopback port 53;
HTTP health/readiness are pod-reachable on 8080/8181. The controller heartbeat
measures reconciliation progress independently of Tailscale availability.

Tailscale always receives TS_ACCEPT_DNS=false. Its existing acceptDNS value
now controls managed discovery. The controller has neither state-volume nor
operator access. CoreDNS runs as 65532 with only NET_BIND_SERVICE; the
controller is non-root with no capabilities. No pod-wide fsGroup is added.
User containers receive the resolver mount through deliberate copy/injection;
managed names, volumes, paths, and declared ports must reject collisions.

## Availability interpretation

For dual-stack status, one matching self-address answer is sufficient. The
controller may try both present families within the shared probe deadline;
an unavailable AAAA record must not disable a working A response. No answer
may activate routing unless its question, transaction, type, and address
match the validated discovery state, and its owner is the queried self name
or is reachable through a validated CNAME chain in that response.

## Delivery and acceptance

Maintained entry points are `scripts/chart/tests/run.sh`,
`scripts/chart/tests/test_dns_controller.py`, and
`scripts/chart/tests/dns-runtime/run.sh`. The dependency evidence is recorded
separately in [the CoreDNS security record](../security/coredns-1.14.7.md).

Implementation proceeds through controller/unit tests, chart/render tests,
then real CoreDNS and isolated Kubernetes tests. Every downloaded dependency
must have an exact version and integrity verification. The official CoreDNS
v1.14.7 index digest is
sha256:7efd3c635b03efd68c4e8398fc45f0d993d0e9ab016f72c1cefb0fd6d01aa286.
Its Socket module assessment is separate from image provenance, SBOM, and
image vulnerability evidence; the security record retains scoped dependency
adjudication rather than claiming a vulnerability-free binary.

Acceptance observes upstream traffic, not merely generated configuration.
The healthy-runtime transition target is 12 seconds. Tests cover routing
boundaries, negative answers, transport fallback, reloads, restart behavior,
strict input parsing, lifecycle gates, mount injection, no-Tailscale operation,
LocalAPI read versus mutation permissions, and OnDelete upgrade/rollback.
Helm lint, chart/release suites, package validation, and diff checks complete
repository validation. Commits require DCO signoff and explanatory bodies.

Authenticated live-Tailnet transitions and real IPv6-only Tailnet acceptance
must remain explicitly unverified unless credentials and an appropriate
isolated environment are supplied. A CoreDNS crash can interrupt application
DNS until restart; secondary application nameservers are deliberately absent.

For OnDelete migration, keep the old tailnet DNS rule, upgrade, recreate the
Pod, verify DNS/fallback/identity, then remove the rule only if unshared.
Restore old DNS requirements before rollback and old-Pod recreation.

## Implementation progress

- Controller, chart 2.9.0, documentation, image update rule, and CI unit-test
  entry point are implemented.
- The final controller unit/socket suite passed 29 tests. The complete chart
  render suite and release fixtures passed.
- The pinned-image Docker suite passed routing boundaries, ordered fallback,
  negative-answer preservation, suffix changes, bounded transitions, restart,
  static-inode retention, malformed LocalAPI state, and invalid Corefile reload
  rejection while continuing to serve the previous configuration.
- The final selected gpubox image passed a credential-free, network-isolated
  LocalAPI permission test as UID/GID 65532: status returned HTTP 200 and
  preference mutation was denied. Python 3.14.4 is present.
- Actual-chart Kubernetes 1.34.11 acceptance passed: no-Tailscale startup and
  cluster DNS; non-root writes and read-only static resolver; controller and
  CoreDNS restarts without application restart or resolver inode replacement;
  controlled LocalAPI enable/disable/malformed/disconnected/recovery routing;
  OnDelete upgrade from 2.8.2 and rollback; and the missing-Python startup gate.
- The isolated node required direct OCI import, recovery from stale image
  pulls, and switching its native snapshotter to overlayfs after verifying
  the exact image starts there. Native unpack copied cumulative layers to
  22 GB. The chart reference locally aliased its verified AMD64 child manifest;
  this test setup did not validate a fresh multi-architecture registry pull.
- No production or shared tailnet configuration has been changed.
