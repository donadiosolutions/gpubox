#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CHART_DIR="${ROOT_DIR}/charts/gpubox"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_contains() {
  local file="$1"
  local expected="$2"

  grep -Fq -- "${expected}" "${file}" || fail "expected ${file} to contain: ${expected}"
}

assert_not_contains() {
  local file="$1"
  local unexpected="$2"

  if grep -Fq -- "${unexpected}" "${file}"; then
    fail "expected ${file} not to contain: ${unexpected}"
  fi
}

assert_count() {
  local file="$1"
  local expected="$2"
  local needle="$3"
  local actual=""

  actual="$(grep -Fc -- "${needle}" "${file}" || true)"
  [[ "${actual}" == "${expected}" ]] || fail "expected ${file} to contain ${needle} ${expected} time(s), found ${actual}"
}

assert_before() {
  local file="$1"
  local first="$2"
  local second="$3"
  local first_line=""
  local second_line=""

  first_line="$(grep -Fnxm1 -- "${first}" "${file}" | cut -d: -f1 || true)"
  second_line="$(grep -Fnxm1 -- "${second}" "${file}" | cut -d: -f1 || true)"
  [[ -n "${first_line}" ]] || fail "expected ${file} to contain ordered marker: ${first}"
  [[ -n "${second_line}" ]] || fail "expected ${file} to contain ordered marker: ${second}"
  (( first_line < second_line )) || fail "expected ${first} to appear before ${second} in ${file}"
}

expect_render_fail() {
  local name="$1"
  local expected="$2"
  shift 2
  local output="${TMP_DIR}/${name}.log"

  if helm template gpubox "${CHART_DIR}" --namespace gpubox "$@" >"${output}" 2>&1; then
    fail "expected render scenario ${name} to fail"
  fi
  assert_contains "${output}" "${expected}"
}

helm lint "${CHART_DIR}" >/dev/null

chart_version="$(awk '$1 == "version:" { print $2; exit }' "${CHART_DIR}/Chart.yaml")"
[[ -n "${chart_version}" ]] || fail "could not read the chart version from ${CHART_DIR}/Chart.yaml"

null_tailscale_values="${TMP_DIR}/null-tailscale-values.yaml"
printf 'tailscale: null\n' >"${null_tailscale_values}"
expect_render_fail null-tailscale \
  "missing property 'tailscale'" \
  --values "${null_tailscale_values}"

expect_render_fail invalid-hostname \
  "/tailscale/hostname" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set-string 'tailscale.hostname=invalid hostname'

render_default="${TMP_DIR}/default.yaml"
helm template gpubox "${CHART_DIR}" \
  --namespace gpubox \
  --kube-version 1.28.0 >"${render_default}"

assert_not_contains "${render_default}" "name: tailscale"
assert_not_contains "${render_default}" "TS_USERSPACE"
assert_not_contains "${render_default}" "gpubox-tailscale-state"
assert_contains "${render_default}" "helm.sh/chart: gpubox-${chart_version}"

render_enabled="${TMP_DIR}/enabled.yaml"
helm template gpubox "${CHART_DIR}" \
  --namespace gpubox \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=gpubox-tailscale-auth >"${render_enabled}"

assert_contains "${render_enabled}" "name: tailscale-sysctl"
assert_contains "${render_enabled}" "name: tailscale"
assert_contains "${render_enabled}" "restartPolicy: Always"
assert_contains "${render_enabled}" "ghcr.io/tailscale/tailscale:v1.102.2@sha256:321ce041508c19079b57a28b6666c8d81ab0b08accc0a2585b3ab663d557ac24"
assert_contains "${render_enabled}" "name: TS_USERSPACE"
assert_count "${render_enabled}" 1 "name: TS_USERSPACE"
assert_contains "${render_enabled}" "name: TS_ACCEPT_DNS"
assert_contains "${render_enabled}" "--accept-routes=true --shields-up=false"
assert_contains "${render_enabled}" "name: gpubox-tailscale-state"
assert_contains "${render_enabled}" "claimName: gpubox-tailscale-state"
assert_contains "${render_enabled}" "name: gpubox-tailscale-auth"
assert_count "${render_enabled}" 2 "automountServiceAccountToken: false"
assert_contains "${render_enabled}" "name: TS_KUBE_SECRET"
assert_contains "${render_enabled}" "name: TS_AUTH_ONCE"
assert_contains "${render_enabled}" "name: TS_ENABLE_HEALTH_CHECK"
assert_contains "${render_enabled}" 'value: "127.0.0.1:9002"'
assert_contains "${render_enabled}" "http://127.0.0.1:9002/healthz"
assert_contains "${render_enabled}" "mountPath: \"/var/lib/tailscale\""
assert_count "${render_enabled}" 1 "mountPath: \"/var/lib/tailscale\""
assert_not_contains "${render_enabled}" "livenessProbe:"
assert_not_contains "${render_enabled}" "readinessProbe:"
assert_not_contains "${render_enabled}" "kind: Secret"
assert_before "${render_enabled}" "        - name: tailscale-sysctl" "        - name: tailscale"
assert_before "${render_enabled}" "        - name: tailscale" "        - name: gpubox"

