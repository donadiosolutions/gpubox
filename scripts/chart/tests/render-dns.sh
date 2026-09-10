#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CHART_DIR="${ROOT_DIR}/charts/gpubox"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
assert_contains() { grep -Fq -- "$2" "$1" || fail "expected $1 to contain: $2"; }
assert_not_contains() { if grep -Fq -- "$2" "$1"; then fail "expected $1 not to contain: $2"; fi; }
assert_count() {
  local actual
  actual="$(grep -Fc -- "$3" "$1" || true)"
  [[ "${actual}" == "$2" ]] || fail "expected $1 to contain $3 $2 time(s), found ${actual}"
}
assert_line_count() {
  local actual
  actual="$(grep -Fxc -- "$3" "$1" || true)"
  [[ "${actual}" == "$2" ]] || fail "expected $1 to contain exact line $3 $2 time(s), found ${actual}"
}
assert_before() {
  local first_line second_line
  first_line="$(grep -Fnxm1 -- "$2" "$1" | cut -d: -f1 || true)"
  second_line="$(grep -Fnxm1 -- "$3" "$1" | cut -d: -f1 || true)"
  [[ -n "${first_line}" && -n "${second_line}" ]] || fail "missing order marker in $1"
  (( first_line < second_line )) || fail "expected $2 before $3 in $1"
}
assert_adjacent() {
  awk -v first="$2" -v second="$3" '
    $0 == first { if ((getline next_line) > 0 && next_line == second) found = 1 }
    END { exit(found ? 0 : 1) }
  ' "$1" || fail "expected adjacent lines in $1: $2 then $3"
}
extract_named_block() {
  awk -v name="$2" '
    $0 == "        - name: " name || $0 == "        - name: \"" name "\"" { found = 1; started = 1; marker = $0 }
    started && $0 != marker && ($0 ~ /^        - name: / || $0 ~ /^      (volumes|containers):/) { exit }
    found { print }
  ' "$1" >"$3"
  [[ -s "$3" ]] || fail "could not extract container block $2 from $1"
}
expect_render_fail() {
  local name="$1" expected="$2" output
  shift 2
  output="${TMP_DIR}/${name}.log"
  if helm template gpubox "${CHART_DIR}" --namespace gpubox "$@" >"${output}" 2>&1; then
    fail "expected render scenario ${name} to fail"
  fi
  assert_contains "${output}" "${expected}"
}
render_statefulset() {
  local output="$1"
  shift
  helm template gpubox "${CHART_DIR}" --namespace gpubox --kube-version 1.34.11 \
    --show-only templates/statefulset.yaml "$@" >"${output}"
}

