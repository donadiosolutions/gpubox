#!/bin/bash
# 02-path-defaults.sh - Add default directories to the PATH environment.

# `pathmunge` prepends missing entries, so list lowest-precedence paths first.
pathList=(
  "/bin"
  "/sbin"
  "/usr/bin"
  "/usr/sbin"
  "/usr/local/bin"
  "/usr/local/sbin"
  "${CARGO_HOME:-$HOME/.cargo}/bin"
  "${CONDA_DIR:-/opt/conda}/bin"
  "${NPM_CONFIG_PREFIX:-$HOME/.npm-packages}/bin"
  "${PYENV_ROOT:-$HOME/.pyenv}/bin"
  "$HOME/bin"
  "$HOME/.local/bin"
)

for path in ${pathList[@]}; do
  pathmunge "$path"
done

unset path
unset pathList
