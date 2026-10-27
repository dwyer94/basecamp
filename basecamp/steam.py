"""SteamCMD installs and one-click updates (R-11, R-30..R-32; docs/02-architecture.md).

One worker thread runs one SteamCMD at a time (R-31); other installs and updates wait in a
queue. Each job is a row in ``steam_runs`` (so a failed update stays marked after a
Basecamp restart) and a log file ``logs\\steamcmd\\<server id>\\<run id>.log``. SteamCMD's
output also goes into the server's console, so the page shows it live on the console
WebSocket.

An update holds the server's action lock throughout (``Supervisor.updating``):

1. back up the save (``Backups.make(server, "pre-update")``, R-51); if that fails, stop here
   with nothing changed,
2. stop it through its own stop recipe (R-04a): Stopping, then Updating,
3. ``steamcmd +force_install_dir <dir> +login anonymous +app_update <id> [-beta b]
   [validate] +quit``; retried once when SteamCMD only updated itself,
4. start it again only if it was running before. If SteamCMD failed it isn't started: the
   run is marked failed, its output kept, and the backup kept (R-32).

A first install (R-11) runs the same command into an empty folder of a stopped server.
Discord gets "update started / finished / failed" through the notifier's ``update`` event.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import psutil

from basecamp import procs, templates
from basecamp.backups import BackupError, Backups, save_folder
from basecamp.console import Console, LineSource
from basecamp.db import SteamRun, Store
from basecamp.models import NotifyEvent, Server, ServerKind, ServerState
from basecamp.notify import Notifier
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor, SupervisorError, UpdateHold

log = logging.getLogger(__name__)

_PROGRESS = re.compile(r"progress:\s*(\d+(?:\.\d+)?)")
_SUCCESS = re.compile(r"Success! App '(\d+)' (fully installed|already up to date)", re.I)
_ERROR = re.compile(r"^\s*ERROR!\s*(.+)$")
# What SteamCMD prints when it has only updated itself and exits (code 7 on Windows).
_SELF_UPDATE = ("Update complete, launching", "Restarting steamcmd by request")
_SELF_UPDATE_CODE = 7
_POKE_EVERY_S = 1.0  # progress pushes to the page at most this often
_LOG_TAIL_BYTES = 64 * 1024


class SteamError(Exception):
    pass


class Kind(StrEnum):
    INSTALL = "install"
    UPDATE = "update"


class Phase(StrEnum):
    QUEUED = "queued"
    BACKUP = "backup"
    STOPPING = "stopping"
    STEAMCMD = "steamcmd"
    STARTING = "starting"


@dataclass
class Job:
    run_id: int
    server_id: int
    kind: Kind
    who: str
    phase: Phase = Phase.QUEUED
    progress: float | None = None  # percent, from SteamCMD's "progress:" lines
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen[bytes] | None = None

    def as_dict(self, position: int) -> dict:
        return {"run_id": self.run_id, "kind": self.kind.value, "phase": self.phase.value,
                "progress": self.progress, "position": position, "who": self.who,
                "queued_at": self.queued_at, "started_at": self.started_at,
                "cancelling": self.cancel.is_set()}


@dataclass
class _Outcome:
    code: int | None = None
    success: str = ""  # the "Success! ..." line
    error: str = ""  # the last "ERROR! ..." line
    self_update: bool = False
    timed_out: bool = False
    cancelled: bool = False


def installed_build(server: Server) -> str | None:
    """The build ID in ``<install>\\steamapps\\appmanifest_<appid>.acf``, if it's there."""
    if not server.install_dir.strip() or not server.steam_app_id:
        return None
    for manifest in templates.read_manifests(Path(server.install_dir)):
        if manifest.app_id == server.steam_app_id:
            return manifest.build_id or None
    return None


def steam_command(settings: Settings, server: Server) -> list[str]:
    exe = settings.steamcmd_exe
    cmd = [str(exe)]
    if exe.suffix.lower() == ".py":  # tools/fake_steamcmd.py on the dev PC and in tests
        cmd = [str(procs.host_python()), "-I", str(exe)]
    # +force_install_dir has to come before +login.
    cmd += ["+force_install_dir", server.install_dir, "+login", "anonymous",
            "+app_update", str(server.steam_app_id)]
    if server.steam_beta.strip():
        cmd += ["-beta", server.steam_beta.strip()]
    if server.steam_validate:
        cmd.append("validate")
    cmd.append("+quit")
    return cmd