default_render="${TMP_DIR}/default.yaml"
render_statefulset "${default_render}"
extract_named_block "${default_render}" dns-setup "${TMP_DIR}/dns-setup.block"
extract_named_block "${default_render}" dns-controller "${TMP_DIR}/dns-controller.block"
extract_named_block "${default_render}" dns "${TMP_DIR}/dns.block"
extract_named_block "${default_render}" gpubox "${TMP_DIR}/gpubox.block"
assert_before "${default_render}" "        - name: dns-setup" "        - name: dns-controller"
assert_before "${default_render}" "        - name: dns-controller" "        - name: dns"
assert_contains "${TMP_DIR}/dns-controller.block" "--magicdns-enabled false"
assert_contains "${TMP_DIR}/dns-controller.block" "/usr/bin/python3 is required in the gpubox image"
assert_contains "${TMP_DIR}/dns-controller.block" "- check-startup"
assert_contains "${TMP_DIR}/dns-controller.block" "- check-live"
assert_contains "${TMP_DIR}/dns-controller.block" "runAsUser: 65532"
assert_contains "${TMP_DIR}/dns-controller.block" "runAsGroup: 65532"
assert_contains "${TMP_DIR}/dns-controller.block" "runAsNonRoot: true"
assert_contains "${TMP_DIR}/dns-controller.block" "readOnlyRootFilesystem: true"
assert_contains "${TMP_DIR}/dns-controller.block" "allowPrivilegeEscalation: false"
assert_count "${TMP_DIR}/dns-controller.block" 3 "mountPath:"
assert_count "${TMP_DIR}/dns-controller.block" 1 "cpu: 10m"
assert_not_contains "${TMP_DIR}/dns-controller.block" "name: home"
assert_not_contains "${TMP_DIR}/dns-controller.block" "name: tailscale-state"
assert_not_contains "${TMP_DIR}/dns-controller.block" "operator"
assert_contains "${TMP_DIR}/dns.block" "registry.k8s.io/coredns/coredns:v1.14.7@sha256:7efd3c635b03efd68c4e8398fc45f0d993d0e9ab016f72c1cefb0fd6d01aa286"
assert_contains "${TMP_DIR}/dns.block" "- /etc/gpubox-dns/Corefile"
assert_contains "${TMP_DIR}/dns.block" "- NET_BIND_SERVICE"
assert_contains "${TMP_DIR}/dns.block" "runAsUser: 65532"
assert_contains "${TMP_DIR}/dns.block" "runAsGroup: 65532"
assert_contains "${TMP_DIR}/dns.block" "runAsNonRoot: true"
assert_contains "${TMP_DIR}/dns.block" "readOnlyRootFilesystem: true"
assert_contains "${TMP_DIR}/dns.block" "path: /ready"
assert_contains "${TMP_DIR}/dns.block" "path: /health"
assert_count "${TMP_DIR}/dns.block" 2 "port: 8181"
assert_count "${TMP_DIR}/dns.block" 1 "port: 8080"
assert_not_contains "${TMP_DIR}/dns.block" "subPath: Corefile"
assert_count "${TMP_DIR}/dns.block" 1 "cpu: 10m"
assert_count "${TMP_DIR}/dns-setup.block" 3 "mountPath:"
assert_not_contains "${TMP_DIR}/dns-setup.block" "name: home"
assert_contains "${TMP_DIR}/dns-setup.block" "chmod 0755 /opt/gpubox-dns"
assert_contains "${TMP_DIR}/dns-setup.block" "cp /opt/gpubox-dns-source/dns-controller.py /opt/gpubox-dns/.dns-controller.py.new"
assert_before "${TMP_DIR}/dns-setup.block" "              chmod 0444 /opt/gpubox-dns/.dns-controller.py.new" "              mv -f /opt/gpubox-dns/.dns-controller.py.new /opt/gpubox-dns/dns-controller.py"
assert_before "${TMP_DIR}/dns-setup.block" "              mv -f /opt/gpubox-dns/.dns-controller.py.new /opt/gpubox-dns/dns-controller.py" "              chmod 0555 /opt/gpubox-dns"
assert_contains "${TMP_DIR}/dns-setup.block" "name: gpubox-dns-controller-source"
assert_contains "${TMP_DIR}/dns-setup.block" "mountPath: /opt/gpubox-dns-source"
assert_contains "${TMP_DIR}/dns-setup.block" "name: gpubox-dns-controller"
assert_contains "${TMP_DIR}/dns-setup.block" "mountPath: /opt/gpubox-dns"
assert_before "${TMP_DIR}/dns-setup.block" "              chown 0:0 /var/run/gpubox-dns" "              chmod 0750 /var/run/gpubox-dns"
assert_before "${TMP_DIR}/dns-setup.block" "              chmod 0750 /var/run/gpubox-dns" "              chown 65532:65532 /var/run/gpubox-dns"
assert_contains "${TMP_DIR}/gpubox.block" "mountPath: /etc/resolv.conf"
assert_contains "${TMP_DIR}/gpubox.block" "subPath: client-resolv.conf"
assert_contains "${TMP_DIR}/gpubox.block" "readOnly: true"
assert_contains "${default_render}" "checksum/dns-controller:"
script_hash="$(sha256sum "${CHART_DIR}/files/dns-controller.py" | cut -d' ' -f1)"
assert_contains "${default_render}" "checksum/dns-controller: ${script_hash}"
assert_count "${default_render}" 2 "ghcr.io/donadiosolutions/gpubox:v2.6.1@sha256:b7439261c35baef39e50f2a6767a990495826b16d8151edb6295878037ac6832"
assert_contains "${default_render}" "name: gpubox-dns-controller"
assert_contains "${default_render}" "name: gpubox-dns-controller-source"
assert_line_count "${default_render}" 2 "            - name: gpubox-dns-controller"
assert_line_count "${default_render}" 1 "        - name: gpubox-dns-controller"
assert_line_count "${default_render}" 1 "            - name: gpubox-dns-controller-source"
assert_line_count "${default_render}" 1 "        - name: gpubox-dns-controller-source"
assert_contains "${TMP_DIR}/dns-controller.block" "mountPath: /opt/gpubox-dns"
assert_not_contains "${TMP_DIR}/dns-controller.block" "gpubox-dns-controller-source"
assert_contains "${default_render}" "name: gpubox-tailscale-socket"
assert_not_contains "${default_render}" "        - name: tailscale"

