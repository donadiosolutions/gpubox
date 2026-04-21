#!/usr/bin/env bash
set -euo pipefail

MNT_HOME="${HOME:-/home/gpubox}"
TUNNEL_NAME="${TUNNEL_NAME:-gpubox}"
WORKDIR="${WORKDIR:-/home/gpubox/workspace}"
FALLBACK_TMPDIR="${MNT_HOME}/.tmp"
CONDA_HOME="${MNT_HOME}/.conda"
CONDA_ENVS_DIR="${CONDA_HOME}/envs"
CONDA_PKGS_DIR="${CONDA_HOME}/pkgs"
SSH_HOSTKEY_DIR="${SSH_HOSTKEY_DIR:-${MNT_HOME}/.gpubox/ssh-hostkeys}"
SSHD_READINESS_PORT="${SSHD_READINESS_PORT:-22}"
SSHD_READINESS_TIMEOUT_SECONDS="${SSHD_READINESS_TIMEOUT_SECONDS:-10}"
PODMAN_GRAPHROOT="${PODMAN_GRAPHROOT:-${MNT_HOME}/.local/share/containers/storage}"
DEFAULT_PATH="/opt/conda/bin:/home/gpubox/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

if [[ -z "${PATH:-}" ]]; then
  export PATH="${DEFAULT_PATH}"
fi

is_writable_dir_for_gpubox() {
  local dir="$1"
  gosu gpubox:gpubox test -d "${dir}" && gosu gpubox:gpubox test -w "${dir}"
}

copy_file_if_distinct() {
  local src="$1"
  local dest="$2"

  # Bind mounts can expose the same host file at different paths; skip those copies.
  if [[ "${src}" == "${dest}" ]] || [[ -e "${dest}" && "${src}" -ef "${dest}" ]]; then
    return 0
  fi

  cp -f "${src}" "${dest}"
}

ensure_dir_owned() {
  local dir="$1"
  local owner="$2"
  local group="$3"
  local mode="${4:-0755}"

  if [[ -e "${dir}" && ! -d "${dir}" ]]; then
    echo "ERROR: ${dir} exists but is not a directory." >&2
    return 1
  fi

  if [[ ! -d "${dir}" ]]; then
    mkdir -p "${dir}"
    chmod "${mode}" "${dir}" || true
  fi

  chown "${owner}:${group}" "${dir}" || true
}

ensure_file_owned() {
  local path="$1"
  local owner="$2"
  local group="$3"
  local mode="$4"
  local template="${5:-}"

  if [[ -e "${path}" && ! -f "${path}" ]]; then
    echo "ERROR: ${path} exists but is not a regular file." >&2
    return 1
  fi

  if [[ ! -e "${path}" ]]; then
    if [[ -n "${template}" && -f "${template}" ]] && [[ "${template}" != "${path}" ]]; then
      cp -f "${template}" "${path}"
    else
      install -m "${mode}" /dev/null "${path}"
    fi

    chmod "${mode}" "${path}" || true
  fi

  chown "${owner}:${group}" "${path}" || true
}

append_managed_block_if_missing() {
  local path="$1"
  local marker="$2"
  local content="$3"

  if grep -Fq "${marker}" "${path}" 2>/dev/null; then
    return 0
  fi

  if [[ -s "${path}" ]]; then
    printf '\n' >>"${path}"
  fi

  printf '%s\n' "${content}" >>"${path}"
}

file_has_active_reference() {
  local path="$1"
  local pattern="$2"

  grep -Eq "^[[:space:]]*[^#[:space:]].*${pattern}" "${path}" 2>/dev/null
}

