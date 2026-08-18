#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  printf 'usage: %s GENERATOR_IMAGE SUBJECT_IMAGE\n' "$0" >&2
  exit 2
fi

generator_image=$1
subject_image=$2

rootfs=''
baseline_output=''
compact_output=''
log_dir=''
subject_container=''

cleanup() {
  local status=$?

  if [[ -n "$subject_container" ]]; then
    docker rm -f "$subject_container" >"$log_dir/container-rm.log" 2>&1 || true
  fi
  if [[ -n "$rootfs" ]]; then
    rm -rf -- "$rootfs"
  fi
  if [[ -n "$baseline_output" ]]; then
    rm -rf -- "$baseline_output"
  fi
  if [[ -n "$compact_output" ]]; then
    rm -rf -- "$compact_output"
  fi
  if [[ -n "$log_dir" ]]; then
    rm -rf -- "$log_dir"
  fi

  exit "$status"
}
trap cleanup EXIT

rootfs=$(mktemp -d)
baseline_output=$(mktemp -d)
compact_output=$(mktemp -d)
log_dir=$(mktemp -d)

subject_id=$(docker image inspect "$subject_image" \
  --format '{{.Id}}' \
  2>"$log_dir/subject-inspect.stderr")
if [[ ! "$subject_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  printf 'inspect subject image: invalid image identity\n' >&2
  exit 1
fi

subject_container=$(docker create "$subject_image" \
  2>"$log_dir/container-create.stderr")
if [[ -z "$subject_container" ]]; then
  printf 'create subject container: empty container identity\n' >&2
  exit 1
fi

docker export "$subject_container" \
  2>"$log_dir/container-export.stderr" | \
  tar -xpf - -C "$rootfs" --no-same-owner --no-same-permissions --warning=all \
  2>"$log_dir/rootfs-extract.stderr"

docker run --rm \
  --entrypoint /bin/syft-scanner \
  -e BUILDKIT_SCAN_DESTINATION=/out \
  -e BUILDKIT_SCAN_SOURCE=/scan/sbom \
  -v "${rootfs}:/scan/sbom:ro,z" \
  -v "${baseline_output}:/out:z" \
  "$generator_image" \
  >"$log_dir/baseline-scanner.log" 2>&1

docker run --rm \
  -e BUILDKIT_SCAN_DESTINATION=/out \
  -e BUILDKIT_SCAN_SOURCE=/scan/sbom \
  -v "${rootfs}:/scan/sbom:ro,z" \
  -v "${compact_output}:/out:z" \
  "$generator_image" \
  >"$log_dir/compact-generator.log" 2>&1

baseline_statement="$baseline_output/sbom.spdx.json"
compact_statement="$compact_output/sbom.spdx.json"
if [[ ! -f "$baseline_statement" ]]; then
  printf 'validate baseline output: sbom.spdx.json was not produced\n' >&2
  exit 1
fi
if [[ ! -f "$compact_statement" ]]; then
  printf 'validate compact output: sbom.spdx.json was not produced\n' >&2
  exit 1
fi

SUBJECT_IMAGE="$subject_image" \
SUBJECT_ID="$subject_id" \
BASELINE_STATEMENT="$baseline_statement" \
COMPACT_STATEMENT="$compact_statement" \
python3 - <<'PY'
import collections
import json
import os
from pathlib import Path

SPDX_PREDICATE = "https://spdx.dev/Document"
BASELINE_MAX_BYTES = 40 << 20
COMPACT_MAX_BYTES = 32 << 20


def load_statement(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        statement = json.load(stream)
    if not isinstance(statement, dict):
        raise AssertionError(f"{path}: statement is not an object")
    if statement.get("predicateType") != SPDX_PREDICATE:
        raise AssertionError(f"{path}: predicateType is not SPDX")
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict):
        raise AssertionError(f"{path}: predicate is not an object")
    return statement, predicate


def relationship_key(relationship):
    return json.dumps(relationship, sort_keys=True, separators=(",", ":"))


baseline_path = Path(os.environ["BASELINE_STATEMENT"])
compact_path = Path(os.environ["COMPACT_STATEMENT"])
baseline_bytes = baseline_path.stat().st_size
compact_bytes = compact_path.stat().st_size
if baseline_bytes <= BASELINE_MAX_BYTES:
    raise AssertionError(
        f"baseline statement is {baseline_bytes} bytes, must exceed {BASELINE_MAX_BYTES}"
    )
if compact_bytes >= baseline_bytes:
    raise AssertionError(
        f"compact statement is {compact_bytes} bytes, baseline is {baseline_bytes}"
    )
if compact_bytes > COMPACT_MAX_BYTES:
    raise AssertionError(
        f"compact statement is {compact_bytes} bytes, must not exceed {COMPACT_MAX_BYTES}"
    )

_, baseline_predicate = load_statement(baseline_path)
_, compact_predicate = load_statement(compact_path)

baseline_packages = baseline_predicate.get("packages")
compact_packages = compact_predicate.get("packages")
if not isinstance(baseline_packages, list) or not baseline_packages:
    raise AssertionError("baseline contains no packages")
if not isinstance(compact_packages, list) or not compact_packages:
    raise AssertionError("compact output contains no packages")

baseline_files = baseline_predicate.get("files")
if not isinstance(baseline_files, list) or not baseline_files:
    raise AssertionError("baseline contains no files")
if "files" in compact_predicate:
    raise AssertionError("compact output contains a files field")

file_ids = set()
for index, file_object in enumerate(baseline_files):
    if not isinstance(file_object, dict):
        raise AssertionError(f"baseline file {index} is not an object")
    file_id = file_object.get("SPDXID")
    if not isinstance(file_id, str) or not file_id:
        raise AssertionError(f"baseline file {index} has no non-empty SPDXID")
    if file_id in file_ids:
        raise AssertionError(f"baseline file SPDXID is duplicated: {file_id}")
    file_ids.add(file_id)


def package_map(packages, label):
    result = {}
    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            raise AssertionError(f"{label} package {index} is not an object")
        package_id = package.get("SPDXID")
        if not isinstance(package_id, str) or not package_id:
            raise AssertionError(f"{label} package {index} has no SPDXID")
        if package_id in result:
            raise AssertionError(f"{label} package SPDXID is duplicated: {package_id}")
        result[package_id] = package
    return result


baseline_package_map = package_map(baseline_packages, "baseline")
compact_package_map = package_map(compact_packages, "compact")
if set(baseline_package_map) != set(compact_package_map):
    raise AssertionError("compact package IDs do not equal baseline package IDs")

for package_id, baseline_package in baseline_package_map.items():
    expected_package = dict(baseline_package)
    if "hasFiles" in expected_package:
        has_files = expected_package["hasFiles"]
        if not isinstance(has_files, list):
            raise AssertionError(f"baseline package {package_id} has invalid hasFiles")
        filtered = [file_id for file_id in has_files if file_id not in file_ids]
        if filtered:
            expected_package["hasFiles"] = filtered
        else:
            expected_package.pop("hasFiles")
    if compact_package_map[package_id] != expected_package:
        raise AssertionError(f"compact package changed JSON values for {package_id}")

baseline_relationships = baseline_predicate.get("relationships")
compact_relationships = compact_predicate.get("relationships")
if not isinstance(baseline_relationships, list):
    raise AssertionError("baseline relationships is not an array")
if not isinstance(compact_relationships, list):
    raise AssertionError("compact relationships is not an array")

retained_baseline = []
for relationship in baseline_relationships:
    if not isinstance(relationship, dict):
        raise AssertionError("baseline relationship is not an object")
    endpoints = {
        relationship.get("spdxElementId"),
        relationship.get("relatedSpdxElement"),
    }
    if endpoints.isdisjoint(file_ids):
        retained_baseline.append(relationship)

for relationship in compact_relationships:
    if not isinstance(relationship, dict):
        raise AssertionError("compact relationship is not an object")
    endpoints = {
        relationship.get("spdxElementId"),
        relationship.get("relatedSpdxElement"),
    }
    if not endpoints.isdisjoint(file_ids):
        raise AssertionError("compact relationship references a removed file")

expected_relationships = collections.Counter(
    relationship_key(relationship) for relationship in retained_baseline
)
actual_relationships = collections.Counter(
    relationship_key(relationship) for relationship in compact_relationships
)
if actual_relationships != expected_relationships:
    # Syft resolves package relationships concurrently. Independent scanner
    # runs can select a different SPDXID for an otherwise byte-identical
    # package. Compare those endpoints by the package JSON already proven
    # equal above, while still requiring every relationship to be retained.
    def package_token(package):
        value = dict(package)
        value.pop("SPDXID", None)
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    baseline_package_tokens = {
        package_id: package_token(package)
        for package_id, package in baseline_package_map.items()
    }
    compact_package_tokens = {
        package_id: package_token(package)
        for package_id, package in compact_package_map.items()
    }

    def normalized_relationship_key(relationship, package_tokens):
        normalized = dict(relationship)
        for field in ("spdxElementId", "relatedSpdxElement"):
            endpoint = normalized.get(field)
            if endpoint in package_tokens:
                normalized[field] = "PACKAGE:" + package_tokens[endpoint]
        return relationship_key(normalized)

    normalized_expected = collections.Counter(
        normalized_relationship_key(relationship, baseline_package_tokens)
        for relationship in retained_baseline
    )
    normalized_actual = collections.Counter(
        normalized_relationship_key(relationship, compact_package_tokens)
        for relationship in compact_relationships
    )
    if normalized_actual != normalized_expected:
        raise AssertionError(
            "compact relationships do not equal baseline non-file relationships"
        )

print(
    "subject_image="
    + os.environ["SUBJECT_IMAGE"]
    + " subject_id="
    + os.environ["SUBJECT_ID"]
    + f" baseline_bytes={baseline_bytes} compact_bytes={compact_bytes}"
    + f" baseline_packages={len(baseline_packages)} compact_packages={len(compact_packages)}"
    + f" baseline_relationships={len(baseline_relationships)}"
    + f" compact_relationships={len(compact_relationships)}"
)
PY
