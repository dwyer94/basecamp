"""Save backups: zip a server's save folder, keep the newest N, restore one (R-50..R-53).

The zip files are the record (D-35): ``<backups>\\<server id>\\YYYYMMDD-HHMMSS-<reason>.zip``,
with a JSON zip comment saying which server and folder it came from. Listing reads the
folder, so a backup copied in or deleted by hand shows up as it is.

A backup is written to a ``.tmp`` file and renamed when complete, and fails as a whole if
any file can't be read: a half backup that looks whole is worse than none. Restore needs
the server stopped (it holds the server's lock throughout), backs up the current save
first, unpacks into a new folder next to the save folder and then swaps the two.

Phase 3's update flow calls ``make(server, "pre-update")`` before it stops a server (R-51).
"""

from __future__ import annotations

import contextlib
import json
import logging
import ntpath
import os
import re
import shutil
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from basecamp import activity
from basecamp.db import Store
from basecamp.models import NotifyEvent, Server, ServerState, save_folder
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor

log = logging.getLogger(__name__)

_NAME = re.compile(r"^(\d{8})-(\d{6})-([a-z0-9-]{1,32})(?:-(\d+))?\.zip$")
_REASON = re.compile(r"^[a-z0-9-]{1,32}$")
_COMMENT_MAX = 65535

# (server, event, text): how a scheduled backup that failed is reported (Discord).
Report = Callable[[Server, NotifyEvent, str], None]


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class Backup:
    name: str
    reason: str
    created: float  # from the file name (local time), so a copied file keeps its age
    size_bytes: int
    files: int | None  # from the zip comment; None for a zip Basecamp didn't make

    def as_dict(self) -> dict:
        return {"name": self.name, "reason": self.reason, "created": self.created,
                "size_bytes": self.size_bytes, "files": self.files}


def check_name(name: str) -> None:
    """Refuse anything that isn't one of our backup file names (no paths, no '..')."""
    if not _NAME.match(name):
        raise BackupError(f"Not a backup name: {name!r}")


