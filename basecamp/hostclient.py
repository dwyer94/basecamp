"""Basecamp's side of the host control channel (see basecamp/host.py for the protocol)."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from collections.abc import Iterator
from typing import Any

# Host protocol versions this Basecamp can use. Raise MAX when host.py gains ops; only
# raise MIN if an old version can no longer be running anywhere (see docs/07-updates.md).
MIN_HOST_PROTOCOL = 1
MAX_HOST_PROTOCOL = 2


class HostError(Exception):
    pass


class IncompatibleHost(HostError):
    pass


class HostClient:
    def __init__(self, port: int, token: str, timeout: float = 5.0):
        self.port = port
        self.token = token
        self.timeout = timeout
        self.protocol: int | None = None
        self.host_version = ""
        self.host_pid: int | None = None
        self._stream_sock: socket.socket | None = None
        self._lock = threading.Lock()

    def _connect(self, timeout: float | None) -> tuple[socket.socket, Any]:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        rfile = sock.makefile("rb")
        try:
            self._send(sock, {"op": "hello", "token": self.token,
                              "protocol": MAX_HOST_PROTOCOL})
            reply = self._read(rfile)
            if not reply.get("ok"):
                raise HostError(reply.get("error", "hello refused"))
            protocol = int(reply["protocol"])
            if protocol < MIN_HOST_PROTOCOL:
                raise IncompatibleHost(f"host speaks protocol {protocol}, need "
                                       f">= {MIN_HOST_PROTOCOL}")
            self.protocol = protocol
            self.host_version = str(reply.get("host_version", ""))
            self.host_pid = reply.get("host_pid")
            sock.settimeout(timeout)
            return sock, rfile
        except BaseException:
            rfile.close()
            sock.close()
            raise

    @staticmethod
    def _send(sock: socket.socket, data: dict) -> None:
        sock.sendall((json.dumps(data) + "\n").encode("utf-8"))

    @staticmethod
    def _read(rfile: Any) -> dict:
        raw = rfile.readline()
        if not raw:
            raise HostError("host closed the connection")
        return json.loads(raw)

    def hello(self) -> int:
        sock, rfile = self._connect(self.timeout)
        rfile.close()
        sock.close()
        assert self.protocol is not None
        return self.protocol

    def request(self, op: str, timeout: float | None = None, since: int = 1,
                **fields: Any) -> dict:
        """Send one op. ``since``: the protocol that added it; an older host gets HostError."""
        try:
            sock, rfile = self._connect(timeout or self.timeout)
            try:
                if (self.protocol or 1) < since:
                    raise HostError(f"{op}: host speaks protocol {self.protocol}, needs {since}")
                self._send(sock, {"op": op, **fields})
                reply = self._read(rfile)
            finally:
                rfile.close()
                sock.close()
        except (OSError, ValueError) as e:
            raise HostError(f"{op}: {e}") from e
        if not reply.get("ok"):
            raise HostError(f"{op}: {reply.get('error', 'failed')}")
        return reply

    def status(self) -> dict:
        return self.request("status")

    def send_line(self, text: str) -> None:
        self.request("send", text=text)

    def note(self, text: str) -> None:
        self.request("note", text=text)

    def mark_stopping(self) -> None:
        self.request("stopping")

    def ctrl_break(self) -> None:
        self.request("ctrl_break", timeout=10)

    def ctrl_c(self) -> None:
        self.request("ctrl_c", timeout=10, since=2)

    def kill(self) -> None:
        self.request("kill", timeout=15)

    def attach(self, after_seq: int = 0) -> Iterator[dict]:
        """Replay lines after `after_seq`, then stream live events until the game exits
        (ends after the "exit" event) or ``close_stream`` is called (raises HostError)."""
        try:
            sock, rfile = self._connect(None)
        except (OSError, ValueError) as e:
            raise HostError(f"attach: {e}") from e
        with self._lock:
            self._stream_sock = sock
        try:
            self._send(sock, {"op": "attach", "after_seq": after_seq})
            reply = self._read(rfile)
            if not reply.get("ok"):
                raise HostError(reply.get("error", "attach refused"))
            while True:
                raw = rfile.readline()
                if not raw:
                    return
                yield json.loads(raw)
        except (OSError, ValueError) as e:
            raise HostError(f"stream: {e}") from e
        finally:
            with self._lock:
                self._stream_sock = None
            rfile.close()
            sock.close()

    def close_stream(self) -> None:
        with self._lock:
            sock = self._stream_sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