tailscale_render="${TMP_DIR}/tailscale.yaml"
render_statefulset "${tailscale_render}" --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=gpubox-tailscale-auth
extract_named_block "${tailscale_render}" dns-controller "${TMP_DIR}/tailscale-controller.block"
extract_named_block "${tailscale_render}" tailscale "${TMP_DIR}/tailscale.block"
assert_before "${tailscale_render}" "        - name: dns" "        - name: tailscale-sysctl"
assert_before "${tailscale_render}" "        - name: tailscale-sysctl" "        - name: tailscale"
assert_contains "${TMP_DIR}/tailscale-controller.block" "--magicdns-enabled true"
assert_adjacent "${TMP_DIR}/tailscale.block" "            - name: TS_ACCEPT_DNS" '              value: "false"'
assert_adjacent "${TMP_DIR}/tailscale.block" "            - name: TS_SOCKET" "              value: /var/run/gpubox-tailscale/tailscaled.sock"
assert_contains "${TMP_DIR}/tailscale.block" "name: gpubox-tailscale-socket"
assert_count "${tailscale_render}" 3 "name: gpubox-tailscale-socket"
assert_count "${tailscale_render}" 2 "name: tailscale-state"
assert_not_contains "${TMP_DIR}/tailscale-controller.block" "name: tailscale-state"

tailscale_no_dns_render="${TMP_DIR}/tailscale-no-dns.yaml"
render_statefulset "${tailscale_no_dns_render}" --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=gpubox-tailscale-auth --set tailscale.acceptDNS=false
extract_named_block "${tailscale_no_dns_render}" dns-controller "${TMP_DIR}/tailscale-no-dns-controller.block"
assert_contains "${TMP_DIR}/tailscale-no-dns-controller.block" "--magicdns-enabled false"

disabled_render="${TMP_DIR}/disabled.yaml"
render_statefulset "${disabled_render}" --set dns.enabled=false
assert_not_contains "${disabled_render}" "        - name: dns-setup"
assert_not_contains "${disabled_render}" "        - name: dns-controller"
assert_not_contains "${disabled_render}" "        - name: dns"
assert_not_contains "${disabled_render}" "name: gpubox-dns"
assert_not_contains "${disabled_render}" "name: gpubox-tailscale-socket"

tailscale_dns_disabled_render="${TMP_DIR}/tailscale-dns-disabled.yaml"
helm template gpubox "${CHART_DIR}" --namespace gpubox --kube-version 1.29.0 \
  --show-only templates/statefulset.yaml --set dns.enabled=false \
  --set tailscale.enabled=true --set tailscale.acceptDNS=false \
  --set tailscale.authKey.existingSecret=gpubox-tailscale-auth >"${tailscale_dns_disabled_render}"
extract_named_block "${tailscale_dns_disabled_render}" tailscale "${TMP_DIR}/tailscale-dns-disabled.block"
assert_adjacent "${TMP_DIR}/tailscale-dns-disabled.block" "            - name: TS_ACCEPT_DNS" '              value: "false"'
assert_not_contains "${tailscale_dns_disabled_render}" "        - name: dns-controller"
assert_not_contains "${tailscale_dns_disabled_render}" "name: gpubox-tailscale-socket"

custom_values="${TMP_DIR}/custom-values.yaml"
cat >"${custom_values}" <<'YAML'
tailscale:
  enabled: true
  acceptDNS: true
  authKey:
    existingSecret: gpubox-tailscale-auth
ssh:
  authorizedKeys:
    - ssh-ed25519 AAAATEST render-test
initContainers:
  - name: ordinary-init
    image: busybox:1.38.0
    command: ["sh", "-c"]
    args: ["test -r /etc/resolv.conf"]
    env:
      - name: PRESERVED
        value: init
  - name: native-init
    image: busybox:1.38.0
    restartPolicy: Always
    env:
      - name: PRESERVED
        value: native
sidecars:
  - name: ordinary-sidecar
    image: busybox:1.38.0
    command: ["sleep"]
    args: ["infinity"]
    env:
      - name: PRESERVED
        value: sidecar