ensure_shell_init_for_home() {
  local home_dir="$1"
  local owner="$2"
  local group="$3"
  local bashrc_path="${home_dir}/.bashrc"
  local profile_path="${home_dir}/.profile"
  local bashrc_dir="${home_dir}/.bashrc.d"
  local profile_dir="${home_dir}/.profile.d"
  local profile_creds_path="${home_dir}/.profilecreds"
  local bashrc_template=""
  local profile_template=""
  local bashrc_block=""
  local profile_dir_block=""
  local profile_creds_block=""

  if [[ "${home_dir}" != "/etc/skel" ]]; then
    bashrc_template="/etc/skel/.bashrc"
    profile_template="/etc/skel/.profile"
  fi

  ensure_dir_owned "${home_dir}" "${owner}" "${group}"
  ensure_dir_owned "${bashrc_dir}" "${owner}" "${group}"
  ensure_dir_owned "${profile_dir}" "${owner}" "${group}"
  ensure_file_owned "${bashrc_path}" "${owner}" "${group}" 0644 "${bashrc_template}"
  ensure_file_owned "${profile_path}" "${owner}" "${group}" 0644 "${profile_template}"
  ensure_file_owned "${profile_creds_path}" "${owner}" "${group}" 0700
  chmod 0700 "${profile_creds_path}" || true

  read -r -d '' bashrc_block <<'EOF' || true
# >>> gpubox-managed shell init: ~/.bashrc.d >>>
if [ -d "$HOME/.bashrc.d" ]; then
  for rc in "$HOME"/.bashrc.d/*; do
    [ -r "$rc" ] || continue
    [ -f "$rc" ] || continue
    . "$rc"
  done
fi
unset rc
# <<< gpubox-managed shell init: ~/.bashrc.d <<<
EOF

  read -r -d '' profile_dir_block <<'EOF' || true
# >>> gpubox-managed shell init: ~/.profile.d >>>
if [ -d "$HOME/.profile.d" ]; then
  for rc in "$HOME"/.profile.d/*; do
    [ -r "$rc" ] || continue
    [ -f "$rc" ] || continue
    . "$rc"
  done
fi
unset rc
# <<< gpubox-managed shell init: ~/.profile.d <<<
EOF

  read -r -d '' profile_creds_block <<'EOF' || true
# >>> gpubox-managed shell init: ~/.profilecreds >>>
if [ -r "$HOME/.profilecreds" ]; then
  . "$HOME/.profilecreds"
fi
# <<< gpubox-managed shell init: ~/.profilecreds <<<
EOF

  if ! file_has_active_reference "${bashrc_path}" '\.bashrc\.d'; then
    append_managed_block_if_missing "${bashrc_path}" "# >>> gpubox-managed shell init: ~/.bashrc.d >>>" "${bashrc_block}"
  fi

  if ! file_has_active_reference "${profile_path}" '\.profile\.d'; then
    append_managed_block_if_missing "${profile_path}" "# >>> gpubox-managed shell init: ~/.profile.d >>>" "${profile_dir_block}"
  fi

  if ! file_has_active_reference "${profile_path}" '\.profilecreds'; then
    append_managed_block_if_missing "${profile_path}" "# >>> gpubox-managed shell init: ~/.profilecreds >>>" "${profile_creds_block}"
  fi
}

ensure_podman_rootless_runtime() {
  local gpubox_uid=""
  local gpubox_gid=""
  local runtime_dir=""
  local podman_config_dir=""
  local storage_conf=""
  local containers_conf=""

  gpubox_uid="$(id -u gpubox)"
  gpubox_gid="$(id -g gpubox)"
  runtime_dir="${XDG_RUNTIME_DIR:-/run/user/${gpubox_uid}}"
  podman_config_dir="${MNT_HOME}/.config/containers"
  storage_conf="${podman_config_dir}/storage.conf"
  containers_conf="${podman_config_dir}/containers.conf"

  mkdir -p "${runtime_dir}" "${podman_config_dir}" "${PODMAN_GRAPHROOT}"
  chown "${gpubox_uid}:${gpubox_gid}" "${runtime_dir}" "${podman_config_dir}" "${PODMAN_GRAPHROOT}"
  chmod 700 "${runtime_dir}"
  export XDG_RUNTIME_DIR="${runtime_dir}"

  if [[ ! -f "${storage_conf}" ]]; then
    cat >"${storage_conf}" <<EOF
[storage]
driver = "overlay"
runroot = "${runtime_dir}/containers"
graphroot = "${PODMAN_GRAPHROOT}"

[storage.options]
mount_program = "/usr/bin/fuse-overlayfs"
EOF
    chown "${gpubox_uid}:${gpubox_gid}" "${storage_conf}"
    chmod 600 "${storage_conf}"
  fi

  if [[ ! -f "${containers_conf}" ]]; then
    cat >"${containers_conf}" <<'EOF'
[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
cdi_spec_dirs = ["/var/run/cdi", "/etc/cdi"]

[network]
default_rootless_network_cmd = "slirp4netns"

[containers]
annotations = ["run.oci.keep_original_groups=1"]
EOF
    chown "${gpubox_uid}:${gpubox_gid}" "${containers_conf}"
    chmod 600 "${containers_conf}"
  fi
}

ensure_gpubox_gpu_device_access() {
  local device=""
  local gid=""
  local group_name=""
  local seen_groups=""

  shopt -s nullglob
  for device in /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools /dev/nvidia-modeset /dev/nvidia[0-9]* /dev/dri/renderD*; do
    [[ -e "${device}" ]] || continue
    gid="$(stat -c '%g' "${device}")"
    [[ "${gid}" =~ ^[0-9]+$ ]] || continue

    group_name="$(getent group "${gid}" | cut -d: -f1 || true)"
    if [[ -z "${group_name}" ]]; then
      group_name="hostdev${gid}"
      if ! getent group "${group_name}" >/dev/null 2>&1; then
        groupadd -g "${gid}" "${group_name}" || true
      fi
    fi

    if [[ " ${seen_groups} " == *" ${group_name} "* ]]; then
      continue
    fi
    seen_groups="${seen_groups} ${group_name}"
    usermod -aG "${group_name}" gpubox || true
  done
  shopt -u nullglob
}

ensure_podman_cuda_support() {
  local cdi_runtime_dir="/var/run/cdi"
  local oci_hook_dir="/etc/containers/oci/hooks.d"
  local src_dir=""
  local spec=""
  local dest=""
  local found_nvidia_cdi_spec=0
  local found_nvidia_hook=0
  local hook_candidate=""
  local hook_dest="${oci_hook_dir}/oci-nvidia-hook.json"
  local nvidia_runtime_config="/etc/nvidia-container-runtime/config.toml"

  mkdir -p "${cdi_runtime_dir}"
  chmod 0755 "${cdi_runtime_dir}" || true

  # Prefer local CDI generation if nvidia-ctk is available in the image.
  if command -v nvidia-ctk >/dev/null 2>&1; then
    if nvidia-ctk cdi generate --output="${cdi_runtime_dir}/nvidia.yaml" >/dev/null 2>&1; then
      found_nvidia_cdi_spec=1
    else
      echo "WARN: nvidia-ctk CDI generation failed; falling back to existing CDI specs." >&2
    fi
  fi

  # Sync NVIDIA CDI specs from common injection paths (including host root mount at /host).
  for src_dir in /etc/cdi /var/run/cdi /host/etc/cdi /host/var/run/cdi; do
    [[ -d "${src_dir}" ]] || continue
    shopt -s nullglob
    for spec in "${src_dir}"/*.yaml; do
      [[ -f "${spec}" ]] || continue
      if ! grep -q "nvidia.com/gpu" "${spec}"; then
        continue
      fi
      dest="${cdi_runtime_dir}/$(basename "${spec}")"
      copy_file_if_distinct "${spec}" "${dest}"
      chmod 0644 "${dest}" || true
      found_nvidia_cdi_spec=1
    done
    shopt -u nullglob
  done

  mkdir -p "${oci_hook_dir}"
  for hook_candidate in \
    /etc/containers/oci/hooks.d/oci-nvidia-hook.json \
    /usr/share/containers/oci/hooks.d/oci-nvidia-hook.json \
    /host/etc/containers/oci/hooks.d/oci-nvidia-hook.json \
    /host/usr/share/containers/oci/hooks.d/oci-nvidia-hook.json; do
    [[ -f "${hook_candidate}" ]] || continue
    copy_file_if_distinct "${hook_candidate}" "${hook_dest}"
    chmod 0644 "${hook_dest}" || true
    found_nvidia_hook=1
    break
  done

  # Rootless NVIDIA runtime flows need cgroup writes disabled when config exists.
  if [[ -f "${nvidia_runtime_config}" ]]; then
    if grep -Eq '^\s*#?\s*no-cgroups\s*=' "${nvidia_runtime_config}"; then
      sed -ri 's/^\s*#?\s*no-cgroups\s*=.*/no-cgroups = true/' "${nvidia_runtime_config}" || true
    else
      printf '\n[nvidia-container-cli]\nno-cgroups = true\n' >>"${nvidia_runtime_config}" || true
    fi
  fi

  if (( found_nvidia_cdi_spec == 0 )) && [[ -e /dev/nvidiactl ]]; then
    if (( found_nvidia_hook == 0 )); then
      echo "WARN: NVIDIA devices detected but no NVIDIA CDI spec or OCI hook found; rootless Podman CUDA may require manual --device mounts." >&2
    else
      echo "WARN: NVIDIA devices detected with OCI hook fallback only; prefer CDI (--device nvidia.com/gpu=all) when available." >&2
    fi
  fi
}

