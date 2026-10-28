"""Basecamp launcher: runs the current version and swaps in updates (docs/07-updates.md).

Standard library only and deliberately small, so it almost never changes; a release that
needs a newer one says ``min_launcher`` in its manifest. The scheduled task runs::

    <root>\\python\\pythonw.exe <root>\\launcher.py --data <data folder>

Folder layout (``<root>`` is this file's folder unless ``--root`` says otherwise)::

    versions\\<v>\\       basecamp\\ package, lib\\ (vendored dependencies), migrations\\ ...
    current.txt        version to run
    previous.txt       version to roll back to
    pending.txt        written by Basecamp before it exits with code 3
    trial.txt          a version that hasn't passed its health check yet
    update-failed.json the last rollback, for Basecamp to show (R-92)
    logs\\launcher.log  and logs\\basecamp-output.log (Basecamp's stdout/stderr)

Rules:

1. Run Basecamp from ``versions\\<current>\\``.
2. Exit code 3 and ``pending.txt`` names an unpacked version: current becomes previous,
   pending becomes current, and the new one is started on trial.
3. A version on trial must answer ``/healthz`` with its own version within 60 s. If not, it
   is killed, the DB copy Basecamp made before exiting is put back, and the previous
   version starts again.
4. Any other exit: start again after 5 s, doubling up to 5 min while it keeps failing.
5. Keep the newest 3 version folders (plus current, previous and pending).

Only Basecamp's own process is ever killed. Game servers run under their own detached host
processes (D-25), so they carry on, and the version that starts next adopts them (R-93).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

LAUNCHER_VERSION = 2  # 2: sys.path set by BOOTSTRAP, not PYTHONPATH (D-123)
EXIT_UPDATE = 3
HEALTH_TIMEOUT_S = 60.0
BACKOFF_S = (5, 10, 20, 40, 80, 160, 300)
STABLE_S = 300  # a run this long resets the back-off
KEEP_VERSIONS = 3
CREATE_NO_WINDOW = 0x08000000
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
# Run with -I from the version folder. The runtime's ._pth file (and -I) make Python ignore
# PYTHONPATH, the registry and user site-packages, so Basecamp's own folders go on sys.path
# here: the version folder and its lib\, ahead of the runtime's (D-123).
BOOTSTRAP = ("import os, runpy, sys; v = os.getcwd(); "
             "sys.path[:0] = [v, os.path.join(v, 'lib')]; "
             "runpy.run_module('basecamp', run_name='__main__', alter_sys=True)")

log = logging.getLogger("launcher")


def version_key(text: str) -> tuple | None:
    """Sort key for a version folder name; None if it isn't one."""
    m = _VERSION.match(text)
    if not m:
        return None
    pre = tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                for p in m[4].split(".")) if m[4] else ()
    return (int(m[1]), int(m[2]), int(m[3]), 0 if m[4] else 1, pre)


