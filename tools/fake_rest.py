"""Fake Palworld REST API for tests and dev. Standard library only.

Takes what basecamp/rest.py sends (HTTP basic auth, user ``admin``):

    GET  /v1/api/info | players | metrics | settings     JSON
    POST /v1/api/announce {"message"} | save | shutdown {"waittime", "message"} | stop
         200 with an empty body, like the real server

A wrong password is a 401. ``mode`` makes it misbehave:

    "ok"     the default
    "slow"   answers ``delay`` seconds late
    "drop"   closes the connection without an answer

``calls`` holds (method, endpoint, body) for every call that got past the password.
``on_shutdown`` is called with the wait time when /shutdown is posted (tests use it to end
a fake game). Run it by hand:

    python tools/fake_rest.py [--port 8212] [--password test] [--mode ok]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GETS = {
    "info": {"version": "v0.0.0-fake", "servername": "Fake server", "description": "",
             "worldguid": "0" * 32},
    "players": {"players": []},
    "metrics": {"currentplayernum": 0, "serverfps": 60, "uptime": 1, "maxplayernum": 16},
    "settings": {"Difficulty": "None"},
}
POSTS = ("announce", "save", "shutdown", "stop")


class FakeRest:
    def __init__(self, password: str = "test", mode: str = "ok", port: int = 0,
                 delay: float = 3.0, echo: bool = False,
                 on_shutdown: Callable[[int], object] | None = None) -> None:
        self.password = password
        self.mode = mode
        self.delay = delay
        self.on_shutdown = on_shutdown
        self.calls: list[tuple[str, str, dict | None]] = []
        self.players: list[dict] = []  # who's on: players and metrics answer with these
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # quiet
                pass

            def _authorised(self) -> bool:
                want = base64.b64encode(f"admin:{fake.password}".encode()).decode()
                return self.headers.get("Authorization", "") == f"Basic {want}"

            def _send(self, status: int, payload: dict | None = None) -> None:
                data = json.dumps(payload, indent="\t").encode() if payload is not None else b""
                self.send_response(status)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                if fake.mode == "drop":
                    self.close_connection = True
                    self.connection.close()
                    return
                if fake.mode == "slow":
                    time.sleep(fake.delay)
                name = self.path.removeprefix("/v1/api/")
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if not self._authorised():
                    self._send(401)
                    return
                body = json.loads(raw) if raw else None
                if (method == "GET" and name not in GETS) or (
                        method == "POST" and name not in POSTS):
                    self._send(404)
                    return
                with fake.lock:
                    fake.calls.append((method, name, body))
                if echo:
                    print(f"rest: {method} {name} {body or ''}", flush=True)
                if method == "GET":
                    answer = dict(GETS[name])
                    if name == "players":
                        answer["players"] = list(fake.players)
                    elif name == "metrics":
                        answer["currentplayernum"] = len(fake.players)
                    self._send(200, answer)
                    return
                self._send(200)
                if name == "shutdown" and fake.on_shutdown is not None:
                    fake.on_shutdown(int((body or {}).get("waittime", 1)))

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> FakeRest:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> FakeRest:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8212)
    parser.add_argument("--password", default="test")
    parser.add_argument("--mode", default="ok", choices=["ok", "slow", "drop"])
    args = parser.parse_args()
    fake = FakeRest(args.password, args.mode, args.port, echo=True).start()
    print(f"Fake REST API on http://127.0.0.1:{fake.port}/v1/api/ ({args.mode}); "
          "Ctrl+C to stop", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fake.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
