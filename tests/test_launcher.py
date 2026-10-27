"""Phase 6: the launcher's swap, health check, rollback, back-off and pruning
(docs/07-updates.md). Each "version" here is a tiny fake ``basecamp`` package."""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import launcher as launcher_mod
from launcher import Launcher, version_key
from tests.conftest import PYTHON, wait_for

FAKE_MAIN = '''
import json, os, sys, threading, time, tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

here = Path(__file__).resolve().parent.parent
version = here.name
data = Path(os.environ["BASECAMP_DATA_DIR"])
mode = (here / "mode.txt").read_text().strip() if (here / "mode.txt").exists() else "ok"
with open(data / "starts.txt", "a") as f:
    f.write(f"{version} {os.environ['BASECAMP_ROOT']} {os.environ['BASECAMP_LAUNCHER']}\\n")
if mode == "crash":
    sys.exit(1)
port = tomllib.loads((data / "settings.toml").read_text())["basecamp"]["port"]


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": mode != "bad", "version": version}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if mode != "hang":
    server = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
while True:
    stop = data / "exit.txt"
    if stop.exists():
        code = int(stop.read_text())
        stop.unlink()
        os._exit(code)
    time.sleep(0.05)
'''


def free_tcp() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def add_version(root: Path, version: str, mode: str = "ok") -> None:
    pkg = root / "versions" / version / "basecamp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (pkg / "__main__.py").write_text(FAKE_MAIN)
    (pkg.parent / "mode.txt").write_text(mode)


class Env:
    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "root"
        self.data = tmp / "data"
        (self.root / "versions").mkdir(parents=True)
        self.data.mkdir()
        (self.data / "settings.toml").write_text(f"[basecamp]\nport = {free_tcp()}\n")
        self.launcher = Launcher(self.root, self.data, str(PYTHON), health_timeout_s=8,
                                 backoff_s=(0.2, 0.4, 0.8), stable_s=3600)
        self.thread: threading.Thread | None = None

    def file(self, name: str) -> str:
        return self.launcher.read(name)

    def starts(self) -> list[str]:
        try:
            return [ln.split()[0] for ln in (self.data / "starts.txt").read_text().splitlines()]
        except OSError:
            return []

    def run(self) -> None:
        self.thread = threading.Thread(target=self.launcher.run, daemon=True)
        self.thread.start()

    def exit_basecamp(self, code: int) -> None:
        (self.data / "exit.txt").write_text(str(code))

    def stop(self) -> None:
        self.launcher.stop.set()
        if self.thread:
            self.thread.join(40)


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.stop()


