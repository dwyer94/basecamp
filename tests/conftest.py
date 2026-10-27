from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from basecamp.db import Store
from basecamp.models import ACTIVE_STATES, Port, Protocol, ServerConfig
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor

REPO = Path(__file__).resolve().parent.parent
FAKE_SERVER = REPO / "tools" / "fake_server.py"
# The base interpreter, not the venv's python.exe (which is itself a launcher that starts
# the base one as a child). Fake servers are stdlib only.
PYTHON = Path(getattr(sys, "_base_executable", sys.executable))

if sys.platform != "win32":
    pytest.skip("Basecamp targets Windows", allow_module_level=True)


def wait_for(cond: Callable[[], object], timeout: float = 10.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {cond}")


def free_port(protocol: Protocol = Protocol.UDP) -> int:
    kind = socket.SOCK_DGRAM if protocol is Protocol.UDP else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def fake_config(name: str, *extra: str, port: int | None = None,
                protocol: Protocol = Protocol.UDP, **fields: object) -> ServerConfig:
    port = free_port(protocol) if port is None else port
    args = ["-I", str(FAKE_SERVER), "--name", name, "--port", str(port),
            "--proto", protocol.value, *extra]
    values: dict[str, object] = dict(
        name=name,
        exe_path=str(PYTHON),
        args=subprocess.list2cmdline(args),
        ports=[Port(port=port, protocol=protocol, label="game")],
        join_port=port,
        ready_pattern="Server ready",
        ready_after_s=10,
    )
    values.update(fields)
    return ServerConfig(**values)


def console_text(sup: Supervisor, server_id: int) -> str:
    return "\n".join(line.text for line in sup.console(server_id).lines())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.load(tmp_path / "data", low_ram_mb=0, boot_gap_s=0.2,
                         real_exe_timeout_s=10, kill_wait_s=10)


@pytest.fixture
def store(settings: Settings) -> Iterator[Store]:
    store = Store.open(settings.db_path, settings.migrations_dir)
    yield store
    store.close()


@pytest.fixture
def sup(store: Store, settings: Settings) -> Iterator[Supervisor]:
    sup = Supervisor(store, settings)
    yield sup
    for server in store.list_servers():
        if sup.state(server.id) in ACTIVE_STATES:
            sup.kill(server.id)
    sup.detach()


class FakeWebhook:
    """A local HTTP endpoint that takes Discord webhook posts. ``answers`` is a list of
    (status, json body) to give before falling back to 204."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.answers: list[tuple[int, dict | None]] = []
        self.lock = threading.Lock()
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with hook.lock:
                    status, answer = hook.answers.pop(0) if hook.answers else (204, None)
                    if status == 204:
                        hook.posts.append(body)
                payload = json.dumps(answer).encode() if answer is not None else b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/webhooks/1/test"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def texts(self) -> list[str]:
        with self.lock:
            return [p["content"] for p in self.posts]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def webhook() -> Iterator[FakeWebhook]:
    hook = FakeWebhook()
    yield hook
    hook.close()