setup_ssh_host_keys() {
  local key_type=""
  local private_key=""
  local public_key=""
  local etc_private_key=""
  local etc_public_key=""

  # Persist host keys on the mounted home volume to preserve server identity across restarts.
  if ! mkdir -p "${SSH_HOSTKEY_DIR}"; then
    echo "WARN: Unable to create ${SSH_HOSTKEY_DIR}; falling back to ephemeral host keys in /etc/ssh." >&2
    ssh-keygen -A
    return 0
  fi

  chmod 700 "${SSH_HOSTKEY_DIR}" || true
  chown root:root "${SSH_HOSTKEY_DIR}" || true

  for key_type in rsa ecdsa ed25519; do
    private_key="${SSH_HOSTKEY_DIR}/ssh_host_${key_type}_key"
    public_key="${private_key}.pub"
    etc_private_key="/etc/ssh/ssh_host_${key_type}_key"
    etc_public_key="${etc_private_key}.pub"

    if [[ ! -s "${private_key}" ]]; then
      ssh-keygen -q -N "" -t "${key_type}" -f "${private_key}"
    fi

    chmod 600 "${private_key}" || true
    chmod 644 "${public_key}" || true
    chown root:root "${private_key}" "${public_key}" || true

    ln -sf "${private_key}" "${etc_private_key}"
    ln -sf "${public_key}" "${etc_public_key}"
  done
}

