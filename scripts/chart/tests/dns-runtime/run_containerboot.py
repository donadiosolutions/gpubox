#!/usr/bin/env python3
"""Verify the chart's credential-free Tailscale LocalAPI socket contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any


SOCKET = "/var/run/gpubox-tailscale/tailscaled.sock"
HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[3]
CONTROLLER_DIR = REPO / "charts/gpubox/files"
PREFIX = f"gpubox-dns-containerboot-{os.getpid()}"


def docker(*arguments: str, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("docker", *arguments), check=check, text=True, **kwargs)


def chart_contract() -> tuple[str, str, str, str]:
    rendered = subprocess.run(
        (
            "helm",
            "template",
            "containerboot",
            str(REPO / "charts/gpubox"),
            "--show-only",
            "templates/statefulset.yaml",
            "--kube-version",
            "1.34.11",
            "--set",
            "tailscale.enabled=true",
            "--set",
            "tailscale.acceptRoutes=false",
            "--set-string",
            "tailscale.authKey.value=test-only-never-used",
        ),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    init_section = rendered.stdout.split("      initContainers:\n", 1)[1].split("      containers:\n", 1)[0]
    blocks = {
        match.group(1): match.group(2)
        for match in re.finditer(
            r"(?ms)^        - name: ([^\n]+)\n(.*?)(?=^        - name: |\Z)",
            init_section,
        )
    }
    tailscale = blocks["tailscale"]
    controller = blocks["dns-controller"]
    socket_env = f"            - name: TS_SOCKET\n              value: {SOCKET}\n"
    assert socket_env in tailscale, tailscale
    assert "            runAsUser: 0\n" in tailscale, tailscale
    assert "            runAsUser: 65532\n" in controller, controller
    assert "            runAsGroup: 65532\n" in controller, controller
    assert "            readOnlyRootFilesystem: true\n" in controller, controller
    assert "              drop:\n                - ALL\n" in controller, controller

    def image(block: str) -> str:
        match = re.search(r'^          image: "?([^"\n]+)"?$', block, re.MULTILINE)
        assert match, block
        return match.group(1)

    setup = blocks["dns-setup"]
    setup_image = image(setup)
    setup_args = re.search(r"(?ms)^          args:\n            - \|\n(.*?)(?=^          volumeMounts:)", setup)
    assert setup_args, setup
    setup_command = "\n".join(
        line.removeprefix("              ") for line in setup_args.group(1).splitlines()
    ).rstrip()
    return image(tailscale), image(controller), setup_image, setup_command


def test_dns_setup(app_image: str, setup_image: str, setup_command: str) -> None:
    source = f"{PREFIX}-source"
    code = f"{PREFIX}-code"
    state = f"{PREFIX}-state"
    for volume in (source, code, state):
        docker("volume", "create", volume, stdout=subprocess.DEVNULL)
    try:
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "-u",
            "0:0",
            "-v",
            f"{source}:/source",
            "-v",
            f"{CONTROLLER_DIR}:/input:ro",
            "--entrypoint",
            "/bin/sh",
            app_image,
            "-c",
            "cp /input/dns-controller.py /source/dns-controller.py && chmod 0444 /source/dns-controller.py",
            stdout=subprocess.DEVNULL,
        )
        for _ in range(2):
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop=ALL",
                "--cap-add=CHOWN",
                "--security-opt=no-new-privileges",
                "--security-opt=label=disable",
                "-u",
                "0:0",
                "-v",
                f"{source}:/opt/gpubox-dns-source:ro",
                "-v",
                f"{code}:/opt/gpubox-dns",
                "-v",
                f"{state}:/var/run/gpubox-dns",
                "--entrypoint",
                "/bin/sh",
                setup_image,
                "-c",
                setup_command,
                stdout=subprocess.DEVNULL,
            )
        result = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "-u",
            "65532:65532",
            "-v",
            f"{code}:/opt/gpubox-dns:ro",
            "-v",
            f"{state}:/var/run/gpubox-dns",
            "--entrypoint",
            "/bin/sh",
            app_image,
            "-c",
            (
                "sha256sum /opt/gpubox-dns/dns-controller.py; "
                "stat -c '%a:%u:%g' /opt/gpubox-dns/dns-controller.py; "
                "stat -c '%a:%u:%g' /var/run/gpubox-dns"
            ),
            stdout=subprocess.PIPE,
        )
        lines = result.stdout.splitlines()
        expected = hashlib.sha256((CONTROLLER_DIR / "dns-controller.py").read_bytes()).hexdigest()
        assert lines[0].split()[0] == expected, result.stdout
        assert lines[1:] == ["444:0:0", "750:65532:65532"], result.stdout
    finally:
        for volume in (source, code, state):
            docker("volume", "rm", volume, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_socket(volume: str, app_image: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "-u",
            "65532:65532",
            "-v",
            f"{volume}:/var/run/gpubox-tailscale:ro",
            "--entrypoint",
            "/bin/sh",
            app_image,
            "-c",
            f"test -S {SOCKET}",
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise AssertionError(f"containerboot did not create {SOCKET}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    daemon = f"{PREFIX}-daemon"
    volume = f"{PREFIX}-socket"
    client_volume = f"{PREFIX}-client"
    tailscale_image, app_image, setup_image, setup_command = chart_contract()
    docker("volume", "create", volume, stdout=subprocess.DEVNULL)
    docker("volume", "create", client_volume, stdout=subprocess.DEVNULL)
    try:
        test_dns_setup(app_image, setup_image, setup_command)
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "-u",
            "0:0",
            "-v",
            f"{client_volume}:/client",
            "--entrypoint",
            "/bin/sh",
            tailscale_image,
            "-c",
            "cp /usr/local/bin/tailscale /client/tailscale && chmod 0755 /client/tailscale",
            stdout=subprocess.DEVNULL,
        )
        docker(
            "run",
            "-d",
            "--name",
            daemon,
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "-u",
            "0:0",
            "-e",
            "TS_KUBE_SECRET=",
            "-e",
            "TS_STATE_DIR=mem:",
            "-e",
            "TS_AUTH_ONCE=true",
            "-e",
            "TS_USERSPACE=true",
            "-e",
            "TS_ACCEPT_DNS=false",
            "-e",
            f"TS_SOCKET={SOCKET}",
            "-e",
            "TS_ENABLE_HEALTH_CHECK=true",
            "-e",
            "TS_LOCAL_ADDR_PORT=127.0.0.1:9002",
            "-v",
            f"{volume}:/var/run/gpubox-tailscale",
            tailscale_image,
            stdout=subprocess.DEVNULL,
        )
        wait_for_socket(volume, app_image)

        reader = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "-u",
            "65532:65532",
            "-v",
            f"{volume}:/var/run/gpubox-tailscale:ro",
            "-v",
            f"{CONTROLLER_DIR}:/opt/gpubox-dns:ro",
            "-v",
            f"{client_volume}:/client:ro",
            "--entrypoint",
            "/usr/bin/python3",
            app_image,
            "-I",
            "-B",
            "-c",
            (
                "import json,runpy;"
                "p='/opt/gpubox-dns/dns-controller.py';"
                f"v=runpy.run_path(p)['fetch_localapi_status']('{SOCKET}');"
                "print(json.dumps({'BackendState':v.get('BackendState'),"
                "'CurrentTailnet':v.get('CurrentTailnet'),'Version':v.get('Version')},sort_keys=True))"
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        status = json.loads(reader.stdout)
        assert status["BackendState"] == "NeedsLogin", status
        assert status["CurrentTailnet"] is None, status
        assert status["Version"].startswith("1.102.3-"), status

        mutation = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "-u",
            "65532:65532",
            "-v",
            f"{volume}:/var/run/gpubox-tailscale:ro",
            "-v",
            f"{client_volume}:/client:ro",
            "--entrypoint",
            "/client/tailscale",
            app_image,
            f"--socket={SOCKET}",
            "set",
            "--accept-dns=false",
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert mutation.returncode != 0, mutation
        denial = mutation.stdout + mutation.stderr
        assert "access denied" in denial.lower(), denial
        print(
            "PASS pinned Tailscale containerboot LocalAPI contract "
            f"({status['BackendState']}; mutation denied)"
        )
    except Exception:
        logs = docker("logs", daemon, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout
        if logs:
            print(f"--- {daemon} logs ---\n{logs}", file=sys.stderr)
        raise
    finally:
        if args.keep:
            print(f"Retaining container {daemon} and volume {volume}", file=sys.stderr)
        else:
            docker("rm", "-f", daemon, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            docker("volume", "rm", volume, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            docker("volume", "rm", client_volume, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