def _check_steam(server: Server) -> None:
    if server.kind is not ServerKind.STEAM:
        raise SteamError("Only Steam servers can be installed or updated (R-33)")
    if not server.steam_app_id:
        raise SteamError("Set the Steam app ID first")
    if not server.install_dir.strip():
        raise SteamError("Set the install folder first")


def _check_empty(folder: Path) -> None:
    """A first install goes into a missing or empty folder only (R-11)."""
    if folder.is_file():
        raise SteamError(f"{folder} is a file")
    if folder.is_dir() and any(folder.iterdir()):
        raise SteamError(f"{folder} isn't empty. Use Update for an existing install, or "
                         "pick an empty folder")


class SteamRunner:
    def __init__(self, store: Store, sup: Supervisor, settings: Settings, backups: Backups,
                 notifier: Notifier | None = None):
        self._store = store
        self._sup = sup
        self._settings = settings
        self._backups = backups
        self._notifier = notifier
        self._mu = threading.Lock()
        self._wake = threading.Condition(self._mu)
        self._queue: list[Job] = []  # [0] is running while self._running is set
        self._running: Job | None = None
        self._listeners: list[Callable[[], None]] = []
        self._closed = False
        stale = store.end_unfinished_steam_runs()
        if stale:
            log.warning("Marked %d SteamCMD run(s) from before the restart interrupted", stale)
        self._thread = threading.Thread(target=self._work, name="steamcmd", daemon=True)
        self._thread.start()

    # -- queries ----------------------------------------------------------------

    def add_listener(self, fn: Callable[[], None]) -> None:
        """Called when a job is queued, moves on, reports progress or ends."""
        self._listeners.append(fn)

    def job(self, server_id: int) -> Job | None:
        with self._mu:
            return self._job_locked(server_id)

    def busy(self, server_id: int) -> bool:
        """An install or update has begun on it (queued doesn't count: the server runs
        as normal until its turn)."""
        with self._mu:
            return self._running is not None and self._running.server_id == server_id

    def jobs(self) -> list[dict]:
        with self._mu:
            return [self._as_dict(job) for job in self._all()]

    def view(self, server: Server) -> dict | None:
        """The ``steam`` part of the admin view (None for a non-Steam server)."""
        if server.kind is not ServerKind.STEAM:
            return None
        with self._mu:
            job = self._job_locked(server.id)
            job_view = self._as_dict(job) if job is not None else None
        last = self._store.last_steam_run(server.id)
        install = Path(server.install_dir) if server.install_dir.strip() else None
        exe = self._settings.steamcmd_exe
        return {
            "build_id": installed_build(server),
            "installed": bool(install and install.is_dir() and any(install.iterdir())),
            "job": job_view,
            "last": _run_view(last) if last else None,
            # R-32: shown until a later install or update succeeds.
            "failed": bool(last and last.result in ("failed", "interrupted")),
            "steamcmd": str(exe),
            "steamcmd_found": exe.is_file(),
        }

    def log_text(self, server_id: int, run_id: int) -> str:
        path = self._settings.steam_log_dir(server_id) / f"{run_id}.log"
        with path.open("rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - _LOG_TAIL_BYTES))
            return f.read().decode("utf-8", errors="replace")

    # -- actions ----------------------------------------------------------------

    def enqueue(self, server_id: int, kind: Kind, who: str) -> Job:
        """Queue an install or update. Raises KeyError (no server) or SteamError."""
        server = self._store.get_server(server_id)
        _check_steam(server)
        state = self._sup.state(server_id)
        if state is ServerState.UPDATING:
            raise SteamError(f"{server.name} is already updating")
        if kind is Kind.INSTALL:
            if state not in (ServerState.STOPPED, ServerState.CRASHED):
                raise SteamError(f"Stop {server.name} before installing")
            _check_empty(Path(server.install_dir))
        elif not Path(server.install_dir).is_dir():
            raise SteamError(f"Install folder not found: {server.install_dir}. "
                             "Use Install for a new server")
        with self._mu:
            if self._closed:
                raise SteamError("Basecamp is shutting down")
            if self._job_locked(server_id) is not None:
                raise SteamError(f"{server.name} already has an install or update queued")
            run_id = self._store.add_steam_run(server_id, kind.value, who)
            job = Job(run_id, server_id, kind, who)
            self._queue.append(job)
            ahead = len(self._queue) - 1
            self._wake.notify_all()
        console = self._sup.console(server_id)
        what = "Install" if kind is Kind.INSTALL else "Update"
        console.system(f"{what} queued" + (f" behind {ahead} other run(s)" if ahead else ""))
        log.info("%s: %s queued by %s (run %d)", server.name, kind, who, run_id)
        self._changed()
        return job

    def cancel(self, server_id: int) -> bool:
        """Take a queued job off the queue, or stop a running one. A job that is past
        SteamCMD (starting again) can't be cancelled."""
        with self._mu:
            job = self._job_locked(server_id)
            if job is None:
                return False
            if job is not self._running:
                self._queue.remove(job)
                queued = True
            else:
                queued = False
                if job.phase is Phase.STARTING:
                    return False
                job.cancel.set()
                proc = job.proc
        if queued:
            self._store.update_steam_run(job.run_id, result="cancelled", ended_at=True,
                                         message="Cancelled before it began")
            self._sup.console(server_id).system("Queued run cancelled")
        else:
            self._sup.console(server_id).system("Cancelling the SteamCMD run")
            if proc is not None:
                _kill(proc)
        self._changed()
        return True

    def close(self) -> None:
        """Stop the worker. A running SteamCMD is killed, and its run marked interrupted
        on the next start (it can't finish without Basecamp to start the server again)."""
        with self._mu:
            self._closed = True
            running = self._running
            self._wake.notify_all()
        if running is not None:
            running.cancel.set()
            if running.proc is not None:
                _kill(running.proc)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """For tests: wait until nothing is queued or running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._mu:
                if not self._queue:
                    return True
            time.sleep(0.05)
        return False

    # -- worker -------------------------------------------------------------------

    def _work(self) -> None:
        while True:
            with self._mu:
                while not self._queue and not self._closed:
                    self._wake.wait()
                if self._closed:
                    return
                job = self._running = self._queue[0]
            try:
                self._run_job(job)
            except Exception:
                log.exception("SteamCMD run %d failed", job.run_id)
                self._store.update_steam_run(job.run_id, result="failed", ended_at=True,
                                             message="Basecamp error; see basecamp.log")
                self._sup.console(job.server_id).system(
                    "The run failed with a Basecamp error; see basecamp.log")
            finally:
                with self._mu:
                    self._running = None
                    if job in self._queue:
                        self._queue.remove(job)
                self._changed()

    def _run_job(self, job: Job) -> None:
        try:
            server = self._store.get_server(job.server_id)
        except KeyError:
            self._store.update_steam_run(job.run_id, result="cancelled", ended_at=True,
                                         message="The server was deleted")
            return
        job.started_at = time.time()
        self._store.update_steam_run(job.run_id, result="running", started_at=True)
        self._changed()
        console = self._sup.console(server.id)
        updating = job.kind is Kind.UPDATE
        what = "Update" if updating else "Install"
        with self._sup.updating(server.id) as hold:
            self._store.update_steam_run(job.run_id, was_running=int(hold.was_running))
            if not updating and hold.state_before not in (ServerState.STOPPED,
                                                          ServerState.CRASHED):
                self._end(job, server, "cancelled", f"{server.name} was started meanwhile; "
                          "not installed")
                return
            build_before = installed_build(server) or ""
            self._store.update_steam_run(job.run_id, build_before=build_before)
            console.system(f"{what} starting" + (f" (build {build_before})"
                                                 if build_before else ""))
            self._notify(server, f":arrows_counterclockwise: Updating **{server.name}**"
                         + (", back soon" if hold.was_running else "") if updating
                         else f":inbox_tray: Installing **{server.name}**")
            backup = ""
            if updating:
                job.phase = Phase.BACKUP
                self._changed()
                try:
                    backup = self._backup(server, console)
                except BackupError as e:
                    self._end(job, server, "failed",
                              f"Pre-update backup failed, so nothing was changed: {e}",
                              notify=True)
                    return
                self._store.update_steam_run(job.run_id, backup=backup)
            else:
                try:
                    _check_empty(Path(server.install_dir))
                except SteamError as e:
                    self._end(job, server, "cancelled", str(e), notify=True)
                    return
            if job.cancel.is_set():
                self._end(job, server, "cancelled", "Cancelled before anything changed")
                return
            job.phase = Phase.STOPPING
            self._changed()
            try:
                hold.stop()
            except SupervisorError as e:
                self._end(job, server, "failed", f"Couldn't stop it for the update: {e}",
                          notify=True)
                return
            if job.cancel.is_set():
                self._end(job, server, "cancelled", "Cancelled before SteamCMD ran")
                self._start_again(job, server, hold)
                return
            job.phase = Phase.STEAMCMD
            self._changed()
            ok, message = self._steamcmd(job, server, console)
            if not ok:
                kept = f" The save backup {backup} is kept." if backup else ""
                self._end(job, server, "failed",
                          message + (" Not started on half-updated files." if updating
                                     and hold.was_running else "") + kept, notify=True)
                return
            build_after = installed_build(server) or ""
            self._store.update_steam_run(job.run_id, build_after=build_after)
            same = updating and build_after == build_before and "up to date" in message
            done = (f"{server.name} was already up to date" if same
                    else f"{server.name} {'updated' if updating else 'installed'}"
                    + (f" to build {build_after}" if build_after else ""))
            if updating and hold.was_running:
                done += "; starting it again"
            self._end(job, server, "ok", done, notify=True)
            self._start_again(job, server, hold)

    def _backup(self, server: Server, console: Console) -> str:
        folder = save_folder(server)
        if folder is None:
            console.system("No save folder set for this server, so no backup was made")
            return ""
        if not folder.is_dir():
            console.system(f"Save folder {folder} doesn't exist yet, so no backup was made")
            return ""
        backup = self._backups.make(server, "pre-update")
        self._backups.prune(server)
        return backup.name

    def _start_again(self, job: Job, server: Server, hold: UpdateHold) -> None:
        if not hold.was_running:
            return
        job.phase = Phase.STARTING
        self._changed()
        try:
            hold.start()
            self._store.update_steam_run(job.run_id, started_again=1)
        except SupervisorError as e:
            log.error("%s: start after the update failed: %s", server.name, e)
            self._sup.console(server.id).system(f"Start after the update failed: {e}")
            self._notify(server, f":warning: **{server.name}** couldn't be started after "
                         f"the update: {e}")

    def _end(self, job: Job, server: Server, result: str, message: str, *,
             notify: bool = False) -> None:
        self._store.update_steam_run(job.run_id, result=result, message=message,
                                     ended_at=True)
        level = logging.INFO if result in ("ok", "cancelled") else logging.ERROR
        log.log(level, "%s: %s %s: %s", server.name, job.kind, result, message)
        self._sup.console(server.id).system(
            message if result == "ok" else
            f"{'Update' if job.kind is Kind.UPDATE else 'Install'} {result}: {message}")
        if notify:
            icon = ":white_check_mark:" if result == "ok" else ":x:"
            what = "Update" if job.kind is Kind.UPDATE else "Install"
            self._notify(server, f"{icon} {message}" if result == "ok"
                         else f"{icon} {what} of **{server.name}** {result}: {message}")

    # -- running SteamCMD -------------------------------------------------------

    def _steamcmd(self, job: Job, server: Server, console: Console) -> tuple[bool, str]:
        exe = self._settings.steamcmd_exe
        if not exe.is_file():
            return False, (f"SteamCMD not found at {exe}. Set steamcmd_path in the data "
                           "folder's settings.toml.")
        Path(server.install_dir).mkdir(parents=True, exist_ok=True)
        cmd = steam_command(self._settings, server)
        log_dir = self._settings.steam_log_dir(server.id)
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / f"{job.run_id}.log").open("a", encoding="utf-8") as log_file:
            log_file.write(f"# {subprocess.list2cmdline(cmd)}\n")
            outcome = _Outcome()
            for attempt in (1, 2):
                outcome = self._run_once(job, cmd, exe.parent, console, log_file)
                if outcome.cancelled:
                    return False, "Cancelled while SteamCMD ran; the files may be half updated."
                if outcome.timed_out:
                    minutes = self._settings.steamcmd_timeout_s / 60
                    return False, f"SteamCMD took longer than {minutes:g} min and was stopped."
                if outcome.success and outcome.code == 0:
                    return True, outcome.success
                if (attempt == 1 and not outcome.success
                        and (outcome.self_update or outcome.code == _SELF_UPDATE_CODE)):
                    note = "SteamCMD updated itself; running it again"
                    console.system(note)
                    log_file.write(f"# {note}\n")
                    continue
                break
        if outcome.error:
            return False, f"SteamCMD: {outcome.error} (exit code {outcome.code})."
        return False, f"SteamCMD exited with code {outcome.code} without saying Success."

    def _run_once(self, job: Job, cmd: list[str], cwd: Path, console: Console,
                  log_file) -> _Outcome:  # type: ignore[no-untyped-def]
        outcome = _Outcome()
        lines: queue.Queue[str | None] = queue.Queue()
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as e:
            outcome.error = f"couldn't run it: {e}"
            return outcome
        with self._mu:
            job.proc = proc
        if job.cancel.is_set():  # cancelled while it was being started
            _kill(proc)
        reader = threading.Thread(target=_read_lines, args=(proc, lines),
                                  name=f"steamcmd out {job.run_id}", daemon=True)
        reader.start()
        deadline = time.monotonic() + self._settings.steamcmd_timeout_s
        last_poke = 0.0
        poked = job.progress
        exited_at: float | None = None
        eof = False
        while not eof:
            if exited_at is None and proc.poll() is not None:
                exited_at = time.monotonic()
            elif exited_at is not None and time.monotonic() - exited_at > 5:
                break  # it exited but something it started still holds the pipe
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                line = ""
            if line is None:
                eof = True
            elif line:
                console.write(line, LineSource.STEAM)
                log_file.write(line + "\n")
                if (m := _SUCCESS.search(line)) is not None:
                    outcome.success = line.strip()
                elif (m := _ERROR.match(line)) is not None:
                    outcome.error = m[1].strip()
                elif any(marker in line for marker in _SELF_UPDATE):
                    outcome.self_update = True
                if (m := _PROGRESS.search(line)) is not None:
                    job.progress = min(100.0, float(m[1]))
            if job.progress != poked and time.monotonic() - last_poke >= _POKE_EVERY_S:
                last_poke, poked = time.monotonic(), job.progress
                self._changed()
            if job.cancel.is_set() and not outcome.cancelled:
                outcome.cancelled = True
                _kill(proc)
            if time.monotonic() > deadline and not outcome.timed_out:
                outcome.timed_out = True
                console.system("SteamCMD is taking too long; stopping it")
                _kill(proc)
        try:
            outcome.code = proc.wait(10)
        except subprocess.TimeoutExpired:
            _kill(proc)
        log_file.write(f"# exit code {outcome.code}\n")
        log_file.flush()
        with self._mu:
            job.proc = None
        return outcome

    # -- helpers ----------------------------------------------------------------

    def _all(self) -> list[Job]:
        return list(self._queue)

    def _job_locked(self, server_id: int) -> Job | None:
        return next((j for j in self._queue if j.server_id == server_id), None)

    def _as_dict(self, job: Job) -> dict:
        return job.as_dict(self._queue.index(job))  # 0 = running (or next)

    def _changed(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                log.exception("SteamCMD listener failed")

    def _notify(self, server: Server, text: str) -> None:
        if self._notifier is not None:
            self._notifier.server_event(server, NotifyEvent.UPDATE, text)


def _run_view(run: SteamRun) -> dict:
    return {"run_id": run.id, "kind": run.kind, "result": run.result, "message": run.message,
            "who": run.who, "build_before": run.build_before, "build_after": run.build_after,
            "backup": run.backup, "queued_at": run.queued_at, "started_at": run.started_at,
            "ended_at": run.ended_at}


def _read_lines(proc: subprocess.Popen[bytes], out: queue.Queue[str | None]) -> None:
    """SteamCMD ends some lines with a bare \\r; split on either."""
    assert proc.stdout is not None
    pending = b""
    try:
        while chunk := proc.stdout.read1(4096):
            pending += chunk
            *done, pending = re.split(rb"\r\n|\r|\n", pending)
            for raw in done:
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    out.put(text)
    except (OSError, ValueError):
        pass
    if pending.strip():
        out.put(pending.decode("utf-8", errors="replace").rstrip())
    out.put(None)


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill SteamCMD and anything it started (it restarts itself after a self-update)."""
    with contextlib.suppress(psutil.Error, OSError):
        procs.kill_tree(psutil.Process(proc.pid))
