#!/usr/bin/env python3
"""Mutable Tailscale LocalAPI status fixture over a real Unix socket."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import socketserver
import threading
import time


class LocalAPIHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request_line = self.rfile.readline(8192).decode("ascii", "replace").rstrip()
        headers: dict[str, str] = {}
        while True:
            line = self.rfile.readline(8192)
            if line in (b"", b"\r\n", b"\n"):
                break
            name, _, value = line.decode("ascii", "replace").partition(":")
            headers[name.lower()] = value.strip()
        fixture = self.server.fixture  # type: ignore[attr-defined]
        fixture.record(request_line, headers)
        try:
            body = fixture.status_path.read_bytes()
        except FileNotFoundError:
            body = b"{}"
        response = (
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\nConnection: close\r\n\r\n"
            + body
        )
        self.wfile.write(response)


class LocalAPIServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class Fixture:
    def __init__(self, socket_path: pathlib.Path, status_path: pathlib.Path, log_path: pathlib.Path) -> None:
        self.socket_path = socket_path
        self.status_path = status_path
        self.log_path = log_path
        self.server: LocalAPIServer | None = None

    def record(self, request_line: str, headers: dict[str, str]) -> None:
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "time": time.time(),
                        "request": request_line,
                        "host": headers.get("host", ""),
                    },
                    sort_keys=True,
                )
                + "\n"
            )

    def stop(self, *_args: object) -> None:
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def run(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.server = LocalAPIServer(str(self.socket_path), LocalAPIHandler)
        self.server.fixture = self  # type: ignore[attr-defined]
        os.chmod(self.socket_path, 0o666)
        try:
            self.server.serve_forever(poll_interval=0.1)
        finally:
            self.server.server_close()
            self.socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=pathlib.Path, required=True)
    parser.add_argument("--status", type=pathlib.Path, required=True)
    parser.add_argument("--log", type=pathlib.Path, required=True)
    args = parser.parse_args()
    fixture = Fixture(args.socket, args.status, args.log)
    signal.signal(signal.SIGTERM, fixture.stop)
    signal.signal(signal.SIGINT, fixture.stop)
    fixture.run()


if __name__ == "__main__":
    main()
