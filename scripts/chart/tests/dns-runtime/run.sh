#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-controller}" in
  controller)
    shift || true
    python3 -I -B "${script_dir}/run_controller.py" "$@"
    ;;
  kind)
    shift
    python3 -I -B "${script_dir}/run_kind.py" "$@"
    ;;
  *)
    echo "usage: $0 {controller|kind} [arguments]" >&2
    exit 2
    ;;
esac