render_inline="${TMP_DIR}/inline.yaml"
inline_auth_key="tskey-auth-inline-test"
helm template gpubox "${CHART_DIR}" \
  --namespace gpubox \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set-string "tailscale.authKey.value=${inline_auth_key}" >"${render_inline}"

assert_contains "${render_inline}" "kind: Secret"
assert_contains "${render_inline}" "name: gpubox-tailscale-auth"
assert_contains "${render_inline}" "type: Opaque"
assert_contains "${render_inline}" "\"TS_AUTHKEY\": \"${inline_auth_key}\""
assert_count "${render_inline}" 1 "${inline_auth_key}"

render_custom="${TMP_DIR}/custom.yaml"
helm template gpubox "${CHART_DIR}" \
  --namespace gpubox \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=custom-auth \
  --set tailscale.authKey.secretKey=authkey \
  --set tailscale.hostname=custom-gpubox \
  --set tailscale.acceptDNS=false \
  --set tailscale.acceptRoutes=false \
  --set-string 'tailscale.extraArgs[0]=--advertise-tags=tag:gpubox' \
  --set tailscale.extraEnv[0].name=TS_DEBUG_FIREWALL_MODE \
  --set tailscale.extraEnv[0].value=auto \
  --set tailscale.state.existingClaim=external-ts-state \
  --set tailscale.state.mountPath=/var/lib/custom-tailscale \
  --set tailscale.resources.requests.cpu=20m \
  --set tailscale.startupProbe.initialDelaySeconds=3 \
  --set tailscale.startupProbe.periodSeconds=5 \
  --set tailscale.startupProbe.timeoutSeconds=2 \
  --set tailscale.startupProbe.failureThreshold=30 >"${render_custom}"

assert_not_contains "${render_custom}" "name: tailscale-sysctl"
assert_not_contains "${render_custom}" "name: gpubox-tailscale-state"
assert_contains "${render_custom}" "claimName: external-ts-state"
assert_contains "${render_custom}" "name: custom-auth"
assert_contains "${render_custom}" "key: authkey"
assert_contains "${render_custom}" 'value: "custom-gpubox"'
assert_contains "${render_custom}" 'value: "false"'
assert_contains "${render_custom}" "--accept-routes=false --shields-up=false --advertise-tags=tag:gpubox"
assert_contains "${render_custom}" "name: TS_DEBUG_FIREWALL_MODE"
assert_contains "${render_custom}" "mountPath: \"/var/lib/custom-tailscale\""
assert_contains "${render_custom}" "cpu: 20m"
assert_contains "${render_custom}" "initialDelaySeconds: 3"
assert_contains "${render_custom}" "periodSeconds: 5"
assert_contains "${render_custom}" "timeoutSeconds: 2"
assert_contains "${render_custom}" "failureThreshold: 30"

render_storage="${TMP_DIR}/storage.yaml"
helm template gpubox "${CHART_DIR}" \
  --namespace gpubox \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=gpubox-tailscale-auth \
  --set tailscale.state.storageClass=fast-block \
  --set tailscale.state.size=2Gi \
  --set tailscale.state.labels.storage=test \
  --set tailscale.state.annotations.backup=enabled >"${render_storage}"