YAML
custom_render="${TMP_DIR}/custom.yaml"
render_statefulset "${custom_render}" --values "${custom_values}"
extract_named_block "${custom_render}" ordinary-init "${TMP_DIR}/ordinary-init.block"
extract_named_block "${custom_render}" native-init "${TMP_DIR}/native-init.block"
extract_named_block "${custom_render}" ordinary-sidecar "${TMP_DIR}/ordinary-sidecar.block"
extract_named_block "${custom_render}" ssh-authorized-keys "${TMP_DIR}/ssh-init.block"
assert_before "${custom_render}" "        - name: tailscale" "        - name: ssh-authorized-keys"
assert_before "${custom_render}" "        - name: ssh-authorized-keys" '        - name: "ordinary-init"'
assert_contains "${custom_render}" '        - name: "ordinary-init"'
assert_contains "${custom_render}" '        - name: "native-init"'
assert_contains "${custom_render}" '        - name: "ordinary-sidecar"'
assert_contains "${TMP_DIR}/ordinary-init.block" "value: init"
assert_contains "${TMP_DIR}/ordinary-init.block" "mountPath: /etc/resolv.conf"
assert_not_contains "${TMP_DIR}/ordinary-init.block" "restartPolicy: Always"
assert_contains "${TMP_DIR}/native-init.block" "restartPolicy: Always"
assert_contains "${TMP_DIR}/native-init.block" "value: native"
assert_contains "${TMP_DIR}/native-init.block" "mountPath: /etc/resolv.conf"
assert_contains "${TMP_DIR}/ssh-init.block" "mountPath: /etc/resolv.conf"
assert_contains "${TMP_DIR}/ordinary-sidecar.block" "value: sidecar"
assert_contains "${TMP_DIR}/ordinary-sidecar.block" "- infinity"
assert_contains "${TMP_DIR}/ordinary-sidecar.block" "mountPath: /etc/resolv.conf"

null_dns_values="${TMP_DIR}/null-dns.yaml"
printf 'dns: null\n' >"${null_dns_values}"
expect_render_fail null-dns "missing property 'dns'" --values "${null_dns_values}"
expect_render_fail old-kubernetes "dns.enabled=true requires Kubernetes 1.34 or newer" --kube-version 1.33.9
expect_render_fail host-network "dns.enabled=true is incompatible with pod.hostNetwork=true" --kube-version 1.34.11 --set pod.hostNetwork=true
expect_render_fail service-account-token "dns.enabled=true requires serviceAccount.automountServiceAccountToken=false" --kube-version 1.34.11 --set serviceAccount.automountServiceAccountToken=true
expect_render_fail checksum-annotation "podAnnotations cannot override managed annotation checksum/dns-controller" --kube-version 1.34.11 --set-string 'podAnnotations.checksum/dns-controller=forbidden'
expect_render_fail disabled-magicdns "tailscale.acceptDNS=true requires dns.enabled=true" --kube-version 1.34.11 --set dns.enabled=false --set tailscale.enabled=true --set tailscale.authKey.existingSecret=gpubox-tailscale-auth
expect_render_fail invalid-enabled-type "/dns/enabled" --kube-version 1.34.11 --set-string dns.enabled=yes
expect_render_fail invalid-digest "/dns/image/digest" --kube-version 1.34.11 --set dns.image.digest=sha256:deadbeef
expect_render_fail invalid-pull-policy "/dns/image/pullPolicy" --kube-version 1.34.11 --set dns.image.pullPolicy=Sometimes

name_values="${TMP_DIR}/container-names.yaml"
cat >"${name_values}" <<'YAML'
initContainers:
  - name: "true"
    image: busybox:1.38.0
  - name: "123"
    image: busybox:1.38.0
sidecars:
  - name: "null"
    image: busybox:1.38.0
YAML
name_render="${TMP_DIR}/container-names-render.yaml"
render_statefulset "${name_render}" --values "${name_values}"
assert_contains "${name_render}" '        - name: "true"'
assert_contains "${name_render}" '        - name: "123"'
assert_contains "${name_render}" '        - name: "null"'

invalid_name_values="${TMP_DIR}/invalid-container-names.yaml"
cat >"${invalid_name_values}" <<'YAML'
initContainers:
  - name: "foo #bar"
    image: busybox:1.38.0
YAML
expect_render_fail yaml-significant-name 'initContainers name "foo #bar" must match Kubernetes DNS label syntax' --kube-version 1.34.11 --values "${invalid_name_values}"

cat >"${invalid_name_values}" <<'YAML'
initContainers:
  - name: 123
    image: busybox:1.38.0
YAML
expect_render_fail numeric-name "initContainers name must be a string matching Kubernetes DNS label syntax" --kube-version 1.34.11 --values "${invalid_name_values}"

cat >"${invalid_name_values}" <<'YAML'
sidecars:
  - image: busybox:1.38.0
YAML
expect_render_fail missing-name "sidecars entries require a name matching Kubernetes DNS label syntax" --kube-version 1.34.11 --values "${invalid_name_values}"

cat >"${invalid_name_values}" <<'YAML'
sidecars:
  - name: |-
      line
      break
    image: busybox:1.38.0
