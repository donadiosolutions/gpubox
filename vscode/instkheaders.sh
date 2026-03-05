#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: instkheaders [kernel-release]

Install kernel-version-matched Ubuntu packages for eBPF tooling:
  - linux-headers-<kernel-release>
  - linux-tools-<kernel-release>
  - linux-cloud-tools-<kernel-release>

If [kernel-release] is omitted, the running kernel from `uname -r` is used.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

KERNEL_RELEASE="${1:-$(uname -r)}"

if [[ "$(id -u)" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo --preserve-env=DEBIAN_FRONTEND "$0" "${KERNEL_RELEASE}"
  fi
  echo "instkheaders: root privileges are required (run as root or install sudo)." >&2
  exit 1
fi

export DEBIAN_FRONTEND="${DEBIAN_FRONTEND:-noninteractive}"
REQUESTED_PACKAGES=(
  "linux-headers-${KERNEL_RELEASE}"
  "linux-tools-${KERNEL_RELEASE}"
  "linux-cloud-tools-${KERNEL_RELEASE}"
)

echo "instkheaders: refreshing apt metadata..."
apt-get update

AVAILABLE_PACKAGES=()
MISSING_PACKAGES=()
for pkg in "${REQUESTED_PACKAGES[@]}"; do
  if apt-cache show "${pkg}" >/dev/null 2>&1; then
    AVAILABLE_PACKAGES+=("${pkg}")
  else
    MISSING_PACKAGES+=("${pkg}")
  fi
done

if [[ "${#AVAILABLE_PACKAGES[@]}" -eq 0 ]]; then
  echo "instkheaders: no matching packages found for kernel ${KERNEL_RELEASE}." >&2
  if [[ "${#MISSING_PACKAGES[@]}" -gt 0 ]]; then
    printf 'instkheaders: missing packages: %s\n' "${MISSING_PACKAGES[*]}" >&2
  fi
  exit 2
fi

echo "instkheaders: installing ${AVAILABLE_PACKAGES[*]}"
apt-get install -y --no-install-recommends "${AVAILABLE_PACKAGES[@]}"

if [[ "${#MISSING_PACKAGES[@]}" -gt 0 ]]; then
  printf 'instkheaders: warning, unavailable packages: %s\n' "${MISSING_PACKAGES[*]}" >&2
fi

echo "instkheaders: done for kernel ${KERNEL_RELEASE}"