class Backups:
    def __init__(self, store: Store, sup: Supervisor, settings: Settings,
                 report: Report | None = None):
        self._store = store
        self._sup = sup
        self._settings = settings
        self._report = report
        self._mu = threading.Lock()
        self._locks: dict[int, threading.Lock] = {}
        self._last_try: dict[int, float] = {}  # scheduler: last attempt, kept on failure
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _lock(self, server_id: int) -> threading.Lock:
        with self._mu:
            return self._locks.setdefault(server_id, threading.Lock())

    # -- listing -------------------------------------------------------------------

    def list(self, server_id: int) -> list[Backup]:
        folder = self._settings.server_backup_dir(server_id)
        found = []
        with contextlib.suppress(FileNotFoundError):
            for path in folder.iterdir():
                match = _NAME.match(path.name)
                if not match or not path.is_file():
                    continue
                created = datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S").timestamp()
                found.append(Backup(path.name, match[3], created, path.stat().st_size,
                                    _comment(path).get("files")))
        found.sort(key=lambda b: (b.created, b.name), reverse=True)
        return found

    def path(self, server_id: int, name: str) -> Path:
        check_name(name)
        path = self._settings.server_backup_dir(server_id) / name
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    # -- making --------------------------------------------------------------------

    def make(self, server: Server, reason: str) -> Backup:
        """Zip the server's save folder now. Raises BackupError (nothing is left behind)."""
        if not _REASON.match(reason):
            raise ValueError(f"bad reason {reason!r}")
        source = save_folder(server)
        if source is None:
            raise BackupError("No save folder set for this server")
        if not source.is_dir():
            raise BackupError(f"Save folder not found: {source}")
        folder = self._settings.server_backup_dir(server.id)
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock(server.id):
            target = _free_name(folder, reason)
            tmp = target.with_name(target.name + ".tmp")
            started = time.monotonic()
            try:
                count, raw = _zip_folder(source, tmp, {
                    "basecamp": 1, "server_id": server.id, "server": server.name,
                    "save_dir": str(source), "reason": reason,
                })
                os.replace(tmp, target)
            except BaseException as e:
                tmp.unlink(missing_ok=True)
                if isinstance(e, OSError | zipfile.BadZipFile):
                    raise BackupError(f"Backup of {source} failed: {e}") from e
                raise
        size = target.stat().st_size
        log.info("%s: backup %s, %d files, %.1f MB (%.1f MB raw) in %.1f s", server.name,
                 target.name, count, size / 2**20, raw / 2**20, time.monotonic() - started)
        self._sup.console(server.id).system(
            f"Backup made: {target.name} ({count} files, {_mb(size)})")
        match = _NAME.match(target.name)
        assert match is not None
        return Backup(target.name, reason,
                      datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S").timestamp(),
                      size, count)

    def prune(self, server: Server) -> list[str]:
        """Delete all but the newest ``backup_keep`` backups (R-50). Returns what went."""
        gone = []
        for backup in self.list(server.id)[server.backup_keep:]:
            with contextlib.suppress(FileNotFoundError):
                (self._settings.server_backup_dir(server.id) / backup.name).unlink()
                gone.append(backup.name)
        if gone:
            log.info("%s: removed old backups %s", server.name, ", ".join(gone))
        return gone

    def delete(self, server_id: int, name: str) -> None:
        self.path(server_id, name).unlink()

    # -- restoring -----------------------------------------------------------------

    def restore(self, server: Server, name: str) -> Backup | None:
        """Put a backup back (R-52). The server must be stopped. The current save is backed
        up first ("pre-restore"); that backup is returned (None if there was no save)."""
        path = self.path(server.id, name)
        target = save_folder(server)
        if target is None:
            raise BackupError("No save folder set for this server")
        with self._sup.hold(server.id) as state:
            if state not in (ServerState.STOPPED, ServerState.CRASHED):
                raise BackupError(f"Stop {server.name} before restoring (it is {state})")
            safety = None
            if target.is_dir() and any(target.iterdir()):
                safety = self.make(server, "pre-restore")
            target.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            incoming = target.with_name(f"{target.name}.restoring-{stamp}")
            old = target.with_name(f"{target.name}.old-{stamp}")
            try:
                _unzip(path, incoming)
                if target.exists():
                    os.replace(target, old)
                try:
                    os.replace(incoming, target)
                except OSError:
                    if old.exists():
                        os.replace(old, target)  # put the current save back
                    raise
            except OSError as e:
                shutil.rmtree(incoming, ignore_errors=True)
                raise BackupError(f"Restore failed, the save is unchanged: {e}") from e
            except zipfile.BadZipFile as e:
                shutil.rmtree(incoming, ignore_errors=True)
                raise BackupError(f"{name} is damaged: {e}") from e
            shutil.rmtree(old, ignore_errors=True)
        note = f"Restored {name}" + (f"; the save before it is {safety.name}" if safety else "")
        self._sup.console(server.id).system(note)
        log.info("%s: %s", server.name, note)
        return safety

    # -- schedule ------------------------------------------------------------------

    def start_scheduler(self) -> None:
        self._thread = threading.Thread(target=self._schedule, name="backups", daemon=True)
        self._thread.start()

    def stop_scheduler(self) -> None:
        self._stop.set()

    def due(self, server: Server, now: float | None = None) -> bool:
        """A scheduled backup is due: the schedule is on, the server is running (its save
        only changes then), and the newest backup or last attempt is older than the
        interval."""
        if server.backup_every_h <= 0 or not server.save_dir.strip():
            return False
        if self._sup.state(server.id) is not ServerState.RUNNING:
            return False
        now = time.time() if now is None else now
        newest = self.list(server.id)
        last = max(newest[0].created if newest else 0.0, self._last_try.get(server.id, 0.0))
        return now - last >= server.backup_every_h * 3600

    def run_due(self) -> list[int]:
        done = []
        for server in self._store.list_servers():
            if not self.due(server):
                continue
            self._last_try[server.id] = time.time()
            try:
                backup = self.make(server, "scheduled")
                self.prune(server)
                done.append(server.id)
                activity.record(self._store, "backup.scheduled", server.id, backup.name)
            except BackupError as e:
                log.error("%s: scheduled backup failed: %s", server.name, e)
                activity.record(self._store, "backup.failed", server.id, str(e))
                self._sup.console(server.id).system(f"Scheduled backup failed: {e}")
                if self._report is not None:
                    self._report(server, NotifyEvent.BACKUP_FAILED,
                                 f"Backup of **{server.name}** failed: {e}")
        return done

    def _schedule(self) -> None:
        while not self._stop.wait(self._settings.backup_check_s):
            try:
                self.run_due()
            except Exception:
                log.exception("Backup scheduler")


def _free_name(folder: Path, reason: str) -> Path:
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{reason}"
    path, n = folder / f"{stem}.zip", 2
    while path.exists():
        path, n = folder / f"{stem}-{n}.zip", n + 1
    return path


def _zip_folder(source: Path, target: Path, meta: dict) -> tuple[int, int]:
    count = raw = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(source, onerror=_raise):
            dirs.sort()
            for name in sorted(files):
                path = Path(root, name)
                zf.write(path, path.relative_to(source).as_posix())
                count += 1
                raw += path.stat().st_size
        zf.comment = json.dumps({**meta, "files": count, "bytes": raw}).encode()[:_COMMENT_MAX]
    return count, raw


def _raise(e: OSError) -> None:
    raise e


def _unzip(path: Path, dest: Path) -> None:
    """Unpack into a new folder, refusing members that would land outside it."""
    dest.mkdir(parents=True)
    root = dest.resolve()
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            out = (root / info.filename).resolve()
            if not out.is_relative_to(root) or ntpath.isabs(info.filename):
                raise zipfile.BadZipFile(f"unsafe path in zip: {info.filename}")
        zf.extractall(root)


def _comment(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            data = json.loads(zf.comment or b"{}")
    except (OSError, ValueError, zipfile.BadZipFile):
        return {}
    return data if isinstance(data, dict) else {}


def _mb(size: int) -> str:
    return f"{size / 2**20:.1f} MB" if size >= 2**20 else f"{max(1, size // 1024)} KB"
