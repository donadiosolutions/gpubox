#!/usr/bin/env python3
"""Fail-closed pod-local split DNS controller for gpubox."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import sys
import tempfile
import time
from typing import Any


MAGICDNS_V4 = "100.100.100.100"
MAGICDNS_V6 = "fd7a:115c:a1e0::53"
LISTENER_IP = "127.0.0.1"
POLL_SECONDS = 5.0
REQUEST_SECONDS = 1.0
LIVE_MAX_AGE_SECONDS = 15.0
MAX_RESOLV_CONF = 64 * 1024
MAX_STATE_FILE = 1024 * 1024
MAX_HTTP_HEADERS = 16 * 1024
MAX_HTTP_BODY = 1024 * 1024
MAX_DNS_PACKET = 4096
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_ACTIVE_CORE_RE = re.compile(
    rb"(?P<suffix>[A-Za-z0-9.-]+):53 \{\n"
    rb"    bind 127\.0\.0\.1\n"
    rb"    errors\n"
    rb"    forward \. (?P<magic>100\.100\.100\.100|\[fd7a:115c:a1e0::53\]:53) (?P<upstreams>[^\n]+) \{\n"
    rb"        policy sequential\n"
    rb"        max_fails 1\n"
    rb"        health_check 1s domain (?P<self>[A-Za-z0-9.-]+\.)\n"
    rb"        failover SERVFAIL REFUSED\n"
    rb"    \}\n"
    rb"\}\n\n",
)


class ControllerError(RuntimeError):
    """An expected fail-closed controller error."""


@dataclass(frozen=True)
class ResolverSnapshot:
    raw: bytes
    nameservers: tuple[str, ...]


@dataclass(frozen=True)
class Discovery:
    suffix: str
    self_name: str
    magic_nameserver: str
    query_type: int
    expected_ips: frozenset[str]


def _log(message: str) -> None:
    print(f"dns-controller: {message}", file=sys.stderr, flush=True)


def _usable_ip(value: str, *, forbid_magic: bool) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ControllerError("invalid IP address") from error
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
        raise ControllerError("scoped IPv6 addresses are unsupported")
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    if (
        address.is_unspecified
        or address.is_multicast
        or address.is_loopback
        or (mapped is not None and (mapped.is_loopback or mapped.compressed == MAGICDNS_V4))
    ):
        raise ControllerError("unusable IP address")
    normalized = address.compressed
    if forbid_magic and normalized in {MAGICDNS_V4, MAGICDNS_V6}:
        raise ControllerError("recursive MagicDNS nameserver")
    if normalized == "255.255.255.255":
        raise ControllerError("unusable IP address")
    return normalized


def parse_resolv_conf(raw: bytes) -> ResolverSnapshot:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RESOLV_CONF or b"\0" in raw:
        raise ControllerError("invalid resolver snapshot")
    nameservers: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        fields = stripped.split()
        if fields[0] != b"nameserver":
            continue
        if len(fields) != 2:
            raise ControllerError("invalid nameserver directive")
        try:
            literal = fields[1].decode("ascii")
        except UnicodeDecodeError as error:
            raise ControllerError("invalid nameserver directive") from error
        nameservers.append(_usable_ip(literal, forbid_magic=True))
    if not nameservers:
        raise ControllerError("resolver snapshot has no nameservers")
    return ResolverSnapshot(raw=raw, nameservers=tuple(nameservers))


def make_client_resolv_conf(raw: bytes) -> bytes:
    parse_resolv_conf(raw)
    output: list[bytes] = []
    inserted = False
    for line in raw.splitlines(keepends=True):
        fields = line.strip().split()
        if fields and fields[0] == b"nameserver":
            if not inserted:
                ending = b"\r\n" if line.endswith(b"\r\n") else (b"\n" if line.endswith(b"\n") else b"")
                output.append(b"nameserver " + LISTENER_IP.encode("ascii") + ending)
                inserted = True
            continue
        output.append(line)
    if not inserted:
        raise ControllerError("resolver snapshot has no nameservers")
    return b"".join(output)


def _require_state_dir(state_dir: Path) -> Path:
    path = Path(state_dir)
    try:
        info = path.lstat()
    except OSError as error:
        raise ControllerError("state directory is unavailable") from error
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ControllerError("invalid state directory")
    return path


def _read_regular(path: Path, maximum: int = MAX_STATE_FILE) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > maximum:
            raise ControllerError(f"invalid state file {path.name}")
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
    except ControllerError:
        raise
    except OSError as error:
        raise ControllerError(f"cannot read state file {path.name}") from error
    if len(data) > maximum:
        raise ControllerError(f"oversize state file {path.name}")
    return data


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_once(path: Path, content: bytes) -> bool:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.", dir=path.parent, delete=False) as stream:
            temporary_name = stream.name
            os.fchmod(stream.fileno(), 0o644)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            return False
        os.unlink(temporary_name)
        temporary_name = None
        _fsync_directory(path.parent)
        return True
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def initialize_state(state_dir: Path, resolv_conf: Path) -> ResolverSnapshot:
    state = _require_state_dir(Path(state_dir))
    original_path = state / "original-resolv.conf"
    if original_path.exists() or original_path.is_symlink():
        original = _read_regular(original_path, MAX_RESOLV_CONF)
    else:
        try:
            with Path(resolv_conf).open("rb") as stream:
                original = stream.read(MAX_RESOLV_CONF + 1)
        except OSError as error:
            raise ControllerError("cannot read original resolver") from error
        parse_resolv_conf(original)
        if not _create_once(original_path, original):
            original = _read_regular(original_path, MAX_RESOLV_CONF)
    snapshot = parse_resolv_conf(original)
    client = make_client_resolv_conf(snapshot.raw)
    client_path = state / "client-resolv.conf"
    if client_path.exists() or client_path.is_symlink():
        if _read_regular(client_path, MAX_RESOLV_CONF) != client:
            raise ControllerError("retained client resolver does not match original")
    elif not _create_once(client_path, client):
        if _read_regular(client_path, MAX_RESOLV_CONF) != client:
            raise ControllerError("retained client resolver does not match original")
    return snapshot


def _upstream(address: str) -> str:
    parsed = ipaddress.ip_address(address)
    return address if parsed.version == 4 else f"[{address}]:53"


def make_cluster_corefile(nameservers: tuple[str, ...]) -> bytes:
    if not nameservers:
        raise ControllerError("no cluster nameservers")
    upstreams = " ".join(_upstream(_usable_ip(item, forbid_magic=True)) for item in nameservers)
    return f""".:53 {{
    bind {LISTENER_IP}
    errors
    health :8080
    ready :8181 {{
        monitor continuously
    }}
    reload 2s 1s
    forward . {upstreams} {{
        policy sequential
        max_fails 1
        health_check 1s
    }}
}}
""".encode("ascii")


def _validated_dns_name(value: str, *, absolute: bool) -> str:
    if type(value) is not str or not value or not value.isascii():
        raise ControllerError("invalid DNS name")
    if absolute:
        if not value.endswith(".") or value.endswith(".."):
            raise ControllerError("DNS name must be absolute")
        body = value[:-1]
    else:
        if value.startswith(".") or value.endswith("."):
            raise ControllerError("DNS suffix must not contain surrounding dots")
        body = value
    if not body or len(body.encode("ascii")) > 253:
        raise ControllerError("invalid DNS name length")
    labels = body.split(".")
    if any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise ControllerError("invalid DNS label")
    return body.lower() + "."


def make_active_corefile(nameservers: tuple[str, ...], discovery: Discovery) -> bytes:
    suffix = _validated_dns_name(discovery.suffix, absolute=True)
    self_name = _validated_dns_name(discovery.self_name, absolute=True)
    if not self_name.endswith("." + suffix) or self_name == suffix:
        raise ControllerError("self DNS name is outside MagicDNS suffix")
    magic = _usable_ip(discovery.magic_nameserver, forbid_magic=False)
    if magic not in {MAGICDNS_V4, MAGICDNS_V6}:
        raise ControllerError("invalid MagicDNS service address")
    cluster = " ".join(_upstream(_usable_ip(item, forbid_magic=True)) for item in nameservers)
    zone = suffix[:-1]
    return f"""{zone}:53 {{
    bind {LISTENER_IP}
    errors
    forward . {_upstream(magic)} {cluster} {{
        policy sequential
        max_fails 1
        health_check 1s domain {self_name}
        failover SERVFAIL REFUSED
    }}
}}

