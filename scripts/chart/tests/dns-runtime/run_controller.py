#!/usr/bin/env python3
"""Exercise the shipped controller and CoreDNS with real DNS transports."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


CORE_IMAGE = (
    "registry.k8s.io/coredns/coredns:v1.14.7@"
    "sha256:7efd3c635b03efd68c4e8398fc45f0d993d0e9ab016f72c1cefb0fd6d01aa286"
)
APP_IMAGE = (
    "ghcr.io/donadiosolutions/gpubox:v2.6.1@"
    "sha256:b7439261c35baef39e50f2a6767a990495826b16d8151edb6295878037ac6832"
)
HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[3]
CONTROLLER = REPO / "charts/gpubox/files/dns-controller.py"
PREFIX = "gpubox-dns-impl-controller"


def run(*command: str, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, **kwargs)


def docker(*arguments: str, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return run("docker", *arguments, check=check, **kwargs)


def wait_until(label: str, predicate: Any, timeout: float = 15.0) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {label}; last result: {last!r}")


def transition_time(label: str, predicate: Any, timeout: float = 12.0) -> float:
    started = time.monotonic()
    wait_until(label, predicate, timeout)
    elapsed = time.monotonic() - started
    assert elapsed <= 12.0, (label, elapsed)
    print(f"Transition {label}: {elapsed:.3f}s", flush=True)
    return elapsed


def dig(container: str, name: str, *, tcp: bool = False) -> tuple[str, str]:
    command = [
        "exec",
        container,
        "dig",
        "@127.0.0.1",
        name,
        "A",
        "+tries=1",
        "+time=2",
        "+noall",
        "+comments",
        "+answer",
    ]
    if tcp:
        command.append("+tcp")
    result = docker(*command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    status = "NO_RESPONSE"
    answer = ""
    for line in result.stdout.splitlines():
        if "status:" in line:
            status = line.split("status:", 1)[1].split(",", 1)[0].strip()
        fields = line.split()
        if len(fields) >= 5 and fields[3] == "A":
            answer = fields[4]
    return status, answer


def records(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def write_status(path: pathlib.Path, value: object) -> None:
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(value) + "\n")
    temporary.chmod(0o644)
    os.replace(temporary, path)


def write_raw(path: pathlib.Path, value: str) -> None:
    temporary = path.with_suffix(".new")
    temporary.write_text(value)
    temporary.chmod(0o644)
    os.replace(temporary, path)


def count_name(path: pathlib.Path, name: str, *, transport: str | None = None) -> int:
    return sum(
        1
        for record in records(path)
        if record["qname"].lower() == name.lower()
        and (transport is None or record["transport"] == transport)
    )


def valid_status(*, enabled: bool = True, state: str = "Running", suffix: str = "corp-alpha.ts.net") -> dict[str, object]:
    return {
        "BackendState": state,
        "CurrentTailnet": {
            "MagicDNSEnabled": enabled,
            "MagicDNSSuffix": suffix,
        },
        "Self": {
            "DNSName": f"gpubox.{suffix}.",
            "TailscaleIPs": ["100.64.0.7"],
        },
    }


class Fixture:
    def __init__(self, keep: bool) -> None:
        self.keep = keep
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix=".runtime-", dir=HERE))
        self.cluster_network = f"{PREFIX}-cluster"
        self.magic_network = f"{PREFIX}-magic"
        self.cluster = f"{PREFIX}-upstream"
        self.magic = f"{PREFIX}-magicdns"
        self.secondary = f"{PREFIX}-secondary"
        self.anchor = f"{PREFIX}-anchor"
        self.core = f"{PREFIX}-core"
        self.controller = f"{PREFIX}-app"

    def cleanup(self) -> None:
        if self.keep:
            print(f"Retained fixture at {self.tmp}", file=sys.stderr)
            return
        docker(
            "exec",
            self.controller,
            "/bin/chmod",
            "-R",
            "a+rwX",
            "/run/state",
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for name in (self.controller, self.core, self.anchor, self.cluster, self.secondary, self.magic):
            docker("rm", "-f", name, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for network in (self.magic_network, self.cluster_network):
            docker("network", "rm", network, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        docker(
            "run",
            "--rm",
            "--entrypoint",
            "/bin/chown",
            "--security-opt=label=disable",
            "-v",
            f"{self.tmp}:/cleanup",
            APP_IMAGE,
            "-R",
            f"{os.getuid()}:{os.getgid()}",
            "/cleanup",
            stdout=subprocess.DEVNULL,
        )
        shutil.rmtree(self.tmp)

    def start_upstream(
        self, name: str, network: str, ip: str, role: str, address: str, log: pathlib.Path
    ) -> None:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            "--ip",
            ip,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "-v",
            f"{HERE}:/fixture:ro",
            "-v",
            f"{self.tmp}:/run/fixture",
            "--entrypoint",
            "/usr/bin/python3",
            APP_IMAGE,
            "-I",
            "-B",
            "/fixture/fake_dns.py",
            "--role",
            role,
            "--host",
            "0.0.0.0",
            "--address",
            address,
            "--log",
            f"/run/fixture/{log.name}",
            stdout=subprocess.DEVNULL,
        )

    def setup(self) -> None:
        if not CONTROLLER.is_file():
            raise AssertionError(f"production controller does not exist: {CONTROLLER}")
        os.chmod(self.tmp, 0o777)
        for directory in (self.tmp / "state", self.tmp / "localapi", self.tmp / "fixture"):
            directory.mkdir()
            directory.chmod(0o777)
        (self.tmp / "resolv.conf").write_text(
            "nameserver 172.30.53.53\nnameserver 172.30.53.54\n"
            "search default.svc.cluster.local svc.cluster.local cluster.local\n"
            "options ndots:5 timeout:1 attempts:2\n"
        )
        (self.tmp / "resolv.conf").chmod(0o644)
        write_status(self.tmp / "fixture/status.json", valid_status(enabled=False))

        docker("network", "create", "--subnet", "172.30.53.0/24", self.cluster_network)
        docker("network", "create", "--subnet", "100.100.100.0/24", self.magic_network)
        self.start_upstream(
            self.cluster,
            self.cluster_network,
            "172.30.53.53",
            "cluster",
            "10.96.0.53",
            self.tmp / "cluster.jsonl",
        )
        self.start_upstream(
            self.secondary, self.cluster_network, "172.30.53.54",
            "cluster", "10.96.0.54", self.tmp / "secondary.jsonl",
        )
        self.start_upstream(
            self.magic,
            self.magic_network,
            "100.100.100.100",
            "magic",
            "100.64.0.7",
            self.tmp / "magic.jsonl",
        )
        docker(
            "run",
            "-d",
            "--name",
            self.anchor,
            "--network",
            self.cluster_network,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "-u",
            "65532:65532",
            "--entrypoint",
            "/bin/sleep",
            APP_IMAGE,
            "infinity",
            stdout=subprocess.DEVNULL,
        )
        docker("network", "connect", "--ip", "100.100.100.53", self.magic_network, self.anchor)

        controller_command = (
            "set -eu; "
            "/usr/bin/python3 -I -B /fixture/fake_localapi.py "
            "--socket /run/localapi/tailscaled.sock --status /run/fixture/status.json "
            "--log /run/fixture/localapi.jsonl & "
            "while [ ! -S /run/localapi/tailscaled.sock ]; do sleep 0.05; done; "
            "exec /usr/bin/python3 -I -B /opt/gpubox-dns/dns-controller.py run "
            "--state-dir /run/state --resolv-conf /run/input/resolv.conf "
            "--tailscale-socket /run/localapi/tailscaled.sock --magicdns-enabled true"
        )
        docker(
            "run",
            "-d",
            "--name",
            self.controller,
            "--network",
            f"container:{self.anchor}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "-u",
            "65532:65532",
            "-v",
            f"{HERE}:/fixture:ro",
            "-v",
            f"{CONTROLLER.parent}:/opt/gpubox-dns:ro",
            "-v",
            f"{self.tmp}/state:/run/state",
            "-v",
            f"{self.tmp}/localapi:/run/localapi",
            "-v",
            f"{self.tmp}/fixture:/run/fixture",
            "-v",
            f"{self.tmp}/resolv.conf:/run/input/resolv.conf:ro",
            "--entrypoint",
            "/bin/sh",
            APP_IMAGE,
            "-c",
            controller_command,
            stdout=subprocess.DEVNULL,
        )
        wait_until("controller Corefile", lambda: (self.tmp / "state/Corefile").is_file())
        docker(
            "run",
            "-d",
            "--name",
            self.core,
            "--network",
            f"container:{self.anchor}",
            "--read-only",
            "--cap-drop=ALL",
            "--cap-add=NET_BIND_SERVICE",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "-u",
            "65532:65532",
            "-v",
            f"{self.tmp}/state:/etc/gpubox-dns:ro",
            "--entrypoint",
            "/coredns",
            CORE_IMAGE,
            "-conf",
            "/etc/gpubox-dns/Corefile",
            stdout=subprocess.DEVNULL,
        )

    def probe(self, action: str) -> subprocess.CompletedProcess[str]:
        return docker(
            "exec",
            self.controller,
            "/usr/bin/python3",
            "-I",
            "-B",
            "/opt/gpubox-dns/dns-controller.py",
            action,
            "--state-dir",
            "/run/state",
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    fixture = Fixture(args.keep)
    try:
        fixture.setup()

        wait_until("controller startup probe", lambda: fixture.probe("check-startup").returncode == 0)
        assert fixture.probe("check-live").returncode == 0
        docker("exec", fixture.controller, "/bin/mkdir", "-p", "/run/state/empty")
        empty_startup = docker(
            "exec",
            fixture.controller,
            "/usr/bin/python3",
            "-I",
            "-B",
            "/opt/gpubox-dns/dns-controller.py",
            "check-startup",
            "--state-dir",
            "/run/state/empty",
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        empty_live = docker(
            "exec",
            fixture.controller,
            "/usr/bin/python3",
            "-I",
            "-B",
            "/opt/gpubox-dns/dns-controller.py",
            "check-live",
            "--state-dir",
            "/run/state/empty",
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert empty_startup.returncode == 1
        assert empty_live.returncode == 1
        original = fixture.tmp / "state/original-resolv.conf"
        client = fixture.tmp / "state/client-resolv.conf"
        assert original.read_bytes() == (fixture.tmp / "resolv.conf").read_bytes()
        expected_client = (
            "nameserver 127.0.0.1\n"
            "search default.svc.cluster.local svc.cluster.local cluster.local\n"
            "options ndots:5 timeout:1 attempts:2\n"
        )
        assert client.read_text() == expected_client
        client_inode = client.stat().st_ino

        wait_until(
            "cluster-only CoreDNS answer",
            lambda: dig(fixture.controller, "host.corp-alpha.ts.net.")[1] == "10.96.0.53",
        )
        write_status(fixture.tmp / "fixture/status.json", valid_status())
        transition_time(
            "active MagicDNS answer",
            lambda: dig(fixture.controller, "host.corp-alpha.ts.net.")[1] == "100.64.0.7",
        )

        cluster_before = len(records(fixture.tmp / "cluster.jsonl"))
        magic_before = len(records(fixture.tmp / "magic.jsonl"))
        cases = [
            ("HOST.CoRp-AlPhA.Ts.NeT.", False, "100.64.0.7"),
            ("corp-alpha.ts.net.", False, "100.64.0.7"),
            ("notcorp-alpha.ts.net.", False, "10.96.0.53"),
            ("bare.", False, "10.96.0.53"),
            ("printer.site.local.", False, "10.96.0.53"),
            ("example.org.", False, "10.96.0.53"),
            ("7.0.64.100.in-addr.arpa.", False, "10.96.0.53"),
            ("tcp.corp-alpha.ts.net.", True, "100.64.0.7"),
            ("truncated.corp-alpha.ts.net.", False, "100.64.0.7"),
            ("kubernetes.default.svc.cluster.local.", False, "10.96.0.53"),
            ("host.other.ts.net.", False, "10.96.0.53"),
            ("corp-alpha.ts.net.evil.", False, "10.96.0.53"),
        ]
        for name, tcp, expected in cases:
            status, answer = dig(fixture.controller, name, tcp=tcp)
            assert (status, answer) == ("NOERROR", expected), (name, status, answer)
        magic_new = records(fixture.tmp / "magic.jsonl")[magic_before:]
        cluster_new = records(fixture.tmp / "cluster.jsonl")[cluster_before:]
        assert [r["qname"].lower() for r in magic_new if r["qname"].lower().startswith("host.")] == [
            "host.corp-alpha.ts.net."
        ]
        assert any(r["qname"] == "tcp.corp-alpha.ts.net." and r["transport"] == "tcp" for r in magic_new)
        assert any(r["qname"] == "truncated.corp-alpha.ts.net." and r["transport"] == "udp" for r in magic_new)
        assert any(r["qname"] == "truncated.corp-alpha.ts.net." and r["transport"] == "tcp" for r in magic_new)
        cluster_names = {r["qname"].lower() for r in cluster_new}
        assert {
            "kubernetes.default.svc.cluster.local.",
            "host.other.ts.net.",
            "corp-alpha.ts.net.evil.",
        } <= cluster_names

        assert all(r["qname"].lower() == "corp-alpha.ts.net." or
                   r["qname"].lower().endswith(".corp-alpha.ts.net.") for r in magic_new)
        assert not records(fixture.tmp / "secondary.jsonl")
        docker("stop", fixture.cluster, stdout=subprocess.DEVNULL)
        wait_until("ordered cluster failover", lambda: dig(fixture.controller, "failover.site.local.")[1] == "10.96.0.54")
        assert count_name(fixture.tmp / "secondary.jsonl", "failover.site.local.") > 0
        docker("start", fixture.cluster, stdout=subprocess.DEVNULL)
        wait_until("first cluster resolver recovery", lambda: dig(fixture.controller, "recover.site.local.")[1] == "10.96.0.53")

        for prefix, expected_status, expected_answer in (
            ("servfail", "NOERROR", "10.96.0.53"),
            ("refused", "NOERROR", "10.96.0.53"),
            ("nxdomain", "NXDOMAIN", ""),
            ("nodata", "NOERROR", ""),
        ):
            query_name = f"{prefix}.corp-alpha.ts.net."
            magic_count = count_name(fixture.tmp / "magic.jsonl", query_name)
            cluster_count = count_name(fixture.tmp / "cluster.jsonl", query_name)
            status, answer = dig(fixture.controller, query_name)
            assert (status, answer) == (expected_status, expected_answer)
            assert count_name(fixture.tmp / "magic.jsonl", query_name) == magic_count + 1
            expected_cluster_delta = 1 if prefix in {"servfail", "refused"} else 0
            assert count_name(fixture.tmp / "cluster.jsonl", query_name) == cluster_count + expected_cluster_delta

        docker("stop", fixture.magic, stdout=subprocess.DEVNULL)
        cluster_down_before = count_name(fixture.tmp / "cluster.jsonl", "down.corp-alpha.ts.net.")
        wait_until(
            "MagicDNS transport fallback",
            lambda: dig(fixture.controller, "down.corp-alpha.ts.net.")[1] == "10.96.0.53",
        )
        assert count_name(fixture.tmp / "cluster.jsonl", "down.corp-alpha.ts.net.") > cluster_down_before
        docker("start", fixture.magic, stdout=subprocess.DEVNULL)

        for bad_status in (
            valid_status(enabled=False),
            valid_status(state="NeedsLogin"),
            {"BackendState": "Running"},
            ["not", "an", "object"],
        ):
            write_status(fixture.tmp / "fixture/status.json", valid_status())
            transition_time("reactivate before invalid state", lambda: dig(fixture.controller, "pre-state.corp-alpha.ts.net.")[1] == "100.64.0.7")
            write_status(fixture.tmp / "fixture/status.json", bad_status)
            transition_time(
                "cluster-only invalid or disabled status",
                lambda: dig(fixture.controller, "transition.corp-alpha.ts.net.")[1]
                == "10.96.0.53",
            )
            assert fixture.probe("check-live").returncode == 0

        write_status(fixture.tmp / "fixture/status.json", valid_status())
        transition_time("reactivate before malformed JSON", lambda: dig(fixture.controller, "pre-json.corp-alpha.ts.net.")[1] == "100.64.0.7")
        write_raw(fixture.tmp / "fixture/status.json", "{malformed")
        transition_time(
            "cluster-only malformed JSON",
            lambda: dig(fixture.controller, "malformed.corp-alpha.ts.net.")[1]
            == "10.96.0.53",
        )
        assert fixture.probe("check-live").returncode == 0

        write_status(fixture.tmp / "fixture/status.json", valid_status())
        transition_time("reactivate before unavailable socket", lambda: dig(fixture.controller, "pre-socket.corp-alpha.ts.net.")[1] == "100.64.0.7")
        (fixture.tmp / "localapi/tailscaled.sock").unlink()
        transition_time(
            "cluster-only unavailable LocalAPI",
            lambda: dig(fixture.controller, "unavailable.corp-alpha.ts.net.")[1]
            == "10.96.0.53",
        )
        assert fixture.probe("check-live").returncode == 0

        assert client.stat().st_ino == client_inode
        write_status(fixture.tmp / "fixture/status.json", valid_status())
        docker("restart", fixture.controller, stdout=subprocess.DEVNULL)
        wait_until("controller restart startup", lambda: fixture.probe("check-startup").returncode == 0)
        transition_time(
            "active MagicDNS after controller restart",
            lambda: dig(fixture.controller, "after-restart.corp-alpha.ts.net.")[1]
            == "100.64.0.7",
        )
        assert client.stat().st_ino == client_inode
        assert client.read_text() == expected_client
        assert fixture.probe("check-live").returncode == 0

        write_status(fixture.tmp / "fixture/status.json", valid_status(suffix="corp-beta.ts.net"))
        transition_time("suffix change", lambda: dig(fixture.controller, "new.corp-beta.ts.net.")[1] == "100.64.0.7")
        assert dig(fixture.controller, "old.corp-alpha.ts.net.")[1] == "10.96.0.53"
        assert all(r["qname"].lower().endswith((".corp-alpha.ts.net.", ".corp-beta.ts.net.")) or
                   r["qname"].lower() in {"corp-alpha.ts.net.", "corp-beta.ts.net."}
                   for r in records(fixture.tmp / "magic.jsonl"))
        docker("pause", fixture.controller, stdout=subprocess.DEVNULL)
        try:
            saved_core = (fixture.tmp / "state/Corefile").read_text()
            write_raw(fixture.tmp / "state/Corefile", ".:53 { invalid_plugin }\n")
            time.sleep(4)
            assert dig(fixture.anchor, "retained.corp-beta.ts.net.")[1] == "100.64.0.7"
            logs = docker("logs", fixture.core, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout
            assert "Unknown directive" in logs or "Reload failed" in logs or "Corefile parse failed" in logs, logs
            write_raw(fixture.tmp / "state/Corefile", saved_core)
        finally:
            docker("unpause", fixture.controller, stdout=subprocess.DEVNULL)
        docker("restart", fixture.core, stdout=subprocess.DEVNULL)
        wait_until("CoreDNS restart", lambda: dig(fixture.controller, "core-restart.corp-beta.ts.net.")[1] == "100.64.0.7")

        localapi_before_disabled = len(records(fixture.tmp / "fixture/localapi.jsonl"))
        docker("exec", fixture.controller, "/bin/mkdir", "-p", "/run/state/disabled")
        disabled = docker(
            "exec",
            fixture.controller,
            "/usr/bin/timeout",
            "2",
            "/usr/bin/python3",
            "-I",
            "-B",
            "/opt/gpubox-dns/dns-controller.py",
            "run",
            "--state-dir",
            "/run/state/disabled",
            "--resolv-conf",
            "/run/input/resolv.conf",
            "--tailscale-socket",
            "/run/localapi/tailscaled.sock",
            "--magicdns-enabled",
            "false",
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert disabled.returncode == 124, disabled
        disabled_corefile = fixture.tmp / "state/disabled/Corefile"
        assert disabled_corefile.is_file()
        assert "100.100.100.100" not in disabled_corefile.read_text()
        assert len(records(fixture.tmp / "fixture/localapi.jsonl")) == localapi_before_disabled

        localapi = records(fixture.tmp / "fixture/localapi.jsonl")
        assert localapi
        assert all(r["request"] == "GET /localapi/v0/status?peers=false HTTP/1.1" for r in localapi)
        assert all(r["host"] == "local-tailscaled.sock" for r in localapi)
        print("PASS controller/CoreDNS runtime integration")
    except Exception:
        for name in (fixture.controller, fixture.core):
            logs = docker("logs", name, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout
            if logs:
                print(f"--- {name} logs ---\n{logs}", file=sys.stderr)
        raise
    finally:
        fixture.cleanup()


if __name__ == "__main__":
    main()
