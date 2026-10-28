"""Fake Source RCON server for tests and dev. Standard library only.

Speaks the protocol basecamp/rcon.py does (docs/02-architecture.md): login (type 3,
answered by type 2 with the id, or -1 for a wrong password), commands (type 2, answered
by type 0). What it answers:

    echo <text>      <text>
    big <n>          n characters of "x", split over 4096-byte packets like a real server
    anything else    "ok: <command>"

``mode`` makes it misbehave the way real servers do:

    "valve"   the default: answers carry the command's id
    "id0"     every answer has id 0, like Palworld
    "slow"    each answer comes ``delay`` seconds late
    "drop"    closes the connection after each answered command (a server restart, an
              idle timeout), so the next command finds it dead
    "silent"  logs in, then never answers a command

Every command it gets is in ``commands``. Run it by hand:

    python tools/fake_rcon.py [--port 25575] [--password test] [--mode valve]
"""

from __future__ import annotations

import argparse
import socket
import socketserver
import struct
import sys
import threading
import time

AUTH, AUTH_RESPONSE, EXEC, RESPONSE = 3, 2, 2, 0
SPLIT = 4096


def packet(packet_id: int, kind: int, body: str) -> bytes:
    data = body.encode("utf-8")
    return struct.pack("<iii", len(data) + 10, packet_id, kind) + data + b"\0\0"


class FakeRcon:
    def __init__(self, password: str = "test", mode: str = "valve", port: int = 0,
                 delay: float = 2.0, echo: bool = False) -> None:
        self.password = password
        self.mode = mode
        self.delay = delay
        self.commands: list[str] = []
        self.logins = 0
        self.lock = threading.Lock()
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                sock: socket.socket = self.request
                authed = False
                while True:
                    got = _read(sock)
                    if got is None:
                        return
                    packet_id, kind, body = got
                    if kind == AUTH:
                        with fake.lock:
                            fake.logins += 1
                        authed = body == fake.password
                        sock.sendall(packet(packet_id, RESPONSE, ""))  # as Source servers do
                        sock.sendall(packet(packet_id if authed else -1, AUTH_RESPONSE, ""))
                        continue
                    if not authed:
                        sock.sendall(packet(-1, AUTH_RESPONSE, ""))
                        continue
                    with fake.lock:
                        fake.commands.append(body)
                    if echo:
                        print(f"rcon: {body}", flush=True)
                    if fake.mode == "silent":
                        continue
                    if fake.mode == "slow":
                        time.sleep(fake.delay)
                    answer_id = 0 if fake.mode == "id0" else packet_id
                    text = fake.answer(body)
                    parts = [text[i:i + SPLIT] for i in range(0, len(text), SPLIT)] or [""]
                    try:
                        sock.sendall(b"".join(packet(answer_id, RESPONSE, p) for p in parts))
                    except OSError:
                        return
                    if fake.mode == "drop":
                        sock.close()
                        return

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self._server = Server(("127.0.0.1", port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @staticmethod
    def answer(command: str) -> str:
        name, _, rest = command.partition(" ")
        if name == "echo":
            return rest
        if name == "big":
            return "x" * int(rest)
        return f"ok: {command}"

    def start(self) -> FakeRcon:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> FakeRcon:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _read(sock: socket.socket) -> tuple[int, int, str] | None:
    head = _recv(sock, 4)
    if head is None:
        return None
    (size,) = struct.unpack("<i", head)
    data = _recv(sock, size)
    if data is None:
        return None
    packet_id, kind = struct.unpack("<ii", data[:8])
    return packet_id, kind, data[8:-2].decode("utf-8", errors="replace")


def _recv(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=25575)
    parser.add_argument("--password", default="test")
    parser.add_argument("--mode", default="valve",
                        choices=["valve", "id0", "slow", "drop", "silent"])
    args = parser.parse_args()
    fake = FakeRcon(args.password, args.mode, args.port, echo=True).start()
    print(f"Fake RCON on 127.0.0.1:{fake.port} ({args.mode}); Ctrl+C to stop", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fake.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
