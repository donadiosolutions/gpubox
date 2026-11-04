#!/usr/bin/env python3
"""Reuse immutable published chart bytes when only archive metadata differs."""

import argparse
from pathlib import Path, PurePosixPath
import shutil
import tarfile


MAX_MEMBERS = 10000
MAX_CONTENT_BYTES = 100 * 1024 * 1024


def chart_contents(path):
    entries = {}
    total_size = 0
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or str(name) == ".":
                raise ValueError("chart archive has an unsafe member path")
            key = str(name)
            if key in entries or len(entries) >= MAX_MEMBERS:
                raise ValueError("chart archive has duplicate or too many members")
            if not (member.isfile() or member.isdir()):
                raise ValueError("chart archive has an unsupported member type")
            total_size += member.size
            if total_size > MAX_CONTENT_BYTES:
                raise ValueError("chart archive exceeds the content size limit")
            if member.isfile():
                with archive.extractfile(member) as source:
                    content = source.read(member.size + 1)
                if len(content) != member.size:
                    raise ValueError("chart archive has a truncated member")
            else:
                content = None
            entries[key] = (member.isdir(), member.mode, content)
    if not entries:
        raise ValueError("chart archive is empty")
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("published", type=Path)
    args = parser.parse_args()
    if chart_contents(args.candidate) != chart_contents(args.published):
        raise SystemExit(
            "Chart content changed under an existing version; bump the chart version."
        )
    shutil.copyfile(args.published, args.candidate)
    print("Reused published chart bytes; archive timestamps do not create a release.")


if __name__ == "__main__":
    main()
