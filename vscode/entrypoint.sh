#!/usr/bin/env bash
set -euo pipefail

MNT_HOME="${HOME:-/home/coder}"
TUNNEL_NAME="${TUNNEL_NAME:-gpubox}"
WORKDIR="${WORKDIR:-/home/coder/workspace}"

# Ensure the mounted home has expected directories.
mkdir -p "${WORKDIR}" \
         "${MNT_HOME}/.cache" \
         "${MNT_HOME}/.config" \
         "${MNT_HOME}/.local/share"

# Avoid recursive chown (don’t punish yourself if home is huge).
chown coder:coder "${MNT_HOME}" || true
chown -R coder:coder "${MNT_HOME}/.config" "${MNT_HOME}/.local" "${MNT_HOME}/.cache" "${WORKDIR}" || true

exec gosu coder:coder \
  code tunnel --name "${TUNNEL_NAME}" --accept-server-license-terms "${WORKDIR}"
