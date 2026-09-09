#!/usr/bin/env python3
"""Unit and socket-level tests for the pod-local DNS controller."""

from __future__ import annotations

import importlib.util
import ipaddress
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
CONTROLLER_PATH = ROOT / "charts/gpubox/files/dns-controller.py"
SPEC = importlib.util.spec_from_file_location("gpubox_dns_controller", CONTROLLER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {CONTROLLER_PATH}")
dns_controller = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dns_controller
SPEC.loader.exec_module(dns_controller)


VALID_V4_STATUS = {
    "BackendState": "Running",
    "CurrentTailnet": {
        "MagicDNSEnabled": True,
        "MagicDNSSuffix": "corp-alpha.ts.net",
    },
    "Self": {
        "DNSName": "gpubox.corp-alpha.ts.net.",
        "TailscaleIPs": ["100.64.0.42", "fd7a:115c:a1e0::42"],
    },
}


def dns_name(name: str) -> bytes:
    return b"".join(bytes((len(label),)) + label.encode("ascii") for label in name.rstrip(".").split(".")) + b"\0"


def question_from_query(query: bytes) -> bytes:
    offset = 12
    while query[offset]:
        offset += query[offset] + 1
    return query[12 : offset + 5]


def answer_for(query: bytes, address: str | None, *, truncated: bool = False, ident_delta: int = 0) -> bytes:
    ident = (struct.unpack("!H", query[:2])[0] + ident_delta) & 0xFFFF
    flags = 0x8380 if truncated else 0x8180
    question = question_from_query(query)
    if truncated:
        return struct.pack("!HHHHHH", ident, flags, 1, 0, 0, 0) + question
    if address is None:
        return struct.pack("!HHHHHH", ident, flags, 1, 0, 0, 0) + question
    ip = ipaddress.ip_address(address)
    qtype = 1 if ip.version == 4 else 28
    rdata = ip.packed
    rr = b"\xc0\x0c" + struct.pack("!HHIH", qtype, 1, 30, len(rdata)) + rdata
    return struct.pack("!HHHHHH", ident, flags, 1, 1, 0, 0) + question + rr


class UnixHTTPServer:
    def __init__(self, response: bytes, *, delay: float = 0.0):
        self.response = response
        self.delay = delay
        self.requests: list[bytes] = []
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "tailscaled.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def _serve(self):
        try:
            connection, _ = self.server.accept()
            with connection:
                request = b""
                while b"\r\n\r\n" not in request:
                    part = connection.recv(4096)
                    if not part:
                        break
                    request += part
                self.requests.append(request)
                if self.delay:
                    time.sleep(self.delay)
                if self.response:
                    midpoint = max(1, len(self.response) // 2)
                    connection.sendall(self.response[:midpoint])
                    connection.sendall(self.response[midpoint:])
        except OSError:
            pass


class DNSServer:
    def __init__(self, udp_behavior, tcp_behavior=None):
        self.udp_behavior = udp_behavior
        self.tcp_behavior = tcp_behavior or udp_behavior
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("127.0.0.1", 0))
        self.port = self.tcp.getsockname()[1]
        self.tcp.listen(1)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", self.port))
        self.requests: list[tuple[str, bytes]] = []
        self.stop = threading.Event()
        self.threads = [
            threading.Thread(target=self._serve_udp, daemon=True),
            threading.Thread(target=self._serve_tcp, daemon=True),
        ]

    def __enter__(self):
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *_args):
        self.stop.set()
        self.udp.close()
        self.tcp.close()
        for thread in self.threads:
            thread.join(timeout=2)

    def _serve_udp(self):
        try:
            query, peer = self.udp.recvfrom(4096)
            self.requests.append(("udp", query))
            response = self.udp_behavior(query)
            if response is not None:
                self.udp.sendto(response, peer)
        except OSError:
            pass

    def _serve_tcp(self):
        try:
            connection, _ = self.tcp.accept()
            with connection:
                size = struct.unpack("!H", connection.recv(2))[0]
                query = b""
                while len(query) < size:
                    query += connection.recv(size - len(query))
                self.requests.append(("tcp", query))
                response = self.tcp_behavior(query)
                if response is not None:
                    connection.sendall(struct.pack("!H", len(response)) + response)
        except OSError:
            pass