def make_db(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE marker (v TEXT)")
    conn.execute("INSERT INTO marker VALUES (?)", (marker,))
    conn.commit()
    conn.close()


def db_marker(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT v FROM marker").fetchone()[0]
    finally:
        conn.close()


def test_version_keys():
    assert version_key("0.10.0") > version_key("0.9.0") > version_key("0.9.0-beta.1")
    assert version_key("0.1.0.tmp") is None and version_key("junk") is None


def test_runs_current_with_root_and_launcher_version(env):
    add_version(env.root, "1.0.0")
    env.launcher.write("current.txt", "1.0.0")
    env.run()
    wait_for(lambda: env.starts() == ["1.0.0"])
    line = (env.data / "starts.txt").read_text().split()
    assert Path(line[1]) == env.root and line[2] == str(launcher_mod.LAUNCHER_VERSION)


def test_exit_3_swaps_to_pending_and_keeps_it_when_healthy(env):
    add_version(env.root, "1.0.0")
    add_version(env.root, "2.0.0")
    env.launcher.write("current.txt", "1.0.0")
    env.run()
    wait_for(lambda: env.starts() == ["1.0.0"])
    env.launcher.write("pending.txt", "2.0.0")
    env.exit_basecamp(3)
    wait_for(lambda: env.starts() == ["1.0.0", "2.0.0"])
    wait_for(lambda: not (env.root / "trial.txt").exists())
    assert env.file("current.txt") == "2.0.0" and env.file("previous.txt") == "1.0.0"
    assert not (env.root / "pending.txt").exists()
    assert not (env.root / "update-failed.json").exists()


def test_unhealthy_update_rolls_back_with_the_db_copy(env):
    add_version(env.root, "1.0.0")
    add_version(env.root, "2.0.0", mode="bad")
    make_db(env.data / "basecamp.db", "written by 2.0.0")
    (env.data / "basecamp.db-wal").write_bytes(b"stale")
    make_db(env.data / "db-backups" / "before-2.0.0.db", "before the update")
    env.launcher.write("current.txt", "1.0.0")
    env.run()
    wait_for(lambda: env.starts() == ["1.0.0"])
    env.launcher.write("pending.txt", "2.0.0")
    env.exit_basecamp(3)
    wait_for(lambda: env.starts() == ["1.0.0", "2.0.0", "1.0.0"], timeout=30)
    note = json.loads(env.file("update-failed.json"))
    assert note["from"] == "1.0.0" and note["to"] == "2.0.0" and note["db_restored"]
    assert "no healthy answer" in note["reason"]
    assert env.file("current.txt") == "1.0.0"
    assert not (env.root / "trial.txt").exists() and not (env.root / "previous.txt").exists()
    assert db_marker(env.data / "basecamp.db") == "before the update"
    assert not (env.data / "basecamp.db-wal").exists()


def test_an_update_that_exits_at_once_rolls_back_too(env):
    add_version(env.root, "1.0.0")
    add_version(env.root, "2.0.0", mode="crash")
    env.launcher.write("current.txt", "1.0.0")
    env.run()
    wait_for(lambda: env.starts() == ["1.0.0"])
    env.launcher.write("pending.txt", "2.0.0")
    env.exit_basecamp(3)
    wait_for(lambda: env.starts() == ["1.0.0", "2.0.0", "1.0.0"], timeout=20)
    note = json.loads(env.file("update-failed.json"))
    assert "exited with code 1" in note["reason"] and not note["db_restored"]


def test_a_trial_left_by_a_launcher_restart_is_still_checked(env):
    add_version(env.root, "1.0.0")
    add_version(env.root, "2.0.0", mode="hang")
    for name, value in (("current.txt", "2.0.0"), ("previous.txt", "1.0.0"),
                        ("trial.txt", "2.0.0")):
        env.launcher.write(name, value)
    env.run()
    wait_for(lambda: env.starts() == ["2.0.0", "1.0.0"], timeout=30)
    assert env.file("current.txt") == "1.0.0"


def test_exit_3_without_a_usable_pending_restarts_the_same_version(env):
    add_version(env.root, "1.0.0")
    env.launcher.write("current.txt", "1.0.0")
    env.run()
    wait_for(lambda: env.starts() == ["1.0.0"])
    env.launcher.write("pending.txt", "9.9.9")  # not unpacked
    env.exit_basecamp(3)
    wait_for(lambda: env.starts() == ["1.0.0", "1.0.0"])
    assert env.file("current.txt") == "1.0.0" and not (env.root / "pending.txt").exists()


def test_crashes_restart_with_back_off(env, monkeypatch):
    add_version(env.root, "1.0.0", mode="crash")
    env.launcher.write("current.txt", "1.0.0")
    waits: list[float] = []
    real_wait = env.launcher.stop.wait

    def wait(timeout=None):
        waits.append(timeout)
        return real_wait(timeout)

    monkeypatch.setattr(env.launcher.stop, "wait", wait)
    env.run()
    wait_for(lambda: len(env.starts()) >= 4, timeout=20)
    assert waits[:3] == [0.2, 0.4, 0.8]


def test_missing_current_falls_back_to_previous(env):
    add_version(env.root, "1.0.0")
    env.launcher.write("current.txt", "2.0.0")
    env.launcher.write("previous.txt", "1.0.0")
    env.run()
    wait_for(lambda: env.starts() == ["1.0.0"])
    assert env.file("current.txt") == "1.0.0"


def test_prune_keeps_the_newest_three_and_current_and_previous(env):
    for v in ("0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.5.0", "0.6.0"):
        add_version(env.root, v)
    (env.root / "versions" / "0.7.0.tmp").mkdir()
    env.launcher.write("current.txt", "0.5.0")
    env.launcher.write("previous.txt", "0.1.0")
    env.launcher.prune()
    left = sorted(p.name for p in (env.root / "versions").iterdir())
    assert left == ["0.1.0", "0.4.0", "0.5.0", "0.6.0", "0.7.0.tmp"]


def test_only_one_launcher_per_folder(tmp_path):
    first = launcher_mod._single_instance(tmp_path)
    assert first is not None
    assert launcher_mod._single_instance(tmp_path) is None
    first.close()