""".encode("ascii") + make_cluster_corefile(nameservers)


def _atomic_replace(path: Path, content: bytes) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.", dir=path.parent, delete=False) as stream:
            temporary_name = stream.name
            os.fchmod(stream.fileno(), 0o644)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        _fsync_directory(path.parent)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def publish_corefile(state_dir: Path, content: bytes) -> bool:
    state = _require_state_dir(Path(state_dir))
    if not isinstance(content, bytes) or not content or len(content) > MAX_STATE_FILE:
        raise ControllerError("invalid Corefile content")
    path = state / "Corefile"
    if path.exists() or path.is_symlink():
        if _read_regular(path) == content:
            return False
    try:
        _atomic_replace(path, content)
    except OSError as error:
        raise ControllerError("cannot publish Corefile") from error
    return True


def discovery_from_status(status_value: Any) -> Discovery:
    if type(status_value) is not dict:
        raise ControllerError("LocalAPI status root is not an object")
    if type(status_value.get("BackendState")) is not str or status_value["BackendState"] != "Running":
        raise ControllerError("Tailscale backend is not running")
    tailnet = status_value.get("CurrentTailnet")
    if type(tailnet) is not dict:
        raise ControllerError("CurrentTailnet is not an object")
    if type(tailnet.get("MagicDNSEnabled")) is not bool or tailnet["MagicDNSEnabled"] is not True:
        raise ControllerError("MagicDNS is not enabled")
    suffix = _validated_dns_name(tailnet.get("MagicDNSSuffix"), absolute=False)
    self_status = status_value.get("Self")
    if type(self_status) is not dict:
        raise ControllerError("Self is not an object")
    self_name = _validated_dns_name(self_status.get("DNSName"), absolute=True)
    if self_name == suffix or not self_name.endswith("." + suffix):
        raise ControllerError("self DNS name is outside MagicDNS suffix")
    raw_ips = self_status.get("TailscaleIPs")
    if type(raw_ips) is not list or not raw_ips:
        raise ControllerError("Self.TailscaleIPs is not a nonempty array")
    normalized: set[str] = set()
    for raw_ip in raw_ips:
        if type(raw_ip) is not str:
            raise ControllerError("invalid Self.TailscaleIPs item")
        normalized.add(_usable_ip(raw_ip, forbid_magic=True))
    has_v4 = any(ipaddress.ip_address(item).version == 4 for item in normalized)
    return Discovery(
        suffix,
        self_name,
        MAGICDNS_V4 if has_v4 else MAGICDNS_V6,
        1 if has_v4 else 28,
        frozenset(normalized),
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ControllerError("operation timed out")
    return remaining


def fetch_localapi_status(socket_path: str | Path, *, timeout: float = REQUEST_SECONDS) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    request = (
        b"GET /localapi/v0/status?peers=false HTTP/1.1\r\n"
        b"Host: local-tailscaled.sock\r\n"
        b"Connection: close\r\n"
        b"Accept: application/json\r\n\r\n"
    )
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(_remaining(deadline))
        connection.connect(os.fspath(socket_path))
        connection.settimeout(_remaining(deadline))
        connection.sendall(request)
        received = b""
        while b"\r\n\r\n" not in received:
            connection.settimeout(_remaining(deadline))
            chunk = connection.recv(4096)
            if not chunk:
                raise ControllerError("incomplete LocalAPI HTTP headers")
            received += chunk
            if b"\r\n\r\n" not in received and len(received) > MAX_HTTP_HEADERS:
                raise ControllerError("oversize LocalAPI HTTP headers")
        header_bytes, body = received.split(b"\r\n\r\n", 1)
        if len(header_bytes) + 4 > MAX_HTTP_HEADERS:
            raise ControllerError("oversize LocalAPI HTTP headers")
        lines = header_bytes.decode("latin-1").split("\r\n")
        status_fields = lines[0].split(" ", 2)
        if len(status_fields) < 2 or status_fields[0] not in {"HTTP/1.0", "HTTP/1.1"} or status_fields[1] != "200":
            raise ControllerError("LocalAPI returned non-200 HTTP status")
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if not line or line[0].isspace() or ":" not in line:
                raise ControllerError("invalid LocalAPI HTTP header")
            name, value = line.split(":", 1)
            if not name or any(character.isspace() for character in name):
                raise ControllerError("invalid LocalAPI HTTP header")
            headers.setdefault(name.lower(), []).append(value.strip())
        transfers = headers.get("transfer-encoding", [])
        lengths = headers.get("content-length", [])
        if transfers:
            if transfers != ["chunked"] or lengths:
                raise ControllerError("unsupported LocalAPI transfer encoding")
            body = _read_chunked_body(connection, body, deadline)
        elif lengths:
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise ControllerError("invalid LocalAPI content length")
            length = int(lengths[0])
            if length > MAX_HTTP_BODY:
                raise ControllerError("oversize LocalAPI HTTP body")
            if len(body) > length:
                raise ControllerError("LocalAPI HTTP body exceeds content length")
            while len(body) < length:
                connection.settimeout(_remaining(deadline))
                chunk = connection.recv(min(4096, length - len(body)))
                if not chunk:
                    raise ControllerError("incomplete LocalAPI HTTP body")
                body += chunk
        else:
            while True:
                if len(body) > MAX_HTTP_BODY:
                    raise ControllerError("oversize LocalAPI HTTP body")
                connection.settimeout(_remaining(deadline))
                chunk = connection.recv(4096)
                if not chunk:
                    break
                body += chunk
    except ControllerError:
        raise
    except (OSError, TimeoutError, UnicodeError) as error:
        raise ControllerError("LocalAPI request failed") from error
    finally:
        connection.close()
    try:
        decoded = json.loads(
            body.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON constant")),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError) as error:
        raise ControllerError("invalid LocalAPI JSON") from error
    if type(decoded) is not dict:
        raise ControllerError("LocalAPI status root is not an object")
    return decoded


def _read_chunked_body(connection: socket.socket, initial: bytes, deadline: float) -> bytes:
    pending = initial
    output = bytearray()

    def receive_line() -> bytes:
        nonlocal pending
        while b"\r\n" not in pending:
            if len(pending) > MAX_HTTP_HEADERS:
                raise ControllerError("oversize LocalAPI chunk metadata")
            connection.settimeout(_remaining(deadline))
            part = connection.recv(4096)
            if not part:
                raise ControllerError("incomplete LocalAPI chunked body")
            pending += part
        line, pending = pending.split(b"\r\n", 1)
        return line

    def receive_exact(size: int) -> bytes:
        nonlocal pending
        while len(pending) < size:
            connection.settimeout(_remaining(deadline))
            part = connection.recv(min(4096, size - len(pending)))
            if not part:
                raise ControllerError("incomplete LocalAPI chunked body")
            pending += part
        result, pending = pending[:size], pending[size:]
        return result

    while True:
        size_text = receive_line().split(b";", 1)[0]
        if not size_text or len(size_text) > 16 or re.fullmatch(rb"[0-9A-Fa-f]+", size_text) is None:
            raise ControllerError("invalid LocalAPI chunk size")
        try:
            size = int(size_text, 16)
        except ValueError as error:
            raise ControllerError("invalid LocalAPI chunk size") from error
        if size == 0:
            trailer_bytes = 0
            while True:
                trailer = receive_line()
                trailer_bytes += len(trailer) + 2
                if trailer_bytes > MAX_HTTP_HEADERS:
                    raise ControllerError("oversize LocalAPI chunk trailers")
                if not trailer:
                    return bytes(output)
                if trailer[0:1].isspace() or b":" not in trailer:
                    raise ControllerError("invalid LocalAPI chunk trailer")
        if len(output) + size > MAX_HTTP_BODY:
            raise ControllerError("oversize LocalAPI HTTP body")
        output.extend(receive_exact(size))
        if receive_exact(2) != b"\r\n":
            raise ControllerError("invalid LocalAPI chunk framing")


def _encode_dns_name(name: str) -> bytes:
    absolute = _validated_dns_name(name, absolute=True)
    return b"".join(bytes((len(label),)) + label.encode("ascii") for label in absolute[:-1].split(".")) + b"\0"


def _decode_dns_name(packet: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    next_offset: int | None = None
    visited: set[int] = set()
    while True:
        if offset >= len(packet):
            raise ControllerError("truncated DNS name")
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ControllerError("truncated DNS compression pointer")
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            if pointer >= len(packet) or pointer in visited:
                raise ControllerError("invalid DNS compression pointer")
            visited.add(pointer)
            if next_offset is None:
                next_offset = offset + 2
            offset = pointer
            continue
        if length & 0xC0:
            raise ControllerError("invalid DNS label length")
        offset += 1
        if length == 0:
            end = next_offset if next_offset is not None else offset
            if not labels:
                return ".", end
            return _validated_dns_name(".".join(labels) + ".", absolute=True), end
        if length > 63 or offset + length > len(packet):
            raise ControllerError("truncated DNS label")
        try:
            label = packet[offset : offset + length].decode("ascii")
        except UnicodeDecodeError as error:
            raise ControllerError("non-ASCII DNS label") from error
        if not _DNS_LABEL.fullmatch(label):
            raise ControllerError("invalid DNS label")
        labels.append(label.lower())
        if sum(len(item) + 1 for item in labels) > 254:
            raise ControllerError("oversize DNS name")
        offset += length


def _parse_dns_answer(packet: bytes, ident: int, name: str, query_type: int) -> tuple[bool, frozenset[str]]:
    if len(packet) < 12 or len(packet) > MAX_DNS_PACKET:
        raise ControllerError("invalid DNS packet size")
    response_id, flags, qdcount, ancount, nscount, arcount = struct.unpack("!HHHHHH", packet[:12])
    if response_id != ident or flags & 0x8000 == 0 or flags & 0x7800 or qdcount != 1:
        raise ControllerError("invalid DNS response header")
    offset = 12
    question_name, offset = _decode_dns_name(packet, offset)
    if offset + 4 > len(packet):
        raise ControllerError("truncated DNS question")
    response_type, response_class = struct.unpack("!HH", packet[offset : offset + 4])
    offset += 4
    if question_name != name or response_type != query_type or response_class != 1:
        raise ControllerError("DNS response question mismatch")
    truncated = bool(flags & 0x0200)
    if truncated:
        return True, frozenset()
    if flags & 0x000F:
        return False, frozenset()
    records: list[tuple[str, int, int, bytes, int]] = []
    for index in range(ancount + nscount + arcount):
        owner, offset = _decode_dns_name(packet, offset)
        if offset + 10 > len(packet):
            raise ControllerError("truncated DNS answer")
        record_type, record_class, _ttl, data_length = struct.unpack("!HHIH", packet[offset : offset + 10])
        offset += 10
        data_offset = offset
        if offset + data_length > len(packet):
            raise ControllerError("truncated DNS answer data")
        data = packet[offset : offset + data_length]
        offset += data_length
        if index < ancount:
            records.append((owner, record_type, record_class, data, data_offset))
    if offset != len(packet):
        raise ControllerError("trailing data in DNS response")
    reachable = {name}
    changed = True
    while changed:
        changed = False
        for owner, record_type, record_class, data, data_offset in records:
            if owner not in reachable or record_type != 5 or record_class != 1:
                continue
            target, target_end = _decode_dns_name(packet, data_offset)
            if target_end != data_offset + len(data):
                raise ControllerError("invalid CNAME answer")
            if target not in reachable:
                reachable.add(target)
                changed = True
    addresses: set[str] = set()
    for owner, record_type, record_class, data, _data_offset in records:
        if owner not in reachable or record_type != query_type or record_class != 1:
            continue
        expected_length = 4 if query_type == 1 else 16
        if len(data) != expected_length:
            raise ControllerError("invalid DNS address answer")
        addresses.add(ipaddress.ip_address(data).compressed)
    return False, frozenset(addresses)


def _recv_exact(connection: socket.socket, size: int, deadline: float) -> bytes:
    data = b""
    while len(data) < size:
        connection.settimeout(_remaining(deadline))
        part = connection.recv(size - len(data))
        if not part:
            raise ControllerError("truncated TCP DNS response")
        data += part
    return data


def _probe_one(discovery: Discovery, query_type: int, expected: frozenset[str], endpoint: tuple[str, int], deadline: float) -> bool:
    ident = secrets.randbelow(65536)
    query = struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0) + _encode_dns_name(discovery.self_name) + struct.pack("!HH", query_type, 1)
    family = socket.AF_INET6 if ipaddress.ip_address(endpoint[0]).version == 6 else socket.AF_INET
    udp = socket.socket(family, socket.SOCK_DGRAM)
    try:
        udp.settimeout(_remaining(deadline))
        udp.connect(endpoint)
        udp.send(query)
        udp.settimeout(_remaining(deadline))
        packet = udp.recv(MAX_DNS_PACKET + 1)
    finally:
        udp.close()
    truncated, addresses = _parse_dns_answer(packet, ident, discovery.self_name, query_type)
    if not truncated:
        return bool(addresses & expected)
    tcp = socket.socket(family, socket.SOCK_STREAM)
    try:
        tcp.settimeout(_remaining(deadline))
        tcp.connect(endpoint)
        tcp.settimeout(_remaining(deadline))
        tcp.sendall(struct.pack("!H", len(query)) + query)
        size = struct.unpack("!H", _recv_exact(tcp, 2, deadline))[0]
        if size > MAX_DNS_PACKET:
            raise ControllerError("oversize TCP DNS response")
        packet = _recv_exact(tcp, size, deadline)
    finally:
        tcp.close()
    second_truncated, addresses = _parse_dns_answer(packet, ident, discovery.self_name, query_type)
    return not second_truncated and bool(addresses & expected)


def probe_magicdns(discovery: Discovery, *, timeout: float = REQUEST_SECONDS, endpoint: tuple[str, int] | None = None) -> bool:
    target = endpoint or (discovery.magic_nameserver, 53)
    deadline = time.monotonic() + timeout
    by_type = {
        1: frozenset(item for item in discovery.expected_ips if ipaddress.ip_address(item).version == 4),
        28: frozenset(item for item in discovery.expected_ips if ipaddress.ip_address(item).version == 6),
    }
    ordered_types = [discovery.query_type, 28 if discovery.query_type == 1 else 1]
    query_types = [query_type for query_type in ordered_types if by_type[query_type]]
    for index, query_type in enumerate(query_types):
        expected = by_type[query_type]
        try:
            probes_left = len(query_types) - index
            probe_deadline = deadline
            if probes_left > 1:
                probe_deadline = time.monotonic() + _remaining(deadline) / probes_left
            if _probe_one(discovery, query_type, expected, target, probe_deadline):
                return True
        except (ControllerError, OSError, TimeoutError):
            continue
    return False


def _validate_corefile(content: bytes, nameservers: tuple[str, ...]) -> None:
    if content == make_cluster_corefile(nameservers):
        return
    match = _ACTIVE_CORE_RE.match(content)
    if match is None:
        raise ControllerError("Corefile was not generated by this controller")
    magic_text = match.group("magic").decode("ascii")
    magic = MAGICDNS_V6 if magic_text.startswith("[") else MAGICDNS_V4
    suffix = _validated_dns_name(match.group("suffix").decode("ascii"), absolute=False)
    self_name = _validated_dns_name(match.group("self").decode("ascii"), absolute=True)
    discovery = Discovery(suffix, self_name, magic, 28 if magic == MAGICDNS_V6 else 1, frozenset())
    if make_active_corefile(nameservers, discovery) != content:
        raise ControllerError("Corefile was not generated by this controller")


def check_startup(state_dir: Path) -> None:
    state = _require_state_dir(Path(state_dir))
    snapshot = parse_resolv_conf(_read_regular(state / "original-resolv.conf", MAX_RESOLV_CONF))
    if _read_regular(state / "client-resolv.conf", MAX_RESOLV_CONF) != make_client_resolv_conf(snapshot.raw):
        raise ControllerError("client resolver does not match original")
    _validate_corefile(_read_regular(state / "Corefile"), snapshot.nameservers)


def write_heartbeat(state_dir: Path) -> None:
    state = _require_state_dir(Path(state_dir))
    try:
        _atomic_replace(state / "heartbeat", f"{time.monotonic_ns()}\n".encode("ascii"))
    except OSError as error:
        raise ControllerError("cannot write heartbeat") from error


def check_live(state_dir: Path) -> None:
    check_startup(state_dir)
    heartbeat_path = Path(state_dir) / "heartbeat"
    raw = _read_regular(heartbeat_path, 64)
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ControllerError("invalid heartbeat") from error
    if value <= 0:
        raise ControllerError("invalid heartbeat")
    monotonic_age = (time.monotonic_ns() - value) / 1_000_000_000
    try:
        wall_age = time.time() - heartbeat_path.stat().st_mtime
    except OSError as error:
        raise ControllerError("cannot stat heartbeat") from error
    if monotonic_age < -1 or wall_age < -1 or monotonic_age > LIVE_MAX_AGE_SECONDS or wall_age > LIVE_MAX_AGE_SECONDS:
        raise ControllerError("stale heartbeat")


def run_controller(state_dir: Path, resolv_conf: Path, tailscale_socket: Path, magicdns_enabled: bool) -> None:
    snapshot = initialize_state(state_dir, resolv_conf)
    cluster_corefile = make_cluster_corefile(snapshot.nameservers)
    publish_corefile(state_dir, cluster_corefile)
    write_heartbeat(state_dir)
    _log("cluster-only Corefile written; CoreDNS reload is asynchronous")
    mode = "cluster-only"
    last_error: str | None = None
    while True:
        started = time.monotonic()
        desired = cluster_corefile
        desired_mode = "cluster-only"
        error_message: str | None = None
        if magicdns_enabled:
            try:
                discovery = discovery_from_status(fetch_localapi_status(tailscale_socket))
                if not probe_magicdns(discovery):
                    raise ControllerError("MagicDNS self-name probe failed")
                desired = make_active_corefile(snapshot.nameservers, discovery)
                desired_mode = f"MagicDNS suffix {discovery.suffix}"
            except ControllerError as error:
                error_message = str(error)
        try:
            changed = publish_corefile(state_dir, desired)
            if desired_mode != mode or changed:
                _log(f"{desired_mode} Corefile written; CoreDNS reload is asynchronous")
                mode = desired_mode
            if error_message is not None and error_message != last_error:
                _log(f"discovery unavailable: {error_message}; using cluster-only routing")
            last_error = error_message
        finally:
            write_heartbeat(state_dir)
        time.sleep(max(0.0, POLL_SECONDS - (time.monotonic() - started)))


def _parse_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "check-startup", "check-live"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--state-dir", type=Path, default=Path("/var/run/gpubox-dns"))
        subparser.add_argument("--resolv-conf", type=Path, default=Path("/etc/resolv.conf"))
        subparser.add_argument("--tailscale-socket", type=Path, default=Path("/var/run/gpubox-tailscale/tailscaled.sock"))
        subparser.add_argument("--magicdns-enabled", type=_parse_bool, default=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "run":
            run_controller(arguments.state_dir, arguments.resolv_conf, arguments.tailscale_socket, arguments.magicdns_enabled)
        elif arguments.command == "check-startup":
            check_startup(arguments.state_dir)
        else:
            check_live(arguments.state_dir)
    except KeyboardInterrupt:
        return 0
    except ControllerError as error:
        _log(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
