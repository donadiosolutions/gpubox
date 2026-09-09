#!/usr/bin/env python3
"""Small UDP/TCP DNS upstream used by the managed-DNS runtime tests."""

from __future__ import annotations

import argparse
import json
import signal
import socket
import struct
import threading
import time


def decode_question(packet: bytes) -> tuple[str, int, int]:
    offset = 12
    labels: list[str] = []
    while packet[offset]:
        size = packet[offset]
        offset += 1
        labels.append(packet[offset : offset + size].decode("ascii"))
        offset += size
    offset += 1
    qtype, _qclass = struct.unpack("!HH", packet[offset : offset + 4])
    return ".".join(labels) + ".", qtype, offset + 4


def make_response(packet: bytes, transport: str, address: str, role: str) -> bytes:
    qname, qtype, question_end = decode_question(packet)
    lower_name = qname.lower()
    rd = struct.unpack("!H", packet[2:4])[0] & 0x0100
    question = packet[12:question_end]

    rcode = 0
    if role == "magic" and lower_name.startswith("servfail."):
        rcode = 2
    elif role == "magic" and lower_name.startswith("nxdomain."):
        rcode = 3
    elif role == "magic" and lower_name.startswith("refused."):
        rcode = 5
    if role == "magic" and lower_name.startswith("truncated.") and transport == "udp":
        return packet[:2] + struct.pack("!HHHHH", 0x8280 | rd, 1, 0, 0, 0) + question
    if rcode or (role == "magic" and lower_name.startswith("nodata.")) or qtype != 1:
        return packet[:2] + struct.pack(
            "!HHHHH", 0x8080 | rd | rcode, 1, 0, 0, 0
        ) + question

    answer = (
        b"\xc0\x0c"
        + struct.pack("!HHIH", 1, 1, 30, 4)
        + socket.inet_aton(address)
    )
    return packet[:2] + struct.pack("!HHHHH", 0x8080 | rd, 1, 1, 0, 0) + question + answer


class Server:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stopping = threading.Event()
        self.log = open(args.log, "a", encoding="utf-8", buffering=1)

    def record(self, packet: bytes, transport: str) -> None:
        qname, qtype, _ = decode_question(packet)
        self.log.write(
            json.dumps(
                {
                    "time": time.time(),
                    "role": self.args.role,
                    "transport": transport,
                    "qname": qname,
                    "qtype": qtype,
                },
                sort_keys=True,
            )
            + "\n"
        )

    def serve_udp(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.args.host, self.args.port))
            listener.settimeout(0.2)
            while not self.stopping.is_set():
                try:
                    packet, peer = listener.recvfrom(65535)
                except TimeoutError:
                    continue
                self.record(packet, "udp")
                listener.sendto(
                    make_response(packet, "udp", self.args.address, self.args.role), peer
                )

    def serve_tcp_connection(self, connection: socket.socket) -> None:
        with connection:
            prefix = connection.recv(2)
            if len(prefix) != 2:
                return
            size = struct.unpack("!H", prefix)[0]
            packet = b""
            while len(packet) < size:
                chunk = connection.recv(size - len(packet))
                if not chunk:
                    return
                packet += chunk
            self.record(packet, "tcp")
            answer = make_response(packet, "tcp", self.args.address, self.args.role)
            connection.sendall(struct.pack("!H", len(answer)) + answer)

    def serve_tcp(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.args.host, self.args.port))
            listener.listen()
            listener.settimeout(0.2)
            while not self.stopping.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                threading.Thread(
                    target=self.serve_tcp_connection,
                    args=(connection,),
                    daemon=True,
                ).start()

    def run(self) -> None:
        threads = [
            threading.Thread(target=self.serve_udp),
            threading.Thread(target=self.serve_tcp),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=53)
    parser.add_argument("--address", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    server = Server(args)
    signal.signal(signal.SIGTERM, lambda *_: server.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: server.stopping.set())
    try:
        server.run()
    finally:
        server.log.close()


if __name__ == "__main__":
    main()
