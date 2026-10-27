"""Fake Discord webhook for tests and dev. Standard library only.

Takes what Basecamp sends a webhook URL (``.../api/webhooks/<id>/<token>``):

    POST  <url>                     a message: 204, no body
    POST  <url>?wait=true           a message: 200 with {"id": ...} (the live status, R-63)
    PATCH <url>/messages/<id>       edit that message: 200, or 404 if there's no such id

``answers`` holds (status, json body) pairs given to the next POSTs or PATCHes instead
(429s, 500s), and ``forget(id)`` deletes a message as a Discord user could.

Run it for the browser check, then put the URL it prints in ``data\\secrets.toml``:

    python tools/fake_discord.py [--port 8159]
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeDiscord:
    def __init__(self, port: int = 0, echo: bool = False) -> None:
        self.posts: list[dict] = []  # event messages (POST without wait)
        self.messages: dict[str, str] = {}  # id -> content, for ?wait=true posts and edits
        self.edits: list[tuple[str, str]] = []  # (id, content) per PATCH
        self.answers: list[tuple[int, dict | None]] = []
        self.lock = threading.Lock()
        ids = itertools.count(1_000_000_000_000_000_001)
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def _body(self) -> dict:
                return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

            def _answer(self, status: int, payload: dict | None) -> None:
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                body = self._body()
                wait = "wait=true" in self.path.partition("?")[2]
                with hook.lock:
                    status, answer = hook.answers.pop(0) if hook.answers else (0, None)
                    if not status:
                        if wait:
                            msg_id = str(next(ids))
                            hook.messages[msg_id] = body["content"]
                            status, answer = 200, {"id": msg_id, "content": body["content"]}
                        else:
                            hook.posts.append(body)
                            status = 204
                if echo:
                    print(f"POST{' (wait)' if wait else ''} {status}: {body['content']}\n",
                          flush=True)
                self._answer(status, answer)

            def do_PATCH(self) -> None:
                body = self._body()
                msg_id = self.path.partition("?")[0].rstrip("/").rsplit("/", 1)[-1]
                with hook.lock:
                    status, answer = hook.answers.pop(0) if hook.answers else (0, None)
                    if not status:
                        if "/messages/" in self.path and msg_id in hook.messages:
                            hook.messages[msg_id] = body["content"]
                            hook.edits.append((msg_id, body["content"]))
                            status, answer = 200, {"id": msg_id, "content": body["content"]}
                        else:
                            status, answer = 404, {"message": "Unknown Message", "code": 10008}
                if echo:
                    print(f"PATCH {msg_id} {status}: {body['content']}\n", flush=True)
                self._answer(status, answer)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/webhooks/1/test"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def texts(self) -> list[str]:
        with self.lock:
            return [p["content"] for p in self.posts]

    def forget(self, msg_id: str) -> None:
        with self.lock:
            self.messages.pop(msg_id, None)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8159)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # messages hold emoji and →
    hook = FakeDiscord(args.port, echo=True)
    print(f"Fake Discord webhook: {hook.url}", flush=True)
    try:
        hook._thread.join()
    except KeyboardInterrupt:
        hook.close()


if __name__ == "__main__":
    main()
