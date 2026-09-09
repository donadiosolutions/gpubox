# Managed DNS runtime acceptance

These tests use the production controller script and digest-pinned CoreDNS and
gpubox images. They require existing Docker, Python 3, Helm, and kubectl tools.
They do not join a tailnet or use authentication credentials. The Docker suite
creates isolated bridge networks, including a fixture-only 100.100.100.0/24
network so the real controller can probe its normal MagicDNS address.

Run the container transport and controller tests:

```bash
bash scripts/chart/tests/dns-runtime/run.sh controller
```

The suite checks client answers and upstream query logs, including suffix
boundaries, ordered upstream fallback, NXDOMAIN/NODATA, TCP and truncation,
status failures, suffix changes, reload rejection, and controller/CoreDNS
restarts. Transition assertions observe traffic and enforce the healthy-runtime
12-second acceptance target. The controller and test fixtures use Python's
standard library. There is no DNSSEC validation claim.

For Kubernetes lifecycle acceptance, first create an isolated Kubernetes
1.34.11 Kind cluster, and supply its explicit kubeconfig and cluster name:

```bash
bash scripts/chart/tests/dns-runtime/run.sh kind \
  --kubeconfig /absolute/path/to/isolated-kubeconfig \
  --cluster isolated-kind-cluster
```

Do not supply a production kubeconfig. The suite checks the exact server
version, creates temporary namespaces, installs the real chart, and tests
restart and OnDelete upgrade/rollback behavior. It uses controlled LocalAPI
and DNS fixtures for credential-free MagicDNS transitions. It removes only its
own namespaces. The cluster remains the caller's responsibility. `--keep`
retains test namespaces or Docker fixtures for diagnosis.

The application image is large. Preload its exact chart-selected reference if
the local node cannot pull it reliably. Older Kind versions may not understand
the newer node's containerd configuration during image loading; verify image
identity and availability in the node before running the suite. Substituting a
smaller controller image does not prove the chart-selected image works.

Authenticated live-tailnet transitions, retained real Tailscale identity, and
real IPv6-only tailnet behavior require separately supplied isolated access.
Controlled status and DNS tests cannot establish those properties.