assert_contains "${render_storage}" 'storageClassName: "fast-block"'
assert_contains "${render_storage}" 'storage: "2Gi"'
assert_contains "${render_storage}" "storage: test"
assert_contains "${render_storage}" "backup: enabled"

render_order="${TMP_DIR}/order.yaml"
helm template gpubox "${CHART_DIR}" \
  --namespace gpubox \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=gpubox-tailscale-auth \
  --set-string 'ssh.authorizedKeys[0]=ssh-ed25519 AAAATEST test@example' \
  --set initContainers[0].name=custom-init \
  --set initContainers[0].image=busybox:1.38.0 >"${render_order}"

assert_before "${render_order}" "        - name: tailscale" "        - name: ssh-authorized-keys"
assert_before "${render_order}" "        - name: ssh-authorized-keys" "          name: custom-init"

notes_chart="${TMP_DIR}/notes-chart"
cp -a "${CHART_DIR}" "${notes_chart}"
cp "${notes_chart}/templates/NOTES.txt" "${notes_chart}/notes-source.txt"
printf '%s\n' \
  'apiVersion: v1' \
  'kind: ConfigMap' \
  'metadata:' \
  '  name: notes-test' \
  'data:' \
  '  notes: |-' \
  '{{ tpl (.Files.Get "notes-source.txt") . | nindent 4 }}' \
  >"${notes_chart}/templates/notes-test.yaml"

notes_render="${TMP_DIR}/notes.yaml"
if ! helm template gpubox "${notes_chart}" \
  --namespace gpubox \
  --kube-version 1.29.0 \
  --show-only templates/notes-test.yaml \
  --set tailscale.enabled=true \
  --set-string "tailscale.authKey.value=${inline_auth_key}" >"${notes_render}" 2>&1; then
  sed -n '1,240p' "${notes_render}" >&2
  fail "could not render Helm notes offline"
fi

assert_contains "${notes_render}" "kubectl get svc -n kube-system kube-dns"
assert_contains "${notes_render}" "svc.cluster.local"
assert_contains "${notes_render}" "gpubox-tailscale-state"
assert_not_contains "${notes_render}" "${inline_auth_key}"

expect_render_fail missing-auth \
  "requires exactly one of tailscale.authKey.value or tailscale.authKey.existingSecret" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true

expect_render_fail ambiguous-auth \
  "requires exactly one of tailscale.authKey.value or tailscale.authKey.existingSecret" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set-string tailscale.authKey.value=inline-auth

expect_render_fail whitespace-existing-auth \
  "tailscale.authKey.existingSecret must not contain leading or trailing whitespace" \
  --skip-schema-validation \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set-string tailscale.authKey.value=inline-auth \
  --set-string 'tailscale.authKey.existingSecret= '

expect_render_fail whitespace-inline-auth \
  "tailscale.authKey.value must not contain leading or trailing whitespace" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set-string 'tailscale.authKey.value= inline-auth'

expect_render_fail old-kubernetes \
  "requires Kubernetes 1.29 or newer" \
  --kube-version 1.28.9 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth

expect_render_fail multiple-replicas \
  "requires replicaCount=1" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set replicaCount=2

expect_render_fail host-network \
  "incompatible with pod.hostNetwork=true" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set pod.hostNetwork=true

expect_render_fail service-account-token \
  "requires serviceAccount.automountServiceAccountToken=false" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set serviceAccount.automountServiceAccountToken=true

expect_render_fail empty-digest \
  "/tailscale/image/digest" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set tailscale.image.digest=

expect_render_fail nonprivileged-sidecar \
  "/tailscale/securityContext/privileged" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set tailscale.securityContext.privileged=false

expect_render_fail nonprivileged-sysctl \
  "/tailscale/sysctl/securityContext/privileged" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set tailscale.sysctl.securityContext.privileged=false

expect_render_fail reserved-env \
  "cannot override chart-managed or unsupported authentication variable TS_AUTHKEY" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set tailscale.extraEnv[0].name=TS_AUTHKEY \
  --set tailscale.extraEnv[0].value=forbidden

expect_render_fail reserved-tailscaled-args \
  "cannot override chart-managed or unsupported authentication variable TS_TAILSCALED_EXTRA_ARGS" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set tailscale.extraEnv[0].name=TS_TAILSCALED_EXTRA_ARGS \
  --set-string 'tailscale.extraEnv[0].value=--tun=userspace-networking'

