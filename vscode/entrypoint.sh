#!/usr/bin/env bash
set -euo pipefail

MNT_HOME="${HOME:-/home/gpubox}"
TUNNEL_NAME="${TUNNEL_NAME:-gpubox}"
WORKDIR="${WORKDIR:-/home/gpubox/workspace}"
FALLBACK_TMPDIR="${MNT_HOME}/.tmp"

is_writable_dir_for_gpubox() {
  local dir="$1"
  gosu gpubox:gpubox test -d "${dir}" && gosu gpubox:gpubox test -w "${dir}"
}

# Ensure the mounted home has expected directories.
mkdir -p "${WORKDIR}" \
         "${MNT_HOME}/.cache" \
         "${MNT_HOME}/.config" \
         "${MNT_HOME}/.local/share" \
         "${FALLBACK_TMPDIR}"

# Avoid recursive chown (don’t punish yourself if home is huge).
chown gpubox:gpubox "${MNT_HOME}" || true
chown -R gpubox:gpubox "${MNT_HOME}/.config" "${MNT_HOME}/.local" "${MNT_HOME}/.cache" "${WORKDIR}" "${FALLBACK_TMPDIR}" || true

# PVC-backed /tmp mounts are commonly created without 01777 semantics.
# VS Code needs a writable temp dir to create singleton sockets.
chmod 1777 /tmp || true

EFFECTIVE_TMPDIR="${TMPDIR:-/tmp}"
if ! is_writable_dir_for_gpubox "${EFFECTIVE_TMPDIR}"; then
  if is_writable_dir_for_gpubox /tmp; then
    EFFECTIVE_TMPDIR="/tmp"
  else
    chmod 700 "${FALLBACK_TMPDIR}" || true
    EFFECTIVE_TMPDIR="${FALLBACK_TMPDIR}"
  fi
fi
export TMPDIR="${EFFECTIVE_TMPDIR}"
export TMP="${EFFECTIVE_TMPDIR}"
export TEMP="${EFFECTIVE_TMPDIR}"

# `code tunnel` no longer accepts a workspace positional argument.
# Run from WORKDIR so the tunnel starts in the expected folder.
cd "${WORKDIR}"
exec gosu gpubox:gpubox \
  code tunnel --name "${TUNNEL_NAME}" --accept-server-license-terms
