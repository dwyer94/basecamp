"""Steam's server query, A2S_INFO, for the player count (D-132). Standard library only.

One UDP request to the game's query port; a server may first answer with a challenge
(``A`` + 4 bytes), which is sent back on the request. The answer (``I``) holds the
server's name, map, folder and game as C strings, the app ID, then the player count, the
most players, and the bots. Only those numbers are read. A split (multi-packet) answer
isn't handled; A2S_INFO fits one packet.
"""

from __future__ import annotations

import socket
import time

REQUEST = b"\xff\xff\xff\xffTSource Engine Query\x00"
_HEADER = b"\xff\xff\xff\xff"


class A2SError(Exception):
    pass


def parse_info(data: bytes) -> tuple[int, int]:
    """(players, max players) from an A2S_INFO answer. Raises A2SError."""
    if not data.startswith(_HEADER + b"I") or len(data) < 6:
        raise A2SError("not an A2S_INFO answer")
    i = 6  # header, "I", protocol byte
    try:
        for _ in range(4):  # name, map, folder, game
            i = data.index(b"\x00", i) + 1
        players, most = data[i + 2], data[i + 3]  # after the 2-byte app ID
    except (ValueError, IndexError):
        raise A2SError("A2S_INFO answer cut short") from None
    return players, most


def query_info(host: str, port: int, timeout: float = 3.0) -> tuple[int, int]:
    """Ask a server how many players it has: (players, max players). Raises A2SError."""
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        request = REQUEST
        for _ in range(3):  # the request, and again with a challenge (twice at most)
            left = deadline - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            try:
                sock.sendto(request, (host, port))
                data, _ = sock.recvfrom(1400)
            except TimeoutError:
                raise A2SError("no answer to the server query") from None
            except OSError as e:  # e.g. the port is closed (Windows: connection reset)
                raise A2SError(f"server query failed: {e.strerror or e}") from None
            if data.startswith(_HEADER + b"A") and len(data) >= 9:
                request = REQUEST + data[5:9]
                continue
            return parse_info(data)
    raise A2SError("no answer to the server query")

