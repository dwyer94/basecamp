"""End-to-end test of Basecamp's self-update on the dev PC (Phase 6 done-when, docs/05-plan.md).

    .venv\\Scripts\\python tools\\e2e_selfupdate.py --root C:\\BasecampTest --work <scratch folder>

Uses a throwaway signing key and a local fake release server (tools/fake_releases.py), never
GitHub or the real key. It:

1. builds v0.1.0 (this repo), v0.2.0 (adds a migration), a broken v0.3.0 (``/healthz``
   answers 500) and v0.4.0 with a bad signature, all with the real vendored dependencies;
2. installs v0.1.0 into ``--root`` the way install.ps1 will (launcher + versions folder)
   and starts the launcher with the base Python (no venv, so only lib\\ is on the path);
3. adds a fake game server and starts it;
4. publishes v0.2.0, checks, installs: the launcher swaps, the fake game keeps its PID and
   is adopted with its console, and the new migration ran;
5. publishes v0.3.0, installs: the health check fails, the launcher rolls back to v0.2.0 and
   its DB copy by itself, and the game is still running;
6. publishes v0.4.0 (bad signature): refused, nothing unpacked;
7. stops the game and the launcher.

``--root`` is wiped first, so point it only at a test folder.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import build_release  # noqa: E402

from basecamp import selfupdate  # noqa: E402
from launcher import LAUNCHER_VERSION  # noqa: E402

PYTHON = Path(getattr(sys, "_base_executable", sys.executable))
PORT = 8155
RELEASES_PORT = 8157
BASE = f"http://127.0.0.1:{PORT}"
results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)


def api(method: str, path: str, body: object | None = None, timeout: float = 10) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Content-Type": "application/json", "Origin": BASE})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read()
        return json.loads(raw) if raw else {}


def health() -> dict | None:
    try:
        with urllib.request.urlopen(BASE + "/healthz", timeout=2) as res:
            return json.loads(res.read())
    except (OSError, ValueError):
        return None


def wait(cond, timeout: float, what: str):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = cond()
        except (OSError, ValueError, KeyError):
            value = None
        if value:
            return value
        time.sleep(0.5)
    raise TimeoutError(f"timed out after {timeout}s: {what}")


def make_src(work: Path, version: str, patch=None) -> Path:  # type: ignore[no-untyped-def]
    src = work / "src" / version
    shutil.rmtree(src, ignore_errors=True)
    ignore = shutil.ignore_patterns("__pycache__")
    for name in ("basecamp", "migrations", "templates"):
        shutil.copytree(REPO / name, src / name, ignore=ignore)
    for name in ("launcher.py", "pyproject.toml", "CHANGES.md"):
        shutil.copy2(REPO / name, src / name)
    init = src / "basecamp" / "__init__.py"
    text = init.read_text(encoding="utf-8")
    init.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', text),
                    encoding="utf-8")
    if patch:
        patch(src)
    return src


def add_migration(src: Path) -> None:
    (src / "migrations" / "0099_e2e_probe.sql").write_text(
        "-- e2e test only: proves an update's migrations run\n"
        "CREATE TABLE e2e_probe (x INTEGER);\n",
        encoding="utf-8")


def break_healthz(src: Path) -> None:
    add_migration(src)
    app = src / "basecamp" / "api" / "app.py"
    text = app.read_text(encoding="utf-8")
    old = '        return {"ok": True, "version": __version__}'
    assert old in text
    app.write_text(text.replace(old, '        raise HTTPException(500, "broken on purpose")'),
                   encoding="utf-8")


class Publisher:
    def __init__(self, folder: Path, key: Path, lib: Path, work: Path) -> None:
        self.folder, self.key, self.lib, self.work = folder, key, lib, work
        self.list: list[dict] = []
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "releases.json").write_text("[]")

    def build(self, version: str, patch=None, bad_sig: bool = False) -> Path:  # type: ignore[no-untyped-def]
        src = make_src(self.work, version, patch)
        zip_path = build_release.build(version, self.key, self.work / "dist", src, self.lib,
                                       test_key=True)
        if bad_sig:
            zip_path.with_name(zip_path.name + ".sig").write_bytes(
                Ed25519PrivateKey.generate().sign(zip_path.read_bytes()))
        return zip_path

    def publish(self, zip_path: Path, version: str) -> None:
        for p in (zip_path, zip_path.with_name(zip_path.name + ".sig")):
            shutil.copy2(p, self.folder / p.name)
        self.list.append({"tag_name": f"v{version}", "body": f"E2E test release {version}",
                          "assets": [zip_path.name, zip_path.name + ".sig"]})
        (self.folder / "releases.json").write_text(json.dumps(self.list))


def install_first(root: Path, data: Path, zip_path: Path) -> None:
    """What install.ps1 will do: unpack, check, set current, copy the launcher out."""
    folder = root / "versions" / "0.1.0"
    selfupdate.safe_extract(zip_path, folder)
    selfupdate.check_tree(folder, "0.1.0")
    (root / "current.txt").write_text("0.1.0")
    shutil.copy2(folder / "launcher.py", root / "launcher.py")
    data.mkdir(parents=True)
    (data / "settings.toml").write_text(
        "[basecamp]\n"
        f"port = {PORT}\n"
        f'update_api = "http://127.0.0.1:{RELEASES_PORT}"\n'
        "update_check_h = 0\n"  # the test presses Check now itself
        "low_ram_mb = 0\n", encoding="utf-8")


def free_udp() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def server_status(sid: int) -> dict:
    return api("GET", f"/api/servers/{sid}")


def check_and_wait(version: str) -> dict:
    api("POST", "/api/update/check")
    return wait(lambda: (lambda u: u if u["status"] not in ("checking", "downloading") else None)(
        api("GET", "/api/update")), 120, f"check for {version}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--lib", type=Path, help="reuse a vendored lib folder")
    args = parser.parse_args()
    root, work = args.root.resolve(), args.work.resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    work.mkdir(parents=True, exist_ok=True)
    data = root / "data"

    key = work / "throwaway.key"
    key.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    lib = args.lib or work / "lib"
    if not (lib / "fastapi").is_dir():
        print("Vendoring dependencies with pip (Windows wheels for Python 3.13)...", flush=True)
        shutil.rmtree(lib, ignore_errors=True)
        build_release.vendor(REPO, lib, build_release.PYTHON)

    pub = Publisher(work / "releases", key, lib, work)
    zips = {"0.1.0": pub.build("0.1.0"), "0.2.0": pub.build("0.2.0", add_migration),
            "0.3.0": pub.build("0.3.0", break_healthz),
            "0.4.0": pub.build("0.4.0", add_migration, bad_sig=True)}
    pub.publish(zips["0.1.0"], "0.1.0")
    install_first(root, data, zips["0.1.0"])

    procs: list[subprocess.Popen] = []
    log = (work / "fake-releases.log").open("w")
    procs.append(subprocess.Popen([sys.executable, str(REPO / "tools" / "fake_releases.py"),
                                   "--dir", str(pub.folder), "--port", str(RELEASES_PORT)],
                                  stdout=log, stderr=subprocess.STDOUT))
    launcher = subprocess.Popen([str(PYTHON), "-I", str(root / "launcher.py"), "--root",
                                 str(root), "--data", str(data)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(launcher)
    sid = None
    try:
        h = wait(lambda: health(), 60, "v0.1.0 up")
        step("v0.1.0 runs under the launcher from versions\\0.1.0", h["version"] == "0.1.0")
        u = api("GET", "/api/update")
        step("v0.1.0 knows it runs under the launcher",
             u["can_install"] and u["launcher"] == LAUNCHER_VERSION)

        game_port = free_udp()
        cfg = {"name": "e2e fake game", "exe_path": str(PYTHON),
               "args": subprocess.list2cmdline(["-I", str(REPO / "tools" / "fake_server.py"),
                                                "--name", "e2e", "--port", str(game_port),
                                                "--tick", "1"]),
               "ports": [{"port": game_port, "protocol": "udp", "label": "game"}],
               "join_port": game_port, "ready_pattern": "Server ready", "ready_after_s": 10}
        sid = api("POST", "/api/servers", cfg)["id"]
        api("POST", f"/api/servers/{sid}/start", timeout=60)
        s = wait(lambda: (lambda x: x if x["state"] == "running" else None)(server_status(sid)),
                 30, "fake game running")
        game_pid = s["pid"]
        step("fake game running", True, f"pid {game_pid}")

        # -- 0.1.0 -> 0.2.0 ------------------------------------------------------------
        pub.publish(zips["0.2.0"], "0.2.0")
        u = check_and_wait("0.2.0")
        step("v0.2.0 found, verified and unpacked", u["ready"] and u["ready_version"] == "0.2.0",
             u["error"])
        api("POST", "/api/update/install")
        t0 = time.monotonic()
        h = wait(lambda: (lambda x: x if x and x["version"] == "0.2.0" else None)(health()), 90,
                 "v0.2.0 healthy")
        step("launcher swapped to v0.2.0", (root / "current.txt").read_text() == "0.2.0",
             f"{time.monotonic() - t0:.1f} s from Install to healthy")
        wait(lambda: not (root / "trial.txt").exists(), 30, "trial over")
        step("previous.txt is 0.1.0", (root / "previous.txt").read_text() == "0.1.0")
        s = wait(lambda: (lambda x: x if x["state"] == "running" else None)(server_status(sid)),
                 30, "game adopted")
        step("fake game kept running and was adopted", s["pid"] == game_pid
             and psutil.pid_exists(game_pid), f"pid {s['pid']}, has_console {s['has_console']}")
        api("POST", f"/api/servers/{sid}/command", {"text": "say hello after the update"})
        texts = wait(lambda: (lambda t: t if any("hello after the update" in x for x in t)
                              else None)([ln["text"] for ln in api(
                                  "GET", f"/api/servers/{sid}/console")]), 15, "console echo")
        step("console and stdin work after the update", True,
             f"{len(texts)} lines kept, incl. ones from before the update"
             if len(texts) > 5 else "")
        conn = sqlite3.connect(data / "basecamp.db")
        probe = conn.execute("SELECT 1 FROM sqlite_master WHERE name='e2e_probe'").fetchone()
        conn.close()
        step("v0.2.0's migration ran", probe is not None)
        step("DB copy made before the update",
             (data / "db-backups" / "before-0.2.0.db").is_file())
        # Confirmed once the launcher takes it off trial (polled every 2 s).
        u = wait(lambda: (lambda x: x if x.get("last_update") else None)(
            api("GET", "/api/update")), 30, "update confirmed")
        step("v0.2.0 reports the update", (u.get("last_update") or {}).get("from") == "0.1.0")

        # -- 0.2.0 -> broken 0.3.0 -> rollback --------------------------------------------
        pub.publish(zips["0.3.0"], "0.3.0")
        u = check_and_wait("0.3.0")
        step("v0.3.0 found and unpacked", u["ready"] and u["ready_version"] == "0.3.0",
             u["error"])
        api("POST", "/api/update/install")
        t0 = time.monotonic()
        wait(lambda: (root / "current.txt").read_text() == "0.3.0", 30, "swap to 0.3.0")
        wait(lambda: (root / "update-failed.json").is_file(), 120, "rollback")
        h = wait(lambda: (lambda x: x if x and x["version"] == "0.2.0" else None)(health()), 90,
                 "v0.2.0 back")
        note = json.loads((root / "update-failed.json").read_text())
        step("broken v0.3.0 rolled back to v0.2.0 by itself",
             (root / "current.txt").read_text() == "0.2.0",
             f"{time.monotonic() - t0:.1f} s; reason: {note['reason']}")
        step("DB copy put back", note["db_restored"])
        s = wait(lambda: (lambda x: x if x["state"] == "running" else None)(server_status(sid)),
                 30, "game adopted again")
        step("fake game still running after the rollback", s["pid"] == game_pid)
        u = api("GET", "/api/update")
        step("page shows the rollback", (u.get("failure") or {}).get("to") == "0.3.0"
             and "0.3.0" in u["failed_versions"])
        step("v0.3.0 never called itself a success",
             (u.get("last_update") or {}).get("to") == "0.2.0"
             and "to 0.3.0" not in (data / "logs" / "basecamp.log").read_text(encoding="utf-8"))
        step("v0.3.0 can still be installed by hand", u["ready"]
             and u["ready_version"] == "0.3.0")

        # -- bad signature --------------------------------------------------------------
        pub.publish(zips["0.4.0"], "0.4.0")
        u = check_and_wait("0.4.0")
        step("v0.4.0 with a bad signature is refused", u["status"] == "failed"
             and "signature" in u["error"] and not (root / "versions" / "0.4.0").exists(),
             u["error"])
        h = health()
        step("still on v0.2.0", bool(h and h["version"] == "0.2.0"))
        versions = sorted(p.name for p in (root / "versions").iterdir())
        step("version folders", True, ", ".join(versions))
    except Exception as e:  # report and clean up
        step("unexpected error", False, repr(e))
    finally:
        if sid is not None:
            try:
                api("POST", f"/api/servers/{sid}/stop")
                wait(lambda: server_status(sid)["state"] == "stopped", 60, "game stopped")
            except Exception as e:
                print(f"Couldn't stop the fake game: {e}")
        basecamp = [c for c in psutil.Process(launcher.pid).children()] if launcher.poll() \
            is None else []
        for p in procs:
            p.terminate()
        for c in basecamp:
            with contextlib.suppress(psutil.Error):
                c.terminate()
        log.close()
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed")
    print(f"Launcher log: {root / 'logs' / 'launcher.log'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
