"""Source RCON client, standard library only (docs/02-architecture.md, D-92..D-94).

Protocol (Valve's Source RCON): each packet is ``<size:int32 le><id:int32><type:int32>
<body ascii/utf-8>\\0\\0``, size counting everything after itself. Auth is type 3 and is
answered by a type 2 packet whose id is the request's, or -1 for a wrong password (some
servers send an empty type 0 packet first). A command is type 2, answered by one or more
type 0 packets with its id (Palworld always uses id 0, so 0 counts too; Basecamp's own ids
start at 1).

Long answers come split over several packets of up to 4096 bytes. Valve's trick of a
second, empty packet to mark the end confuses some game servers (Palworld among them), so
this client reads on while the last packet was full size and stops after a short quiet
spell otherwise (D-93).

One ``RconClient`` keeps its connection between commands and reconnects (and logs in
again) once if the connection turns out to be dead. Passwords are never logged or put in
an error message.
"""

from __future__ import annotations

import contextlib
import ipaddress
import itertools
import logging
import socket
import struct
import threading
import time

log = logging.getLogger(__name__)

AUTH = 3
AUTH_RESPONSE = 2
EXEC = 2
RESPONSE = 0

MAX_BODY = 4096  # a server splits longer answers; a packet this full means more follow
_SPLIT_AT = MAX_BODY - 96  # some servers split a little short of 4096
_MAX_PACKET = 1 << 16  # refuse anything bigger: not RCON


class RconError(Exception):
    """Couldn't run the command: refused, timed out, connection lost."""


class RconAuthError(RconError):
    pass


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_host(host: str, allow_remote: bool) -> None:
    """Basecamp only talks RCON (and REST) to games on this PC unless a setting allows
    more (``remote_console_allow_remote``): the password goes over the wire unencrypted."""
    if not allow_remote and not is_loopback(host):
        raise RconError(f"{host} isn't this PC; set remote_console_allow_remote = true "
                        "in settings.toml to allow it")


def encode(packet_id: int, kind: int, body: str) -> bytes:
    data = body.encode("utf-8")
    return struct.pack("<iii", len(data) + 10, packet_id, kind) + data + b"\0\0"


class RconClient:
    def __init__(self, host: str, port: int, password: str, *, timeout: float = 5.0,
                 quiet_s: float = 0.3, allow_remote: bool = False):
        check_host(host, allow_remote)
        self.host = host
        self.port = port
        self._password = password
        self.timeout = timeout
        self.quiet_s = quiet_s
        self._sock: socket.socket | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()  # one command at a time per connection

    def __repr__(self) -> str:  # never the password
        return f"RconClient({self.host}:{self.port})"

    def command(self, text: str) -> str:
        """Run ``text`` and return the answer (may be ""). Reconnects once if the kept
        connection turns out to be dead. Raises RconError."""
        with self._lock:
            fresh = self._sock is None
            try:
                return self._command(text)
            except RconAuthError:
                self.close()
                raise
            except (OSError, RconError) as e:
                self.close()
                if fresh or isinstance(e, TimeoutError | socket.timeout):
                    raise _as_error(e) from None
                log.info("%r: connection lost (%s); reconnecting", self, e)
            try:
                return self._command(text)
            except (OSError, RconError) as e:
                self.close()
                raise _as_error(e) from None

    def close(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    def __enter__(self) -> RconClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ------------------------------------------------------------------

    def _connect(self) -> socket.socket:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except (TimeoutError, ConnectionRefusedError):
            # Windows takes about 2 s to refuse a closed port, so a timeout here means
            # the same thing.
            raise RconError(f"RCON isn't listening on port {self.port} (is it turned on "
                            "in the game's settings?)") from None
        sock.settimeout(self.timeout)
        try:
            auth_id = next(self._ids)
            sock.sendall(encode(auth_id, AUTH, self._password))
            deadline = time.monotonic() + self.timeout
            while True:
                packet_id, kind, _ = self._read(sock, deadline)
                if kind != AUTH_RESPONSE:
                    continue  # the empty RESPONSE some servers send before the answer
                if packet_id == -1:
                    raise RconAuthError("RCON refused the password")
                if packet_id != auth_id:
                    raise RconError("RCON answered the login with the wrong id")
                return sock
        except BaseException:
            sock.close()
            raise

    def _command(self, text: str) -> str:
        if self._sock is None:
            self._sock = self._connect()
        sock = self._sock
        packet_id = next(self._ids)
        sock.sendall(encode(packet_id, EXEC, text))
        deadline = time.monotonic() + self.timeout
        parts: list[str] = []
        while True:
            # After a part that wasn't full, wait only briefly for a straggler.
            short = bool(parts) and len(parts[-1].encode("utf-8")) < _SPLIT_AT
            try:
                got = self._read(sock, min(deadline, time.monotonic() + self.quiet_s)
                                 if short else deadline)
            except (TimeoutError, ConnectionError):
                if not parts:
                    raise
                # Done (or the answer ended on a full packet). A closed connection is
                # noticed again, and reconnected, by the next command.
                return "".join(parts)
            got_id, kind, body = got
            if got_id == -1:
                raise RconAuthError("RCON says the login is no longer valid")
            if got_id in (packet_id, 0) and kind == RESPONSE:
                parts.append(body)  # Palworld answers every command with id 0
            # Anything else is an answer to an earlier, timed-out command: drop it.

    def _read(self, sock: socket.socket, deadline: float) -> tuple[int, int, str]:
        head = self._recv(sock, 4, deadline)
        (size,) = struct.unpack("<i", head)
        if not 10 <= size <= _MAX_PACKET:
            raise RconError(f"RCON sent a bad packet size ({size})")
        data = self._recv(sock, size, deadline)
        packet_id, kind = struct.unpack("<ii", data[:8])
        body = data[8:-2].decode("utf-8", errors="replace")
        return packet_id, kind, body

    @staticmethod
    def _recv(sock: socket.socket, n: int, deadline: float) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("RCON didn't answer in time")
            sock.settimeout(left)
            try:
                chunk = sock.recv(n - len(buf))
            except TimeoutError:
                raise TimeoutError("RCON didn't answer in time") from None
            if not chunk:
                raise ConnectionError("RCON closed the connection")
            buf += chunk
        return bytes(buf)


def _as_error(e: BaseException) -> RconError:
    if isinstance(e, RconError):
        return e
    if isinstance(e, TimeoutError | socket.timeout):
        return RconError("RCON didn't answer in time")
    return RconError(f"RCON connection failed: {e}")
