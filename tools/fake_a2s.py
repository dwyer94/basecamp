"""Fake Steam server query (A2S_INFO) for tests and dev. Standard library only.

Answers ``TSource Engine Query`` on a UDP port with a player count, like a game's query
port. With ``challenge`` on (the default, as current Source servers do) the first request
gets a challenge, and only a request carrying it gets the answer.

    python tools/fake_a2s.py [--port 27015] [--players 3] [--max 16]

``players`` and ``most`` can be changed while it runs; ``silent`` stops it answering.
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import struct
import threading

HEADER = b"\xff\xff\xff\xff"
REQUEST = HEADER + b"TSource Engine Query\x00"
CHALLENGE = b"\x01\x02\x03\x04"


def info_answer(players: int, most: int, name: str = "Fake") -> bytes:
    strings = b"".join(s.encode() + b"\x00" for s in (name, "map", "fake", "Fake game"))
    return (HEADER + b"I\x11" + strings + struct.pack("<H", 0)
            + bytes([min(players, 255), min(most, 255), 0]) + b"dw\x00\x01")


class FakeA2S:
    def __init__(self, players: int = 0, most: int = 16, port: int = 0,
                 challenge: bool = True) -> None:
        self.players = players
        self.most = most
        self.challenge = challenge
        self.silent = False
        self.requests = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", port))
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while True:
            try:
                data, peer = self._sock.recvfrom(1400)
            except OSError:
                return
            if not data.startswith(REQUEST) or self.silent:
                continue
            self.requests += 1
            if self.challenge and data[len(REQUEST):] != CHALLENGE:
                self._sock.sendto(HEADER + b"A" + CHALLENGE, peer)
                continue
            self._sock.sendto(info_answer(self.players, self.most), peer)

    def start(self) -> FakeA2S:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._sock.close()

    def __enter__(self) -> FakeA2S:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fake A2S_INFO server")
    parser.add_argument("--port", type=int, default=27015)
    parser.add_argument("--players", type=int, default=3)
    parser.add_argument("--max", type=int, default=16)
    args = parser.parse_args()
    fake = FakeA2S(args.players, args.max, args.port).start()
    print(f"Fake A2S on udp 127.0.0.1:{fake.port}: {args.players}/{args.max} players. "
          "Ctrl+C to stop.", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        threading.Event().wait()
    fake.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