wait_for_sshd_ready() {
  local elapsed=0
  while (( elapsed < SSHD_READINESS_TIMEOUT_SECONDS )); do
    if pgrep -x sshd >/dev/null 2>&1 && ss -H -ltn "sport = :${SSHD_READINESS_PORT}" | grep -q .; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  echo "ERROR: sshd failed readiness check on port ${SSHD_READINESS_PORT} within ${SSHD_READINESS_TIMEOUT_SECONDS}s." >&2
  pgrep -a sshd >&2 || true
  ss -ltn >&2 || true
  return 1
}

# Ensure the mounted home has expected directories.
mkdir -p "${WORKDIR}" \
         "${MNT_HOME}/.cache" \
         "${MNT_HOME}/.config" \
         "${MNT_HOME}/.local/share" \
         "${CONDA_ENVS_DIR}" \
         "${CONDA_PKGS_DIR}" \
         "${FALLBACK_TMPDIR}"

ensure_shell_init_for_home /etc/skel root root
ensure_shell_init_for_home /root root root
ensure_shell_init_for_home "${MNT_HOME}" gpubox gpubox

# Avoid recursive chown (don’t punish yourself if home is huge).
chown gpubox:gpubox "${MNT_HOME}" || true
chown -R gpubox:gpubox "${MNT_HOME}/.config" "${MNT_HOME}/.local" "${MNT_HOME}/.cache" "${CONDA_HOME}" "${WORKDIR}" "${FALLBACK_TMPDIR}" || true
ensure_podman_rootless_runtime
ensure_gpubox_gpu_device_access
ensure_podman_cuda_support

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
# Mirror all common temp env vars so child tools consistently use the same writable temp dir.
export TMPDIR="${EFFECTIVE_TMPDIR}"
export TMP="${EFFECTIVE_TMPDIR}"
export TEMP="${EFFECTIVE_TMPDIR}"
export CONDA_ENVS_PATH="${CONDA_ENVS_DIR}"
export CONDA_PKGS_DIRS="${CONDA_PKGS_DIR}"
export MAMBA_ROOT_PREFIX="${CONDA_HOME}"

# Ensure OpenSSH runtime prerequisites are present, then start sshd.
mkdir -p /run/sshd
chmod 0755 /run/sshd
setup_ssh_host_keys
/usr/sbin/sshd
wait_for_sshd_ready

# `code tunnel` no longer accepts a workspace positional argument.
# Run from WORKDIR so the tunnel starts in the expected folder.
cd "${WORKDIR}"
exec gosu gpubox:gpubox \
  code tunnel --name "${TUNNEL_NAME}" --accept-server-license-terms --verbose
