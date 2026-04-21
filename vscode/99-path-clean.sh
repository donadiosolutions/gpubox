#!/bin/bash
# 99-path-clean.sh - Remove duplicate entries from the PATH environment.

path_input="${PATH-}"
path_remaining="${path_input}:"
path_entries=()
declare -A seen_entries=()

while [[ "${path_remaining}" == *:* ]]; do
    path_entry="${path_remaining%%:*}"
    path_remaining="${path_remaining#*:}"
    path_key="${#path_entry}:${path_entry}"

    if [[ -v "seen_entries[$path_key]" ]]; then
        continue
    fi

    seen_entries["$path_key"]=1
    path_entries+=("$path_entry")
done

clean_path=""
have_entry=0
for path_entry in "${path_entries[@]}"; do
    if [[ "${have_entry}" -eq 1 ]]; then
        clean_path+=":"
    fi
    clean_path+="${path_entry}"
    have_entry=1
done

export PATH="${clean_path}"

unset clean_path
unset have_entry
unset path_entries
unset path_entry
unset path_input
unset path_key
unset path_remaining
unset seen_entries