class MultiQueryDNSServer:
    def __init__(self, behavior, *, count: int):
        self.behavior = behavior
        self.count = count
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.port = self.udp.getsockname()[1]
        self.requests: list[bytes] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.udp.close()
        self.thread.join(timeout=2)

    def _serve(self):
        try:
            for _ in range(self.count):
                query, peer = self.udp.recvfrom(4096)
                self.requests.append(query)
                response = self.behavior(query)
                if response is not None:
                    self.udp.sendto(response, peer)
        except OSError:
            pass


class ResolverTests(unittest.TestCase):
    def test_parses_ordered_ipv4_and_ipv6_nameservers_and_preserves_bytes(self):
        original = (
            b"# generated by kubelet\n"
            b"search workload.svc.cluster.local svc.cluster.local cluster.local\n"
            b"nameserver 10.96.0.10\n"
            b"nameserver fd00:10:96::a\n"
            b"options ndots:5 timeout:2\n"
        )
        snapshot = dns_controller.parse_resolv_conf(original)
        self.assertEqual(snapshot.nameservers, ("10.96.0.10", "fd00:10:96::a"))
        self.assertEqual(
            dns_controller.make_client_resolv_conf(snapshot.raw),
            b"# generated by kubelet\n"
            b"search workload.svc.cluster.local svc.cluster.local cluster.local\n"
            b"nameserver 127.0.0.1\n"
            b"options ndots:5 timeout:2\n",
        )

    def test_rejects_missing_malformed_and_recursive_nameservers(self):
        bad_inputs = [
            b"search cluster.local\n",
            b"nameserver not-an-ip\n",
            b"nameserver 0.0.0.0\n",
            b"nameserver ::\n",
            b"nameserver 224.0.0.1\n",
            b"nameserver ff02::1\n",
            b"nameserver 127.0.0.1\n",
            b"nameserver ::1\n",
            b"nameserver 100.100.100.100\n",
            b"nameserver fd7a:115c:a1e0::53\n",
            b"nameserver fe80::1%eth0\n",
            b"nameserver ::ffff:127.0.0.1\n",
            b"nameserver ::ffff:100.100.100.100\n",
            b"nameserver 10.96.0.10 extra\n",
        ]
        for raw in bad_inputs:
            with self.subTest(raw=raw):
                with self.assertRaises(dns_controller.ControllerError):
                    dns_controller.parse_resolv_conf(raw)

    def test_keeps_original_and_client_inodes_across_controller_restarts(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            resolv = Path(temporary) / "resolv.conf"
            first = b"search cluster.local\nnameserver 10.96.0.10\noptions ndots:5\n"
            resolv.write_bytes(first)
            snapshot = dns_controller.initialize_state(state, resolv)
            original_inode = (state / "original-resolv.conf").stat().st_ino
            client_inode = (state / "client-resolv.conf").stat().st_ino
            resolv.write_bytes(b"nameserver 192.0.2.53\n")
            restarted = dns_controller.initialize_state(state, resolv)
            self.assertEqual(restarted.raw, first)
            self.assertEqual((state / "original-resolv.conf").stat().st_ino, original_inode)
            self.assertEqual((state / "client-resolv.conf").stat().st_ino, client_inode)
            self.assertEqual((state / "client-resolv.conf").read_bytes(), dns_controller.make_client_resolv_conf(first))
            self.assertEqual(snapshot, restarted)

    def test_rejects_tampered_retained_client_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            resolv = Path(temporary) / "resolv.conf"
            resolv.write_bytes(b"nameserver 10.96.0.10\n")
            dns_controller.initialize_state(state, resolv)
            (state / "client-resolv.conf").write_bytes(b"nameserver 203.0.113.1\n")
            with self.assertRaises(dns_controller.ControllerError):
                dns_controller.initialize_state(state, resolv)

    def test_failed_initial_fsync_never_publishes_partial_static_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "original-resolv.conf"
            with mock.patch.object(dns_controller.os, "fsync", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    dns_controller._create_once(path, b"nameserver 10.96.0.10\n")
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_rejects_oversize_source_without_an_unbounded_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            resolv = Path(temporary) / "resolv.conf"
            resolv.write_bytes(b"nameserver 10.96.0.10\n" + b"#" * dns_controller.MAX_RESOLV_CONF)
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded source read")):
                with self.assertRaises(dns_controller.ControllerError):
                    dns_controller.initialize_state(state, resolv)
            self.assertFalse((state / "original-resolv.conf").exists())
            self.assertFalse((state / "client-resolv.conf").exists())


class CorefileTests(unittest.TestCase):
    def test_generates_exact_cluster_only_corefile_with_ordered_ipv6_upstream(self):
        got = dns_controller.make_cluster_corefile(("10.96.0.10", "fd00:10:96::a"))
        self.assertEqual(
            got,
            """.:53 {
    bind 127.0.0.1
    errors
    health :8080
    ready :8181 {
        monitor continuously
    }
    reload 2s 1s
    forward . 10.96.0.10 [fd00:10:96::a]:53 {
        policy sequential
        max_fails 1
        health_check 1s
    }
}
""".encode(),
        )

    def test_generates_exact_active_corefile_with_immediate_safe_fallback(self):
        discovery = dns_controller.Discovery(
            suffix="corp-alpha.ts.net.",
            self_name="gpubox.corp-alpha.ts.net.",
            magic_nameserver="100.100.100.100",
            query_type=1,
            expected_ips=frozenset({"100.64.0.42"}),
        )
        got = dns_controller.make_active_corefile(("10.96.0.10",), discovery)
        self.assertEqual(
            got,
            """corp-alpha.ts.net:53 {
    bind 127.0.0.1
    errors
    forward . 100.100.100.100 10.96.0.10 {
        policy sequential
        max_fails 1
        health_check 1s domain gpubox.corp-alpha.ts.net.
        failover SERVFAIL REFUSED
    }
}

.:53 {
    bind 127.0.0.1
    errors
    health :8080
    ready :8181 {
        monitor continuously
    }
    reload 2s 1s
    forward . 10.96.0.10 {
        policy sequential
        max_fails 1
        health_check 1s
    }
}
""".encode(),
        )

    def test_atomic_corefile_replacement_is_deterministic_and_skips_unchanged_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            first = b"first\n"
            self.assertTrue(dns_controller.publish_corefile(state, first))
            inode = (state / "Corefile").stat().st_ino
            self.assertFalse(dns_controller.publish_corefile(state, first))
            self.assertEqual((state / "Corefile").stat().st_ino, inode)
            self.assertTrue(dns_controller.publish_corefile(state, b"second\n"))
            self.assertNotEqual((state / "Corefile").stat().st_ino, inode)
            self.assertEqual((state / "Corefile").read_bytes(), b"second\n")
            self.assertEqual([path.name for path in state.iterdir()], ["Corefile"])


class StatusTests(unittest.TestCase):
    def test_accepts_strict_v4_status_and_normalizes_dns_names(self):
        status = {
            **VALID_V4_STATUS,
            "CurrentTailnet": {**VALID_V4_STATUS["CurrentTailnet"], "MagicDNSSuffix": "CoRp-AlPhA.Ts.NeT"},
            "Self": {**VALID_V4_STATUS["Self"], "DNSName": "GPUBox.Corp-Alpha.TS.NET."},
        }
        got = dns_controller.discovery_from_status(status)
        self.assertEqual(got.suffix, "corp-alpha.ts.net.")
        self.assertEqual(got.self_name, "gpubox.corp-alpha.ts.net.")
        self.assertEqual(got.magic_nameserver, "100.100.100.100")
        self.assertEqual(got.query_type, 1)
        self.assertEqual(got.expected_ips, frozenset({"100.64.0.42", "fd7a:115c:a1e0::42"}))

    def test_uses_ipv6_magicdns_only_for_ipv6_only_self(self):
        status = {
            **VALID_V4_STATUS,
            "Self": {
                "DNSName": "gpubox.corp-alpha.ts.net.",
                "TailscaleIPs": ["fd7a:115c:a1e0::42"],
            },
        }
        got = dns_controller.discovery_from_status(status)
        self.assertEqual(got.magic_nameserver, "fd7a:115c:a1e0::53")
        self.assertEqual(got.query_type, 28)
        self.assertEqual(got.expected_ips, frozenset({"fd7a:115c:a1e0::42"}))

    def test_rejects_missing_null_wrong_typed_or_unsafe_status_fields(self):
        mutations = [
            {},
            {**VALID_V4_STATUS, "BackendState": None},
            {**VALID_V4_STATUS, "BackendState": "Stopped"},
            {**VALID_V4_STATUS, "CurrentTailnet": None},
            {**VALID_V4_STATUS, "CurrentTailnet": {"MagicDNSEnabled": 1, "MagicDNSSuffix": "corp-alpha.ts.net"}},
            {**VALID_V4_STATUS, "CurrentTailnet": {"MagicDNSEnabled": False, "MagicDNSSuffix": "corp-alpha.ts.net"}},
            {**VALID_V4_STATUS, "Self": None},
            {**VALID_V4_STATUS, "Self": {"DNSName": "gpubox.corp-alpha.ts.net.", "TailscaleIPs": "100.64.0.42"}},
            {**VALID_V4_STATUS, "Self": {"DNSName": "gpubox.corp-alpha.ts.net.", "TailscaleIPs": []}},
            {**VALID_V4_STATUS, "Self": {"DNSName": "gpubox.corp-alpha.ts.net.", "TailscaleIPs": ["garbage"]}},
        ]
        for status in mutations:
            with self.subTest(status=status):
                with self.assertRaises(dns_controller.ControllerError):
                    dns_controller.discovery_from_status(status)

    def test_rejects_suffix_injection_and_non_descendant_self_names(self):
        suffixes = [
            "",
            ".",
            "ts.net evil",
            "ts.net\n.:53 {",
            "ts.net/other",
            "-bad.ts.net",
            "bad-.ts.net",
            "corp-alpha.ts.net.",
            f"{'a' * 64}.ts.net",
            f"{'a.' * 126}aa",
        ]
        for suffix in suffixes:
            status = {
                **VALID_V4_STATUS,
                "CurrentTailnet": {"MagicDNSEnabled": True, "MagicDNSSuffix": suffix},
            }
            with self.subTest(suffix=suffix):
                with self.assertRaises(dns_controller.ControllerError):
                    dns_controller.discovery_from_status(status)
        for self_name in [
            "corp-alpha.ts.net.",
            "evilcorp-alpha.ts.net.",
            "node.other.ts.net.",
            "gpubox.corp-alpha.ts.net",
        ]:
            status = {**VALID_V4_STATUS, "Self": {**VALID_V4_STATUS["Self"], "DNSName": self_name}}
            with self.subTest(self_name=self_name):
                with self.assertRaises(dns_controller.ControllerError):
                    dns_controller.discovery_from_status(status)


class LocalAPITests(unittest.TestCase):
    def test_fetches_status_over_unix_http_with_exact_request(self):
        body = b'{"BackendState":"Running"}'
        response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        with UnixHTTPServer(response) as server:
            got = dns_controller.fetch_localapi_status(server.path, timeout=1.0)
        self.assertEqual(got, {"BackendState": "Running"})
        self.assertEqual(
            server.requests,
            [b"GET /localapi/v0/status?peers=false HTTP/1.1\r\nHost: local-tailscaled.sock\r\nConnection: close\r\nAccept: application/json\r\n\r\n"],
        )

    def test_rejects_http_errors_invalid_json_and_wrong_json_root(self):
        responses = [
            b"HTTP/1.1 500 Error\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\n{",
            b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nnull",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n",
        ]
        for response in responses:
            with self.subTest(response=response[:40]):
                with UnixHTTPServer(response) as server:
                    with self.assertRaises(dns_controller.ControllerError):
                        dns_controller.fetch_localapi_status(server.path, timeout=1.0)

    def test_rejects_oversize_headers_and_body(self):
        huge_header = b"HTTP/1.1 200 OK\r\nX-Fill: " + b"x" * (dns_controller.MAX_HTTP_HEADERS + 1) + b"\r\n\r\n"
        huge_body_header = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(dns_controller.MAX_HTTP_BODY + 1).encode() + b"\r\n\r\n"
        for response in [huge_header, huge_body_header]:
            with UnixHTTPServer(response) as server:
                with self.assertRaises(dns_controller.ControllerError):
                    dns_controller.fetch_localapi_status(server.path, timeout=1.0)

    def test_accepts_bounded_chunked_and_connection_delimited_json(self):
        responses = [
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n7\r\n{\"ok\":1\r\n1\r\n}\r\n0\r\nX-Trace: yes\r\n\r\n",
            b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"ok\":1}",
        ]
        for response in responses:
            with self.subTest(response=response[:30]):
                with UnixHTTPServer(response) as server:
                    self.assertEqual(dns_controller.fetch_localapi_status(server.path), {"ok": 1})

    def test_deeply_nested_json_fails_closed(self):
        body = b"[" * 2000 + b"0" + b"]" * 2000
        response = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        with UnixHTTPServer(response) as server:
            with self.assertRaises(dns_controller.ControllerError):
                dns_controller.fetch_localapi_status(server.path)

    def test_rejects_non_hex_chunk_sizes(self):
        for size in [b"-1", b"+1", b"1_0", b"0x1"]:
            response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + size + b"\r\n{}\r\n0\r\n\r\n"
            with self.subTest(size=size):
                with UnixHTTPServer(response) as server:
                    with self.assertRaises(dns_controller.ControllerError):
                        dns_controller.fetch_localapi_status(server.path)

    def test_enforces_one_total_http_deadline(self):
        with UnixHTTPServer(b"", delay=0.25) as server:
            started = time.monotonic()
            with self.assertRaises(dns_controller.ControllerError):
                dns_controller.fetch_localapi_status(server.path, timeout=0.05)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.2)


class DNSProbeTests(unittest.TestCase):
    def discovery(self, address="100.64.0.42", qtype=1):
        return dns_controller.Discovery(
            suffix="corp-alpha.ts.net.",
            self_name="gpubox.corp-alpha.ts.net.",
            magic_nameserver="100.100.100.100",
            query_type=qtype,
            expected_ips=frozenset({address}),
        )

    def test_accepts_matching_a_and_aaaa_answers(self):
        for address, qtype in [("100.64.0.42", 1), ("fd7a:115c:a1e0::42", 28)]:
            with self.subTest(address=address):
                with DNSServer(lambda query, answer=address: answer_for(query, answer)) as server:
                    self.assertTrue(
                        dns_controller.probe_magicdns(
                            self.discovery(address, qtype),
                            timeout=1.0,
                            endpoint=("127.0.0.1", server.port),
                        )
                    )

    def test_rejects_nodata_mismatch_wrong_id_and_malformed_packets(self):
        behaviors = [
            lambda query: answer_for(query, None),
            lambda query: answer_for(query, "100.64.0.99"),
            lambda query: answer_for(query, "100.64.0.42", ident_delta=1),
            lambda _query: b"too short",
            lambda query: answer_for(query, "100.64.0.42") + b"trailing",
            lambda query: answer_for(query, "100.64.0.42")[:6] + struct.pack("!H", 2) + answer_for(query, "100.64.0.42")[8:],
        ]
        for behavior in behaviors:
            with self.subTest(behavior=behavior):
                with DNSServer(behavior) as server:
                    self.assertFalse(
                        dns_controller.probe_magicdns(
                            self.discovery(), timeout=1.0, endpoint=("127.0.0.1", server.port)
                        )
                    )

    def test_retries_truncated_udp_over_tcp_within_the_same_deadline(self):
        with DNSServer(
            lambda query: answer_for(query, None, truncated=True),
            lambda query: answer_for(query, "100.64.0.42"),
        ) as server:
            self.assertTrue(
                dns_controller.probe_magicdns(
                    self.discovery(), timeout=1.0, endpoint=("127.0.0.1", server.port)
                )
            )
            self.assertEqual([transport for transport, _ in server.requests], ["udp", "tcp"])

    def test_dual_stack_can_activate_from_second_family_match(self):
        discovery = dns_controller.Discovery(
            suffix="corp-alpha.ts.net.",
            self_name="gpubox.corp-alpha.ts.net.",
            magic_nameserver="100.100.100.100",
            query_type=1,
            expected_ips=frozenset({"100.64.0.42", "fd7a:115c:a1e0::42"}),
        )
        responses = iter(["100.64.0.99", "fd7a:115c:a1e0::42"])

        def respond(query):
            return answer_for(query, next(responses))

        with MultiQueryDNSServer(respond, count=2) as server:
            self.assertTrue(
                dns_controller.probe_magicdns(
                    discovery, timeout=1.0, endpoint=("127.0.0.1", server.port)
                )
            )
            self.assertEqual(len(server.requests), 2)

    def test_dual_stack_reserves_deadline_for_second_family(self):
        discovery = dns_controller.Discovery(
            suffix="corp-alpha.ts.net.",
            self_name="gpubox.corp-alpha.ts.net.",
            magic_nameserver="100.100.100.100",
            query_type=1,
            expected_ips=frozenset({"100.64.0.42", "fd7a:115c:a1e0::42"}),
        )

        def respond(query):
            query_type = struct.unpack("!H", question_from_query(query)[-4:-2])[0]
            return None if query_type == 1 else answer_for(query, "fd7a:115c:a1e0::42")

        with MultiQueryDNSServer(respond, count=2) as server:
            started = time.monotonic()
            self.assertTrue(
                dns_controller.probe_magicdns(
                    discovery, timeout=0.3, endpoint=("127.0.0.1", server.port)
                )
            )
            self.assertLess(time.monotonic() - started, 0.3)


class StateCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir()
        self.resolv = Path(self.temporary.name) / "resolv.conf"
        self.resolv.write_bytes(b"search svc.cluster.local cluster.local\nnameserver 10.96.0.10\noptions ndots:5\n")
        snapshot = dns_controller.initialize_state(self.state, self.resolv)
        dns_controller.publish_corefile(self.state, dns_controller.make_cluster_corefile(snapshot.nameservers))

    def tearDown(self):
        self.temporary.cleanup()

    def test_startup_requires_exact_generated_static_and_corefile_state(self):
        dns_controller.check_startup(self.state)
        (self.state / "Corefile").write_text(".:53 { log }\n")
        with self.assertRaises(dns_controller.ControllerError):
            dns_controller.check_startup(self.state)

    def test_startup_accepts_exact_generated_active_state_after_controller_restart(self):
        discovery = dns_controller.discovery_from_status(VALID_V4_STATUS)
        dns_controller.publish_corefile(
            self.state,
            dns_controller.make_active_corefile(("10.96.0.10",), discovery),
        )
        dns_controller.check_startup(self.state)

    def test_liveness_requires_fresh_monotonic_heartbeat_and_file_timestamp(self):
        dns_controller.write_heartbeat(self.state)
        dns_controller.check_live(self.state)
        (self.state / "heartbeat").write_text(str(time.monotonic_ns() - 60_000_000_000) + "\n")
        with self.assertRaises(dns_controller.ControllerError):
            dns_controller.check_live(self.state)
        dns_controller.write_heartbeat(self.state)
        old = time.time() - 60
        os.utime(self.state / "heartbeat", (old, old))
        with self.assertRaises(dns_controller.ControllerError):
            dns_controller.check_live(self.state)

    def test_cli_checks_return_zero_for_valid_state_and_one_for_stale_live_state(self):
        dns_controller.write_heartbeat(self.state)
        base = ["python3", "-I", "-B", str(CONTROLLER_PATH)]
        startup = subprocess.run(
            base + ["check-startup", "--state-dir", str(self.state)],
            check=False,
            capture_output=True,
        )
        live = subprocess.run(
            base + ["check-live", "--state-dir", str(self.state)],
            check=False,
            capture_output=True,
        )
        self.assertEqual(startup.returncode, 0)
        self.assertEqual(live.returncode, 0)
        (self.state / "heartbeat").write_text("0\n")
        stale = subprocess.run(
            base + ["check-live", "--state-dir", str(self.state)],
            check=False,
            capture_output=True,
        )
        self.assertEqual(stale.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