YAML
expect_render_fail newline-name "sidecars name" --kube-version 1.34.11 --values "${invalid_name_values}"

too_long_name="$(printf 'a%.0s' {1..64})"
expect_render_fail long-name "contain at most 63 characters" --kube-version 1.34.11 --set-string "sidecars[0].name=${too_long_name}" --set sidecars[0].image=busybox:1.38.0
expect_render_fail operator-flag "unsupported flag --operator=default" --kube-version 1.34.11 --set tailscale.enabled=true --set tailscale.authKey.existingSecret=gpubox-tailscale-auth --set-string 'tailscale.extraArgs[0]=--operator=default'
expect_render_fail triple-operator-flag "unsupported flag ---operator=default" --kube-version 1.34.11 --set tailscale.enabled=true --set tailscale.authKey.existingSecret=gpubox-tailscale-auth --set-string 'tailscale.extraArgs[0]=---operator=default'
expect_render_fail socket-env "variable TS_SOCKET" --kube-version 1.34.11 --set tailscale.enabled=true --set tailscale.authKey.existingSecret=gpubox-tailscale-auth --set tailscale.extraEnv[0].name=TS_SOCKET --set tailscale.extraEnv[0].value=/tmp/forbidden.sock
expect_render_fail tailscale-state-path "tailscale.state.mountPath cannot conflict with managed DNS mount path /var/run/gpubox-dns" --kube-version 1.34.11 --set tailscale.enabled=true --set tailscale.authKey.existingSecret=gpubox-tailscale-auth --set tailscale.state.mountPath=/var/run/gpubox-dns

for collision in \
  'init-name|initContainers[0].name=dns-controller|initContainers cannot use managed DNS container name dns-controller' \
  'sidecar-name|sidecars[0].name=dns|sidecars cannot use managed DNS container name dns' \
  'volume-name|extraVolumes[0].name=gpubox-dns|extraVolumes cannot use managed DNS volume name gpubox-dns' \
  'source-volume-name|extraVolumes[0].name=gpubox-dns-controller-source|extraVolumes cannot use managed DNS volume name gpubox-dns-controller-source' \
  'main-resolver|extraVolumeMounts[0].mountPath=/etc|extraVolumeMounts cannot shadow managed resolver path /etc/resolv.conf' \
  'main-root|extraVolumeMounts[0].mountPath=/|extraVolumeMounts cannot shadow managed resolver path /etc/resolv.conf' \
  'init-runtime|initContainers[0].volumeMounts[0].mountPath=/var/run/gpubox-dns/private|initContainers cannot conflict with managed DNS mount path /var/run/gpubox-dns/private' \
  'sidecar-script|sidecars[0].volumeMounts[0].mountPath=/opt/gpubox-dns|sidecars cannot conflict with managed DNS mount path /opt/gpubox-dns' \
  'init-script-source|initContainers[0].volumeMounts[0].mountPath=/opt/gpubox-dns-source|initContainers cannot conflict with managed DNS mount path /opt/gpubox-dns-source' \
  'sidecar-source-volume|sidecars[0].volumeMounts[0].name=gpubox-dns-controller-source|sidecars cannot mount managed DNS volume gpubox-dns-controller-source' \
  'sidecar-run-alias|sidecars[0].volumeMounts[0].mountPath=/run/gpubox-tailscale|sidecars cannot conflict with managed DNS mount path /run/gpubox-tailscale' \
  'main-dns-port|containerPorts[0].containerPort=53|containerPorts cannot declare managed DNS port 53' \
  'init-health-port|initContainers[0].ports[0].containerPort=8080|initContainers cannot declare managed DNS port 8080' \
  'sidecar-ready-port|sidecars[0].ports[0].containerPort=8181|sidecars cannot declare managed DNS port 8181'; do
  IFS='|' read -r name setting expected <<<"${collision}"
  common=(--kube-version 1.34.11 --set "${setting}")
  case "${name}" in
    init-* ) common+=(--set initContainers[0].name=user-init --set initContainers[0].image=busybox:1.38.0) ;;
    sidecar-* ) common+=(--set sidecars[0].name=user-sidecar --set sidecars[0].image=busybox:1.38.0) ;;
    main-resolver|main-root ) common+=(--set extraVolumeMounts[0].name=user-volume) ;;
  esac
  if [[ "${name}" == "init-name" || "${name}" == "sidecar-name" ]]; then
    common=(--kube-version 1.34.11 --set "${setting}" --set "${setting%%.name=*}.image=busybox:1.38.0")
  fi
  expect_render_fail "${name}" "${expected}" "${common[@]}"
done

printf 'All managed DNS chart render tests passed.\n'
