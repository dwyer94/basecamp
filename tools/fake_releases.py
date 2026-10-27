"""A stand-in for GitHub's Releases API, for testing Basecamp's self-update on the dev PC.

    python tools/fake_releases.py --dir <folder> --port 8157

``<folder>\\releases.json`` is a list of releases, newest last::

    [{"tag_name": "v0.2.0", "body": "notes", "prerelease": false, "draft": false,
      "assets": ["basecamp-0.2.0.zip", "basecamp-0.2.0.zip.sig"]}]

and the asset files sit next to it. It answers ``/repos/<owner>/<repo>/releases/latest``
(newest non-draft, non-pre-release), ``/repos/<owner>/<repo>/releases`` (all, newest first)
with an ETag and 304s, and ``/download/<file>``. The file is read again on every request, so
editing it "publishes" a release. Point Basecamp at it with ``update_api =
"http://127.0.0.1:8157"`` in the data folder's settings.toml. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def make_handler(folder: Path, base: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def releases(self) -> list[dict]:
            try:
                data = json.loads((folder / "releases.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = []
            out = []
            for i, rel in enumerate(data):
                rel = dict(rel)
                rel.setdefault("name", rel["tag_name"])
                rel.setdefault("draft", False)
                rel.setdefault("prerelease", False)
                rel.setdefault("published_at", f"2026-01-01T00:00:{i:02d}Z")
                rel["html_url"] = f"{base}/releases/{rel['tag_name']}"
                rel["assets"] = [{"name": n, "browser_download_url": f"{base}/download/{n}"}
                                 for n in rel.get("assets", [])]
                out.append(rel)
            return out

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            m = re.fullmatch(r"/download/([\w.-]+)", path)
            if m:
                file = folder / m[1]
                if not file.is_file():
                    return self.answer(404, b'{"message":"Not Found"}')
                return self.answer(200, file.read_bytes(), "application/octet-stream")
            m = re.fullmatch(r"/repos/[\w.-]+/[\w.-]+/releases(/latest)?", path)
            if not m:
                return self.answer(404, b'{"message":"Not Found"}')
            releases = [r for r in self.releases() if not r["draft"]]
            if m[1]:
                stable = [r for r in releases if not r["prerelease"]]
                if not stable:
                    return self.answer(404, b'{"message":"Not Found"}')
                body = json.dumps(stable[-1]).encode()
            else:
                body = json.dumps(list(reversed(releases))).encode()
            etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
            if self.headers.get("If-None-Match") == etag:
                return self.answer(304, b"", etag=etag)
            self.answer(200, body, etag=etag)

        def answer(self, status: int, body: bytes, kind: str = "application/json",
                   etag: str = "") -> None:
            self.send_response(status)
            if etag:
                self.send_header("ETag", etag)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}",
                  file=sys.stderr, flush=True)

    return Handler


def serve(folder: Path, port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    server.RequestHandlerClass = make_handler(folder, base)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8157)
    args = parser.parse_args()
    server = serve(args.dir.resolve(), args.port)
    print(f"Fake releases on http://127.0.0.1:{server.server_address[1]} from {args.dir}",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