class Launcher:
    def __init__(self, root: Path, data_dir: Path, python: str | None = None, *,
                 health_timeout_s: float = HEALTH_TIMEOUT_S,
                 backoff_s: tuple[float, ...] = BACKOFF_S, stable_s: float = STABLE_S) -> None:
        self.root = root
        self.data_dir = data_dir
        self.python = python or sys.executable
        self.health_timeout_s = health_timeout_s
        self.backoff_s = backoff_s
        self.stable_s = stable_s
        self.stop = threading.Event()
        self.proc: subprocess.Popen | None = None

    # -- small files ---------------------------------------------------------------------

    def read(self, name: str) -> str:
        try:
            return (self.root / name).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def write(self, name: str, text: str) -> None:
        tmp = self.root / f"{name}.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.root / name)

    def remove(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.root / name).unlink()

    def version_dir(self, version: str) -> Path:
        return self.root / "versions" / version

    def runnable(self, version: str) -> bool:
        return bool(version) and version_key(version) is not None and (
            self.version_dir(version) / "basecamp" / "__init__.py").is_file()

    def port(self) -> int:
        try:
            with (self.data_dir / "settings.toml").open("rb") as f:
                return int(tomllib.load(f).get("basecamp", {}).get("port", 8150))
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            return 8150

    # -- running Basecamp ----------------------------------------------------------------

    def spawn(self, version: str) -> subprocess.Popen:
        folder = self.version_dir(version)
        env = dict(os.environ)
        env.update(BASECAMP_ROOT=str(self.root), BASECAMP_DATA_DIR=str(self.data_dir),
                   BASECAMP_LAUNCHER=str(LAUNCHER_VERSION))
        for name in ("PYTHONPATH", "PYTHONHOME"):  # ignored anyway; keep them from the games
            env.pop(name, None)
        (self.root / "logs").mkdir(exist_ok=True)
        with (self.root / "logs" / "basecamp-output.log").open("ab") as out:
            log.info("Starting Basecamp %s", version)
            return subprocess.Popen([self.python, "-I", "-c", BOOTSTRAP], cwd=folder, env=env,
                                    stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)

    def health(self, proc: subprocess.Popen, version: str) -> str:
        """"" once ``/healthz`` answers with ``version``, else why not."""
        url = f"http://127.0.0.1:{self.port()}/healthz"
        deadline = time.monotonic() + self.health_timeout_s
        seen = ""
        while time.monotonic() < deadline and not self.stop.is_set():
            code = proc.poll()
            if code is not None:
                return f"it exited with code {code}"
            try:
                with urllib.request.urlopen(url, timeout=2) as res:
                    answer = json.loads(res.read(4096))
                if answer.get("ok") and answer.get("version") == version:
                    return ""
                seen = f"/healthz answered {answer}"
            except urllib.error.HTTPError as e:
                with contextlib.suppress(OSError):
                    e.read()  # an unread answer closes with a reset; Basecamp logs those
                e.close()
                seen = f"/healthz: {e}"
            except (OSError, ValueError) as e:
                seen = f"/healthz: {e}"
            self.stop.wait(1)
        return f"no healthy answer in {self.health_timeout_s:.0f} s ({seen or 'nothing'})"

    def kill(self, proc: subprocess.Popen) -> None:
        """Basecamp's process only: never the tree (the game hosts must live, R-93)."""
        if proc.poll() is None:
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(30)

    # -- updates -------------------------------------------------------------------------

    def swap(self, current: str) -> bool:
        pending = self.read("pending.txt")
        self.remove("pending.txt")
        if not pending or pending == current:
            log.warning("Exit code 3 but no new version in pending.txt; restarting %s", current)
            return False
        if not self.runnable(pending):
            log.error("pending.txt names %r, which isn't unpacked; restarting %s", pending,
                      current)
            return False
        log.info("Updating %s -> %s", current, pending)
        self.write("previous.txt", current)
        self.write("trial.txt", pending)
        self.write("current.txt", pending)
        return True

    def rollback(self, failed: str, reason: str) -> None:
        previous = self.read("previous.txt")
        self.remove("trial.txt")
        if not self.runnable(previous) or previous == failed:
            log.error("%s isn't healthy (%s) and there's nothing to roll back to", failed,
                      reason)
            return
        log.error("%s isn't healthy (%s); rolling back to %s", failed, reason, previous)
        note = {"id": datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f"), "from": previous,
                "to": failed, "reason": reason, "db_restored": False,
                "at": datetime.now(UTC).isoformat(timespec="seconds")}
        copy = self.data_dir / "db-backups" / f"before-{failed}.db"
        db = self.data_dir / "basecamp.db"
        if copy.is_file():
            try:
                tmp = db.with_name("basecamp.db.restore")
                shutil.copyfile(copy, tmp)
                for suffix in ("-wal", "-shm"):
                    with contextlib.suppress(FileNotFoundError):
                        db.with_name(db.name + suffix).unlink()
                os.replace(tmp, db)
                note["db_restored"] = True
            except OSError as e:
                log.error("Couldn't put the DB copy back: %s", e)
        else:
            log.warning("No DB copy %s; keeping the DB as it is", copy)
        self.write("update-failed.json", json.dumps(note, indent=1))
        self.write("current.txt", previous)
        self.remove("previous.txt")

    def prune(self) -> None:
        keep = {self.read(n) for n in ("current.txt", "previous.txt", "pending.txt")}
        folders = [(version_key(p.name), p) for p in (self.root / "versions").iterdir()
                   if p.is_dir() and version_key(p.name) is not None]
        folders.sort(reverse=True)
        for i, (_, path) in enumerate(folders):
            if i >= KEEP_VERSIONS and path.name not in keep:
                log.info("Removing old version %s", path.name)
                shutil.rmtree(path, ignore_errors=True)

    # -- the loop ------------------------------------------------------------------------

    def run(self) -> None:
        log.info("Launcher %s in %s, data in %s", LAUNCHER_VERSION, self.root, self.data_dir)
        failures = 0
        while not self.stop.is_set():
            version = self.read("current.txt")
            if not self.runnable(version):
                previous = self.read("previous.txt")
                if self.runnable(previous):
                    log.error("current.txt names %r, which isn't there; using %s", version,
                              previous)
                    self.write("current.txt", previous)
                    continue
                log.error("No runnable version (current.txt says %r); waiting", version)
                self.stop.wait(60)
                continue
            started = time.monotonic()
            proc = self.proc = self.spawn(version)
            if self.read("trial.txt") == version:
                reason = self.health(proc, version)
                if self.stop.is_set():
                    break
                if reason:
                    self.kill(proc)
                    self.rollback(version, reason)
                    continue
                log.info("%s is healthy", version)
                self.remove("trial.txt")
            else:
                self.remove("trial.txt")
            with contextlib.suppress(OSError):
                self.prune()
            code = self.wait(proc)
            if self.stop.is_set():
                break
            if code == EXIT_UPDATE:
                self.swap(version)
                failures = 0
                continue
            if time.monotonic() - started >= self.stable_s:
                failures = 0
            delay = self.backoff_s[min(failures, len(self.backoff_s) - 1)]
            failures += 1
            log.warning("Basecamp %s exited with code %s; starting again in %s s", version,
                        code, delay)
            self.stop.wait(delay)
        if self.proc is not None and self.proc.poll() is None:
            log.info("Launcher stopping; stopping Basecamp (game servers keep running)")
            self.kill(self.proc)

    def wait(self, proc: subprocess.Popen) -> int | None:
        while not self.stop.is_set():
            try:
                return proc.wait(1)
            except subprocess.TimeoutExpired:
                continue
        return None


def _single_instance(root: Path):  # type: ignore[no-untyped-def]
    """Hold launcher.lock for as long as this process runs; None if another holds it."""
    import msvcrt

    handle = (root / "launcher.lock").open("a+b")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Runs Basecamp and installs its updates")
    here = Path(__file__).resolve().parent
    parser.add_argument("--root", type=Path, default=here)
    parser.add_argument("--data", type=Path,
                        default=Path(os.environ.get("BASECAMP_DATA_DIR") or here / "data"))
    parser.add_argument("--health-timeout", type=float, default=HEALTH_TIMEOUT_S)
    args = parser.parse_args(argv)
    root, data = args.root.resolve(), args.data.resolve()
    (root / "logs").mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(root / "logs" / "launcher.log",
                                                   maxBytes=1024 * 1024, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    if sys.stderr is not None:  # None under pythonw.exe
        log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)
    lock = _single_instance(root)
    if lock is None:
        log.error("Another launcher is already running in %s", root)
        return 1
    launcher = Launcher(root, data, health_timeout_s=args.health_timeout)
    try:
        launcher.run()
    except KeyboardInterrupt:
        launcher.stop.set()
        if launcher.proc is not None:
            launcher.kill(launcher.proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