expect_render_fail reserved-pod-name \
  "cannot override chart-managed or unsupported authentication variable POD_NAME" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set tailscale.extraEnv[0].name=POD_NAME \
  --set tailscale.extraEnv[0].value=override

expect_render_fail conflicting-arg \
  "cannot override chart-managed or unsupported flag --accept-routes=false" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set-string 'tailscale.extraArgs[0]=--accept-routes=false'

expect_render_fail single-dash-conflicting-arg \
  "cannot override chart-managed or unsupported flag -accept-routes=false" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set-string 'tailscale.extraArgs[0]=-accept-routes=false'

expect_render_fail alternative-auth-arg \
  "cannot override chart-managed or unsupported flag --client-id=forbidden" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set-string 'tailscale.extraArgs[0]=--client-id=forbidden'

expect_render_fail unsupported-ssh-arg \
  "cannot override chart-managed or unsupported flag --ssh" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set-string 'tailscale.extraArgs[0]=--ssh'

expect_render_fail whitespace-arg \
  "/tailscale/extraArgs/0" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set-string 'tailscale.extraArgs[0]=--ssh --accept-routes=false'

expect_render_fail init-name-collision \
  "initContainers cannot use managed Tailscale container name tailscale" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set initContainers[0].name=tailscale

expect_render_fail sidecar-name-collision \
  "sidecars cannot use managed Tailscale container name tailscale-sysctl" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set sidecars[0].name=tailscale-sysctl

expect_render_fail volume-name-collision \
  "extraVolumes cannot use managed Tailscale volume name tailscale-state" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set extraVolumes[0].name=tailscale-state

expect_render_fail mount-name-collision \
  "extraVolumeMounts cannot mount managed Tailscale state volume tailscale-state" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set extraVolumeMounts[0].name=tailscale-state \
  --set extraVolumeMounts[0].mountPath=/var/lib/tailscale-copy

expect_render_fail init-state-mount-collision \
  "initContainers cannot mount managed Tailscale state volume tailscale-state" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set initContainers[0].name=state-observer \
  --set initContainers[0].image=busybox:1.38.0 \
  --set initContainers[0].volumeMounts[0].name=tailscale-state \
  --set initContainers[0].volumeMounts[0].mountPath=/tailscale-state

expect_render_fail sidecar-state-mount-collision \
  "sidecars cannot mount managed Tailscale state volume tailscale-state" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set sidecars[0].name=state-observer \
  --set sidecars[0].image=busybox:1.38.0 \
  --set sidecars[0].volumeMounts[0].name=tailscale-state \
  --set sidecars[0].volumeMounts[0].mountPath=/tailscale-state

expect_render_fail invalid-boolean \
  "/tailscale/acceptDNS" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set-string tailscale.acceptDNS=invalid

expect_render_fail invalid-pull-policy \
  "/tailscale/image/pullPolicy" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set tailscale.image.pullPolicy=Sometimes

expect_render_fail invalid-access-mode \
  "/tailscale/state/accessModes/0" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set tailscale.state.accessModes[0]=ReadWriteEverywhere

expect_render_fail invalid-volume-mode \
  "/tailscale/state/volumeMode" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set tailscale.state.volumeMode=Block

expect_render_fail invalid-size \
  "/tailscale/state/size" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set tailscale.state.size=large

expect_render_fail invalid-probe \
  "/tailscale/startupProbe/periodSeconds" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set tailscale.startupProbe.periodSeconds=0

expect_render_fail invalid-secret-key \
  "/tailscale/authKey/secretKey" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set-string 'tailscale.authKey.secretKey=bad/key'

expect_render_fail invalid-mount-path \
  "/tailscale/state/mountPath" \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set-string 'tailscale.state.mountPath=/var/lib/tail scale'

expect_render_fail whitespace-existing-claim \
  "tailscale.state.existingClaim must not contain leading or trailing whitespace" \
  --skip-schema-validation \
  --kube-version 1.29.0 \
  --set tailscale.enabled=true \
  --set tailscale.authKey.existingSecret=existing-auth \
  --set-string 'tailscale.state.existingClaim= '

printf 'All Tailscale chart render tests passed.\n'
