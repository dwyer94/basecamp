"""Game server lifecycle: start, graceful stop, kill, restart, the state machine, adoption
and start-on-boot (R-02..R-04a, R-20..R-23; docs/02-architecture.md).

Every server runs under its own host process (basecamp/host.py), which owns the game's
pipes and console log and outlives Basecamp. The supervisor starts hosts, talks to them
over their control channel, mirrors their console lines, and reattaches to them after a
Basecamp restart. If a host is gone but its game still runs, the game is adopted bare:
tracked by PID, stopped with the winctrl Ctrl+Break/Ctrl+C helper or a kill, no console.

Threads, not asyncio (D-16): per running server a watcher thread (Starting -> Running,
end of run) and a stream thread (the host's console events). Actions on one server are
serialised by that server's lock; state changes go through ``_set_state`` under one
supervisor-wide lock, which also rewrites ``state.json``.

Seams for later phases:
- ``add_listener`` gets every state change (status WebSocket, Discord, crash restart).
- ``remote`` (basecamp/remote.py) sends RCON and REST stop steps, warnings and console
  commands. A failure there is noted in the console and the stop goes on (D-96).
- An unexpected exit ends in Crashed; basecamp/crashes.py restarts it from a listener (R-05).
- ``hold`` keeps actions off a server while a restore runs.
- ``updating`` holds it for a SteamCMD install or update (basecamp/steam.py): stop through
  the server's own stop recipe, Updating while SteamCMD runs, then start again if asked.

A stop, restart or update that Basecamp starts can warn players first (R-25, D-61): the
server's ``stop_warning`` command goes out at each of its minutes before the stop, while the
action holds the server's lock and the server stays Running. ``stop_now`` skips the rest of
the countdown; ``kill`` ends it and the action with it. Crash restarts never warn.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from basecamp import __version__, host, procs
from basecamp.console import Console, FileTailer, LineSource
from basecamp.db import Store
from basecamp.hostclient import HostClient, HostError
from basecamp.models import (
    ACTIVE_STATES,
    LogSource,
    Server,
    ServerState,
    StopAction,
    StopStep,
    save_folder,
)
from basecamp.remote import RemoteConsoles
from basecamp.settings import Settings

log = logging.getLogger(__name__)

Listener = Callable[[int, ServerState, ServerState], None]
ChangeListener = Callable[[int], None]


class SupervisorError(Exception):
    pass


class InvalidState(SupervisorError):
    pass


class PortInUse(SupervisorError):
    def __init__(self, ports: list[str]):
        super().__init__(f"Port already in use: {', '.join(ports)}")
        self.ports = ports


class NoConsole(SupervisorError):
    pass


@dataclass
class StartResult:
    pid: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class StopResult:
    graceful: bool  # exited during the recipe (or wasn't running)
    killed: bool  # had to be killed
    ended_by: str  # the step that ended it: "stdin", "ctrl_break", "kill", "not running", ...


@dataclass
class ServerStatus:
    id: int
    state: ServerState
    pid: int | None
    started_at: float | None
    uptime_s: float | None
    adopted: bool  # attached after a Basecamp restart
    has_console: bool  # its host is up: output and stdin commands work
    rss_bytes: int | None
    last_exit_code: int | None
    warning: dict | None = None  # a stop countdown: {"kind", "stop_at"} (R-25)


@dataclass
class _Countdown:
    """A stop warning counting down (R-25)."""

    kind: str  # stop, restart, update
    stop_at: float  # epoch seconds
    skip: threading.Event = field(default_factory=threading.Event)  # Stop now, or Kill
    killed: bool = False

    def as_dict(self) -> dict:
        return {"kind": self.kind, "stop_at": self.stop_at}


_SAVE_POLL_S = 1.0  # wait_save: how often the save folder is looked at
_SAVE_SETTLE_S = 3.0  # and how long it must stay unchanged after a save before going on
_SAVE_SETTLE_MAX_S = 30.0  # (but no longer than this)
_CONSOLE_EVENTS = {StopAction.CTRL_BREAK: ("Ctrl+Break", "break"),
                   StopAction.CTRL_C: ("Ctrl+C", "c")}
_WARN_VERB = {"stop": "Stopping", "restart": "Restarting", "update": "Updating"}


class _Run:
    """One run of a server: its host (if any) and the game process."""

    def __init__(self, *, client: HostClient | None, host_proc: psutil.Process | None,
                 started_at: float, adopted: bool):
        self.host = client
        self.host_proc = host_proc
        self.host_info = procs.ProcInfo.of(host_proc) if host_proc is not None else None
        self.game: psutil.Process | None = None
        self.game_info: procs.ProcInfo | None = None
        self.started_at = started_at
        self.adopted = adopted
        self.ready = threading.Event()
        self.exited = threading.Event()
        self.stop_requested = False  # asked by this Basecamp
        self.for_update = False  # stopped for an update: the exit ends in Updating
        self.killed = False
        self.exit_code: int | None = None
        self.exit_expected = False  # the host says the exit was asked for
        self.host_seq = 0  # last host line seen, for re-attaching
        self.host_exit: dict | None = None
        self.stream_done = threading.Event()
        self.tailer: FileTailer | None = None
        self.unsubscribe: Callable[[], None] | None = None
        self.detached = False

    def set_game(self, pid: int | None) -> None:
        if pid is None:
            return
        try:
            proc = psutil.Process(pid)
            info = procs.ProcInfo.of(proc)
        except (psutil.Error, OSError):
            return  # already gone; the exit is on its way
        self.game, self.game_info = proc, info

    @property
    def pid(self) -> int | None:
        return self.game_info.pid if self.game_info else None


class _Slot:
    def __init__(self, console: Console):
        self.lock = threading.Lock()  # serialises start/stop/restart for one server
        self.state = ServerState.STOPPED
        self.run: _Run | None = None
        self.console = console
        self.last_exit_code: int | None = None
        self.countdown: _Countdown | None = None


def install_host_script(settings: Settings) -> Path:
    """Copy host.py into the data folder under a content-hash name, so running hosts never
    depend on a Basecamp version folder (the launcher prunes old ones, docs/07)."""
    source = Path(host.__file__)
    data = source.read_bytes()
    target = settings.hosts_dir / "bin" / f"host-{hashlib.sha256(data).hexdigest()[:12]}.py"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
    return target


class Supervisor:
    def __init__(self, store: Store, settings: Settings, *,
                 remote: RemoteConsoles | None = None):
        self._store = store
        self._settings = settings
        self.remote = remote or RemoteConsoles(settings)
        self._mu = threading.RLock()
        self._slots: dict[int, _Slot] = {}
        self._listeners: list[Listener] = []
        self._change_listeners: list[ChangeListener] = []
        settings.ensure_dirs()
        self._host_script = install_host_script(settings)
        self._host_python = procs.host_python()

    # -- queries ----------------------------------------------------------------

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def add_change_listener(self, fn: ChangeListener) -> None:
        """Called with the server id when something the page shows changes other than its
        state (a stop countdown starting or ending)."""
        self._change_listeners.append(fn)

    def state(self, server_id: int) -> ServerState:
        return self._slot(server_id).state

    def console(self, server_id: int) -> Console:
        return self._slot(server_id).console

    def status(self, server_id: int) -> ServerStatus:
        slot = self._slot(server_id)
        with self._mu:
            run, state, countdown = slot.run, slot.state, slot.countdown
        rss = None
        if run is not None and run.game is not None:
            with contextlib.suppress(psutil.Error):
                rss = run.game.memory_info().rss
        return ServerStatus(
            id=server_id,
            state=state,
            pid=run.pid if run else None,
            started_at=run.started_at if run else None,
            uptime_s=time.time() - run.started_at if run else None,
            adopted=bool(run and run.adopted),
            has_console=bool(run and run.host is not None),
            rss_bytes=rss,
            last_exit_code=slot.last_exit_code,
            warning=countdown.as_dict() if countdown else None,
        )

    # -- actions ----------------------------------------------------------------

    def start(self, server_id: int) -> StartResult:
        server = self._store.get_server(server_id)
        slot = self._slot(server_id)
        if slot.state is ServerState.UPDATING:  # don't wait out a SteamCMD run for the lock
            raise InvalidState(f"{server.name} is updating")
        if slot.countdown is not None:  # nor a stop warning's countdown
            raise InvalidState(f"{server.name} is about to stop")
        with slot.lock:
            return self._start_locked(server, slot)

    def stop(self, server_id: int, *, warn: bool = False) -> StopResult:
        """Graceful stop. ``warn``: send the server's stop warning first (R-25)."""
        server = self._store.get_server(server_id)
        slot = self._slot(server_id)
        with slot.lock:
            if warn and not self._warn(server, slot, "stop"):
                return StopResult(graceful=False, killed=True, ended_by="kill")
            return self._stop_locked(server, slot)

    def restart(self, server_id: int, *, warn: bool = False) -> StartResult | None:
        """Stop, then start. None if it was killed during the stop warning: a kill means
        down, so it isn't started again."""
        server = self._store.get_server(server_id)
        slot = self._slot(server_id)
        with slot.lock:
            if warn and not self._warn(server, slot, "restart"):
                return None
            self._stop_locked(server, slot)
            return self._start_locked(server, slot)

    def counting_down(self, server_id: int) -> bool:
        return self._slot(server_id).countdown is not None

    def stop_now(self, server_id: int) -> bool:
        """Skip the rest of a stop warning's countdown: the stop (restart, update) goes
        ahead at once. False if none is counting down."""
        countdown = self._slot(server_id).countdown
        if countdown is None:
            return False
        countdown.skip.set()
        return True

    def kill(self, server_id: int) -> StopResult:
        """Kill at once, no recipe. Doesn't wait for the server's lock, so it also ends a
        stop that is stuck waiting on a recipe step, or a stop warning's countdown."""
        slot = self._slot(server_id)
        countdown = slot.countdown
        if countdown is not None:
            countdown.killed = True
            countdown.skip.set()
        run = slot.run
        if run is None:
            return StopResult(graceful=True, killed=False, ended_by="not running")
        run.stop_requested = True
        self._set_state(server_id, ServerState.STOPPING)
        self._note(slot, run, "Killing")
        self._kill_run(server_id, run)
        return StopResult(graceful=False, killed=True, ended_by="kill")

    @contextlib.contextmanager
    def hold(self, server_id: int) -> Iterator[ServerState]:
        """Hold the server's action lock, so no start, stop or restart runs meanwhile (a
        restore, later an update). Yields the state at the time the lock was taken."""
        slot = self._slot(server_id)
        with slot.lock:
            yield slot.state

    @contextlib.contextmanager
    def updating(self, server_id: int) -> Iterator[UpdateHold]:
        """Hold the server's action lock for a SteamCMD install or update. The hold stops
        the server through its own stop recipe (R-04a) and keeps it Updating; when the block
        ends it is Stopped, unless the block started it again."""
        server = self._store.get_server(server_id)
        slot = self._slot(server_id)
        with slot.lock:
            hold = UpdateHold(self, server, slot)
            try:
                yield hold
            finally:
                if slot.state is ServerState.UPDATING:
                    self._set_state(server_id, ServerState.STOPPED)

    def stop_all(self) -> dict[int, StopResult]:
        with self._mu:
            ids = [sid for sid, slot in self._slots.items() if slot.run is not None]
        return {sid: self.stop(sid) for sid in ids}

    def send_command(self, server_id: int, text: str, channel: str = "stdin") -> None:
        """Type a command into the server's console (R-23): through its host's stdin, or
        over RCON or REST with the answer shown in the console."""
        slot = self._slot(server_id)
        run = slot.run
        if run is None:
            raise InvalidState("Server isn't running")
        if channel != "stdin":
            server = self._store.get_server(server_id)
            self._note(slot, run, f"({channel}) {text}")
            try:
                answer = self.remote.send(server, channel, text)
            except Exception as e:
                self._note(slot, run, f"{channel.upper()} '{text}' failed: {e}")
                raise SupervisorError(f"{channel.upper()} failed: {e}") from None
            for line in (answer.splitlines() or ["(no answer)"])[:200]:
                self._note(slot, run, f"  {line}")
            return
        if run.host is None:
            raise NoConsole("No console: the server's host is gone; restart to get it back")
        try:
            run.host.send_line(text)  # the host echoes it into the console
        except HostError as e:
            raise SupervisorError(f"Couldn't send the command: {e}") from e

    # -- start ------------------------------------------------------------------

    def _start_locked(self, server: Server, slot: _Slot) -> StartResult:
        if slot.state not in (ServerState.STOPPED, ServerState.CRASHED):
            raise InvalidState(f"{server.name} is {slot.state}")
        exe = Path(server.exe_path)
        if not exe.is_file():
            raise SupervisorError(f"Exe not found: {exe}")
        busy = self._busy_ports(server)
        if busy:
            slot.console.system(f"Not started: port in use ({', '.join(busy)})")
            raise PortInUse(busy)
        warnings = self._ram_warnings(server)
        for warning in warnings:
            log.warning("%s: %s", server.name, warning)
            slot.console.system(f"Warning: {warning}")

        cwd = Path(server.working_dir) if server.working_dir else exe.parent
        cmdline = subprocess.list2cmdline([str(exe)])
        if server.args.strip():
            cmdline += " " + server.args.strip()
        log_file = ""
        if server.log_source is LogSource.FILE:
            log_file = str(cwd / server.log_file)  # an absolute log_file wins in the join

        host_dir = self._settings.host_dir(server.id)
        host_dir.mkdir(parents=True, exist_ok=True)
        for name in ("host.json", "exit.json"):
            (host_dir / name).unlink(missing_ok=True)
        token = secrets.token_hex(16)
        spec = {
            "protocol": host.PROTOCOL,
            "basecamp_version": __version__,
            "server_id": server.id,
            "name": server.name,
            "cmdline": cmdline,
            "cwd": str(cwd),
            "log_file": log_file,
            "real_exe_name": server.real_exe_name.strip(),
            "real_exe_timeout_s": self._settings.real_exe_timeout_s,
            "console_log": str(slot.console.log_path),
            "console_log_bytes": self._settings.console_log_bytes,
            "console_log_backups": self._settings.console_log_backups,
            "ring_lines": self._settings.console_lines,
            "host_dir": str(host_dir),
            "token": token,
        }
        spec_path = host_dir / "spec.json"
        host.write_json(spec_path, spec)
        slot.console.close()  # the host owns console.log from here on
        proc = procs.spawn_host(self._host_python, self._host_script, spec_path)
        info = self._wait_for_host(host_dir, proc)
        if "error" in info:
            slot.console.system(f"Not started: {info['error']}")
            raise SupervisorError(f"{server.name}: {info['error']}")

        client = HostClient(int(info["port"]), token)
        try:
            client.hello()
            host_proc = psutil.Process(int(info["host_pid"]))
        except (HostError, OSError, psutil.Error) as e:
            raise SupervisorError(f"{server.name}: host didn't answer: {e}") from e
        run = _Run(client=client, host_proc=host_proc, started_at=time.time(), adopted=False)
        run.set_game(int(info["game_pid"]))
        log.info("Started %s under host pid %s: game pid %s, %s", server.name, host_proc.pid,
                 info["game_pid"], cmdline)
        with self._mu:
            slot.run = run
        self._watch_ready(server, slot, run)
        self._start_stream(server, slot, run)
        self._set_state(server.id, ServerState.STARTING)
        self._start_watcher(server, slot, run)
        return StartResult(pid=int(info["game_pid"]), warnings=warnings)

    def _wait_for_host(self, host_dir: Path, proc: subprocess.Popen[bytes]) -> dict:
        deadline = time.monotonic() + self._settings.host_start_timeout_s
        path = host_dir / "host.json"
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError, ValueError):
                return json.loads(path.read_text(encoding="utf-8"))
            if proc.poll() is not None:
                with contextlib.suppress(OSError, ValueError):
                    return json.loads(path.read_text(encoding="utf-8"))
                return {"error": f"host exited at once (code {proc.returncode}); "
                                 f"see {host_dir / 'host.log'}"}
            time.sleep(0.05)
        with contextlib.suppress(psutil.Error):
            procs.kill_tree(psutil.Process(proc.pid))
        return {"error": "host didn't start in time"}

    def _watch_ready(self, server: Server, slot: _Slot, run: _Run) -> None:
        if not server.ready_pattern:
            return
        pattern = server.ready_pattern

        def check_ready(line):  # type: ignore[no-untyped-def]
            if line.source is LineSource.OUT and pattern in line.text:
                run.ready.set()

        run.unsubscribe = slot.console.subscribe(check_ready)

    def _busy_ports(self, server: Server) -> list[str]:
        wanted = {(p.port, p.protocol) for p in server.ports}
        busy: set[tuple[int, str]] = set()
        # Our own active servers first: a game may not have bound its port yet.
        with self._mu:
            others = [sid for sid, s in self._slots.items()
                      if sid != server.id and s.state in ACTIVE_STATES]
        for other_id in others:
            try:
                other = self._store.get_server(other_id)
            except KeyError:
                continue
            for p in other.ports:
                if (p.port, p.protocol) in wanted:
                    busy.add((p.port, p.protocol))
        for p in procs.busy_ports(server.ports):
            busy.add((p.port, p.protocol))
        return [f"{proto}/{port}" for port, proto in sorted(busy)]

    def _ram_warnings(self, server: Server) -> list[str]:
        need = max(self._settings.low_ram_mb, server.min_free_ram_mb)
        free = procs.free_ram_mb()
        if free < need:
            return [f"Low free RAM: {free} MB free, want {need} MB"]
        return []

    # -- watching ---------------------------------------------------------------

    def _start_stream(self, server: Server, slot: _Slot, run: _Run) -> None:
        run.stream_done.clear()
        threading.Thread(target=self._stream, args=(server, slot, run),
                         name=f"stream {server.id}", daemon=True).start()

    def _stream(self, server: Server, slot: _Slot, run: _Run) -> None:
        assert run.host is not None
        try:
            for event in run.host.attach(run.host_seq):
                kind = event.get("type")
                if kind == "line":
                    run.host_seq = int(event["seq"])
                    slot.console.write(str(event["text"]), LineSource(event["source"]),
                                       ts=float(event["ts"]), to_file=False)
                elif kind == "tracking":
                    run.set_game(int(event["pid"]))
                    log.info("%s: tracking real exe, pid %s", server.name, event["pid"])
                    self._save_state()
                elif kind == "exit":
                    run.host_exit = event
        except HostError as e:
            if not run.detached:
                log.info("%s: host stream ended: %s", server.name, e)
        finally:
            run.stream_done.set()

    def _start_watcher(self, server: Server, slot: _Slot, run: _Run) -> None:
        threading.Thread(target=self._watch, args=(server, slot, run),
                         name=f"watch {server.id}", daemon=True).start()

    def _watch(self, server: Server, slot: _Slot, run: _Run) -> None:
        failures = 0
        while not run.detached:
            if (slot.state is ServerState.STARTING and not run.stop_requested
                    and (run.ready.is_set()
                         or time.time() - run.started_at >= server.ready_after_s)):
                self._set_state(server.id, ServerState.RUNNING)
            if run.host is None:  # bare game: watch the process itself
                if run.game is None:
                    break
                try:
                    run.exit_code = run.game.wait(timeout=0.2)
                except psutil.TimeoutExpired:
                    continue
                except psutil.Error:
                    pass
                break
            if not run.stream_done.wait(0.2) or run.detached:
                continue
            outcome = run.host_exit or self._read_exit_file(server.id)
            if outcome is not None:
                run.exit_code = outcome.get("exit_code")
                run.exit_expected = bool(outcome.get("stop_requested"))
                break
            failures += 1
            if _alive(run.host_proc) and failures <= 5:
                time.sleep(0.2 * failures)  # a hiccup on the channel: re-attach
                self._start_stream(server, slot, run)
                continue
            if _alive(run.game):
                log.warning("%s: host is gone, game pid %s still running; console lost",
                            server.name, run.pid)
                run.host = None
                slot.console.system("Lost the server's host; the game is still running "
                                    "without a console. Restart it to get the console back")
                self._save_state()
                continue
            break
        if not run.detached:
            self._finish(server, slot, run)

    def _read_exit_file(self, server_id: int) -> dict | None:
        try:
            data = json.loads((self._settings.host_dir(server_id) / "exit.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _finish(self, server: Server, slot: _Slot, run: _Run) -> None:
        if run.tailer is not None:
            run.tailer.stop()
        if run.unsubscribe is not None:
            run.unsubscribe()
        expected = run.stop_requested or run.exit_expected
        code = run.exit_code
        if run.host is None or run.host_exit is None:
            # (With a host, it already wrote "Process exited" when it saw the exit.)
            slot.console.system(f"Process exited (code {code})" if code is not None
                                else "Process exited")
        (self._settings.host_dir(server.id) / "exit.json").unlink(missing_ok=True)
        log.log(logging.INFO if expected else logging.WARNING,
                "%s exited (code %s)%s", server.name, code, "" if expected else ": crash")
        with self._mu:
            slot.run = None
            slot.last_exit_code = code
        self.remote.forget(server.id)  # a kept RCON connection to that run is dead
        new = ServerState.STOPPED if expected else ServerState.CRASHED
        if expected and run.for_update:
            new = ServerState.UPDATING  # Stopping -> Updating: no "stopped" message
        self._set_state(server.id, new)
        run.exited.set()

    # -- stop -------------------------------------------------------------------

    def _stop_locked(self, server: Server, slot: _Slot) -> StopResult:
        run = slot.run
        if run is None:
            return StopResult(graceful=True, killed=False, ended_by="not running")
        run.stop_requested = True
        if run.host is not None:
            with contextlib.suppress(HostError):
                run.host.mark_stopping()
        self._set_state(server.id, ServerState.STOPPING)
        self._note(slot, run, "Stopping")
        for step in server.effective_stop_recipe():
            if run.exited.is_set():
                break
            if step.action is StopAction.WAIT_SAVE:
                self._wait_for_save(server, slot, run, step.wait_s)
                continue
            if not self._do_step(server, slot, run, step):
                continue
            if run.exited.wait(step.wait_s):
                if run.killed:  # sup.kill() ended it while we waited
                    return StopResult(graceful=False, killed=True, ended_by="kill")
                log.info("%s stopped after %s", server.name, step.action)
                return StopResult(graceful=True, killed=False, ended_by=step.action.value)
        if run.exited.is_set():
            return StopResult(graceful=not run.killed, killed=run.killed,
                              ended_by="kill" if run.killed else "exited")
        self._note(slot, run, "Still running after the stop recipe; killing")
        log.warning("%s didn't stop; killing", server.name)
        self._kill_run(server.id, run)
        return StopResult(graceful=False, killed=True, ended_by="kill")

    def _do_step(self, server: Server, slot: _Slot, run: _Run, step: StopStep) -> bool:
        """Carry out one stop step. False if it couldn't be done (so don't wait on it)."""
        if step.action not in _CONSOLE_EVENTS:
            return self._send(server, slot, run, step.action, step.command, "stop step")
        name, event = _CONSOLE_EVENTS[step.action]
        self._note(slot, run, f"Sending {name}")
        if run.host is not None:
            try:
                if step.action is StopAction.CTRL_C:
                    run.host.ctrl_c()
                else:
                    run.host.ctrl_break()
                return True
            except HostError as e:
                log.warning("%s: %s through the host failed: %s", server.name, name, e)
        return run.pid is not None and procs.send_ctrl_event(run.pid, event)

    def _wait_for_save(self, server: Server, slot: _Slot, run: _Run, wait_s: float) -> bool:
        """The wait_save step (D-121): wait up to ``wait_s`` for a file in the save folder to
        change (the game's own autosave), then for the folder to settle. For games that
        don't save on exit. True if a save was seen; either way the recipe goes on."""
        folder = save_folder(server)
        if folder is None or not folder.is_dir():
            self._note(slot, run,
                       f"Skipped waiting for a save: no save folder ({folder or 'not set'})")
            return False
        self._note(slot, run, f"Waiting up to {wait_s:g} s for the game to save")
        before = _folder_state(folder)
        deadline = time.monotonic() + wait_s
        while not run.exited.wait(_SAVE_POLL_S):
            now = _folder_state(folder)
            if now != before:
                settle_until = time.monotonic() + _SAVE_SETTLE_MAX_S
                while not run.exited.wait(_SAVE_SETTLE_S):  # let it finish writing
                    later = _folder_state(folder)
                    if later == now or time.monotonic() >= settle_until:
                        break
                    now = later
                changed = sorted(k for k in now if now[k] != before.get(k))
                self._note(slot, run, "Saved: " + (", ".join(changed) or "files removed"))
                return True
            if time.monotonic() >= deadline:
                self._note(slot, run, f"No save in {wait_s:g} s; going on")
                return False
        return False

    def _send(self, server: Server, slot: _Slot, run: _Run, action: StopAction, command: str,
              what: str) -> bool:
        """Send a stop step's or warning's command by stdin or RCON. False (with a note in
        the console) if it couldn't be sent."""
        if action is StopAction.STDIN:
            if run.host is not None:
                try:
                    run.host.send_line(command)  # the host echoes it into the console
                    return True
                except HostError as e:
                    log.warning("%s: stdin %s failed: %s", server.name, what, e)
            slot.console.system(f"Skipped {what} '{command}': no stdin",
                                to_file=run.host is None)
            return False
        name = action.value.upper()
        try:
            answer = self.remote.send(server, action, command)
        except Exception as e:  # never blocks a stop (D-96)
            log.warning("%s: %s %s failed: %s", server.name, name, what, e)
            self._note(slot, run, f"Skipped {what} '{command}': {name} failed: {e}")
            return False
        self._note(slot, run, f"({action.value}) {command}"
                              + (f": {_short(answer)}" if answer.strip() else ""))
        return True

    def _warn(self, server: Server, slot: _Slot, kind: str,
              abort: threading.Event | None = None) -> bool:
        """The stop warning (R-25, D-61), with the server's lock held: send the warning at
        each of its minutes before the stop, then wait for the stop time. Only for a
        Running server that has a warning. A warning that can't be sent ends the countdown
        and the stop goes ahead, as with a stop step that can't be done. False if the
        action should not go ahead: killed meanwhile, or ``abort`` set (update cancelled)."""
        warning = server.stop_warning
        run = slot.run
        if (warning is None or not warning.minutes or run is None
                or slot.state is not ServerState.RUNNING):
            return True
        countdown = _Countdown(kind, time.time() + warning.lead_s)
        with self._mu:
            slot.countdown = countdown
        self._changed(server.id)
        self._note(slot, run, f"{_WARN_VERB[kind]} in {warning.minutes[0]:g} min; "
                              "warning players")
        log.info("%s: %s in %g min, warning players", server.name, kind, warning.minutes[0])

        def wait_until(when: float) -> bool:  # True: the countdown ends now
            while time.time() < when:
                if (countdown.skip.wait(min(0.25, when - time.time())) or run.exited.is_set()
                        or (abort is not None and abort.is_set())):
                    return True
            return countdown.skip.is_set()

        try:
            for minutes in warning.minutes:
                if wait_until(countdown.stop_at - minutes * 60):
                    break
                if not self._send(server, slot, run, warning.action, warning.text(minutes),
                                  "warning"):
                    self._note(slot, run, f"Couldn't warn players; the {kind} goes ahead now")
                    break
            else:
                wait_until(countdown.stop_at)
        finally:
            with self._mu:
                slot.countdown = None
            self._changed(server.id)
        if countdown.killed:
            return False
        if abort is not None and abort.is_set():
            self._note(slot, run, f"{kind.capitalize()} cancelled; the server keeps running")
            return False
        if countdown.skip.is_set():
            self._note(slot, run, "Stop now: the rest of the warning is skipped")
        return True

    def _kill_run(self, server_id: int, run: _Run) -> None:
        run.killed = True
        if run.host is not None:
            try:
                run.host.kill()
            except HostError as e:
                log.warning("Server %s: kill through the host failed: %s", server_id, e)
        if not run.exited.wait(2):
            procs.kill_tree(run.game)  # host gone or stuck: kill the game ourselves
        if not run.exited.wait(self._settings.kill_wait_s):
            log.error("Server %s still not gone after kill", server_id)

    def _note(self, slot: _Slot, run: _Run | None, text: str) -> None:
        """A Basecamp line in the console. Goes through the host while there is one, so the
        host's console.log has it in order and a later Basecamp sees it in the replay."""
        if run is not None and run.host is not None:
            try:
                run.host.note(text)
                return
            except HostError:
                pass
        slot.console.system(text, to_file=run is None or run.host is None)

    # -- adoption and boot ------------------------------------------------------

    def adopt_orphans(self) -> list[int]:
        """Re-attach to servers from before a Basecamp restart (R-04, R-04a, D-25): to their
        hosts when they're up (console and stdin keep working), else to the bare game.
        Runs that ended while Basecamp was down are reported now: Crashed unless the exit
        was asked for or the PC restarted. Returns the ids of servers that are running."""
        attached = []
        for key, entry in self._load_state().items():
            try:
                server = self._store.get_server(int(key))
            except (KeyError, ValueError) as e:
                log.warning("Ignoring state.json entry %s: %s", key, e)
                continue
            try:
                if self._adopt_one(server, self._slot(server.id), entry):
                    attached.append(server.id)
            except Exception:
                log.exception("Adopting %s failed", server.name)
        self._save_state()  # drops entries for runs that are over
        return attached

    def _adopt_one(self, server: Server, slot: _Slot, entry: dict) -> bool:
        started_at = float(entry.get("started_at") or time.time())
        host_entry = entry.get("host") or {}
        if host_entry:
            host_proc = procs.match_process(host_entry.get("pid", 0), host_entry.get("exe", ""),
                                            host_entry.get("create_time", 0))
            if host_proc is not None:
                client = HostClient(int(host_entry["port"]), str(host_entry["token"]))
                try:
                    client.hello()
                    status = client.status()
                except (HostError, OSError) as e:
                    log.warning("%s: host pid %s doesn't answer (%s)", server.name,
                                host_proc.pid, e)
                    status = None
                if status is not None and status.get("running"):
                    run = _Run(client=client, host_proc=host_proc, started_at=started_at,
                               adopted=True)
                    run.set_game(status.get("game_pid"))
                    run.ready.set()
                    run.stop_requested = bool(status.get("stop_requested"))
                    with self._mu:
                        slot.run = run
                    self._note(slot, run, f"Basecamp reattached (host protocol "
                                          f"{client.protocol}, version {client.host_version})")
                    log.info("Reattached %s: host pid %s, game pid %s", server.name,
                             host_proc.pid, run.pid)
                    self._start_stream(server, slot, run)
                    self._set_state(server.id, ServerState.RUNNING)
                    self._start_watcher(server, slot, run)
                    return True

        outcome = self._read_exit_file(server.id) if host_entry else None
        if outcome is not None:
            self._report_exit_while_down(server, slot, outcome.get("exit_code"),
                                         bool(outcome.get("stop_requested")))
            return False

        game = procs.match_process(entry.get("pid", 0), entry.get("exe", ""),
                                   entry.get("create_time", 0))
        if game is not None:
            run = _Run(client=None, host_proc=None, started_at=started_at, adopted=True)
            run.game, run.game_info = game, procs.ProcInfo.of(game)
            run.ready.set()
            with self._mu:
                slot.run = run
            slot.console.system(f"Attached to running server (pid {game.pid}) without console;"
                                " restart it to get the console back")
            log.info("Adopted %s bare (pid %s)", server.name, game.pid)
            if server.log_source is LogSource.FILE:
                cwd = Path(server.working_dir or Path(server.exe_path).parent)
                run.tailer = FileTailer(cwd / server.log_file, slot.console.write).start()
            self._set_state(server.id, ServerState.RUNNING)
            self._start_watcher(server, slot, run)
            return True

        if psutil.boot_time() > started_at:
            log.info("%s was running before the PC restarted", server.name)
            slot.console.system("Was running before the PC restarted")
            return False
        self._report_exit_while_down(server, slot, None, False)
        return False

    def _report_exit_while_down(self, server: Server, slot: _Slot, code: int | None,
                                expected: bool) -> None:
        how = f"code {code}" if code is not None else "exit code unknown"
        slot.console.system(f"{'Stopped' if expected else 'Crashed'} while Basecamp was down "
                            f"({how})")
        log.log(logging.INFO if expected else logging.WARNING,
                "%s exited while Basecamp was down (%s)", server.name, how)
        (self._settings.host_dir(server.id) / "exit.json").unlink(missing_ok=True)
        slot.last_exit_code = code
        self._set_state(server.id, ServerState.STOPPED if expected else ServerState.CRASHED)

    def start_on_boot(self, gap_s: float | None = None,
                      cancel: threading.Event | None = None) -> dict[int, StartResult | str]:
        """Start every start-on-boot server, one after another (R-02). Already running
        (adopted) ones are left alone."""
        gap = self._settings.boot_gap_s if gap_s is None else gap_s
        cancel = cancel or threading.Event()
        servers = sorted((s for s in self._store.list_servers() if s.start_on_boot),
                         key=lambda s: (s.boot_order, s.id))
        results: dict[int, StartResult | str] = {}
        started_one = False
        for server in servers:
            if cancel.is_set():
                break
            if self.state(server.id) not in (ServerState.STOPPED, ServerState.CRASHED):
                results[server.id] = "already running"
                continue
            if started_one and cancel.wait(gap):
                break
            try:
                results[server.id] = self.start(server.id)
                started_one = True
            except (SupervisorError, OSError) as e:
                log.error("Start on boot failed for %s: %s", server.name, e)
                results[server.id] = f"failed: {e}"
        return results

    def detach(self) -> None:
        """Let go of all servers without stopping them (Basecamp shutting down, D-09).
        Hosts keep running and state.json is kept, so the next Basecamp reattaches."""
        with self._mu:
            runs = [slot.run for slot in self._slots.values() if slot.run is not None]
        for run in runs:
            run.detached = True
            if run.host is not None:
                run.host.close_stream()
            if run.tailer is not None:
                run.tailer.stop()
        with self._mu:
            for slot in self._slots.values():
                slot.console.close()

    # -- state and state.json -----------------------------------------------------

    def _slot(self, server_id: int) -> _Slot:
        with self._mu:
            slot = self._slots.get(server_id)
            if slot is None:
                s = self._settings
                console = Console(s.server_log_dir(server_id) / "console.log",
                                  s.console_lines, s.console_log_bytes, s.console_log_backups)
                slot = self._slots[server_id] = _Slot(console)
            return slot

    def _set_state(self, server_id: int, new: ServerState) -> None:
        slot = self._slot(server_id)
        with self._mu:
            old = slot.state
            slot.state = new
            self._save_state()
        if old is new:
            return
        log.debug("Server %s: %s -> %s", server_id, old, new)
        for fn in list(self._listeners):
            try:
                fn(server_id, old, new)
            except Exception:
                log.exception("State listener failed")

    def _changed(self, server_id: int) -> None:
        for fn in list(self._change_listeners):
            try:
                fn(server_id)
            except Exception:
                log.exception("Change listener failed")

    def _save_state(self) -> None:
        with self._mu:
            data = {}
            for sid, slot in self._slots.items():
                run = slot.run
                if run is None or run.exited.is_set():
                    continue
                entry: dict[str, object] = {"started_at": run.started_at,
                                            "state": slot.state.value}
                if run.game_info is not None:
                    entry.update(pid=run.game_info.pid, exe=run.game_info.exe,
                                 create_time=run.game_info.create_time)
                if run.host is not None and run.host_info is not None:
                    entry["host"] = {
                        "pid": run.host_info.pid, "exe": run.host_info.exe,
                        "create_time": run.host_info.create_time, "port": run.host.port,
                        "token": run.host.token, "protocol": run.host.protocol,
                    }
                data[str(sid)] = entry
            path = self._settings.state_path
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, path)

    def _load_state(self) -> dict[str, dict]:
        try:
            data = json.loads(self._settings.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.error("Can't read %s: %s", self._settings.state_path, e)
            return {}
        return data if isinstance(data, dict) else {}


class UpdateHold:
    """What ``Supervisor.updating`` yields: the server's lock is held throughout."""

    def __init__(self, sup: Supervisor, server: Server, slot: _Slot):
        self._sup = sup
        self._server = server
        self._slot = slot
        self.state_before = slot.state
        # Starting counts: it was meant to be up, so it comes back after the update.
        self.was_running = slot.state in (ServerState.STARTING, ServerState.RUNNING)

    def warn(self, abort: threading.Event | None = None) -> bool:
        """The stop warning before the update (R-25). False if the update should not go
        on: ``abort`` was set (cancelled; the server is left running)."""
        self._sup._warn(self._server, self._slot, "update", abort)
        return not (abort is not None and abort.is_set())

    def stop(self) -> StopResult:
        """Stop it with its stop recipe (kill at the end, as for any stop) and go to
        Updating. Raises SupervisorError if it is still running after that."""
        slot = self._slot
        run = slot.run
        result = StopResult(graceful=True, killed=False, ended_by="not running")
        if run is not None:
            run.for_update = True
            result = self._sup._stop_locked(self._server, slot)
            run.exited.wait(self._sup._settings.kill_wait_s)
            if slot.run is run:
                raise SupervisorError(f"{self._server.name} is still running after the stop")
        if slot.state is not ServerState.UPDATING:  # it wasn't running, or Crashed
            self._sup._set_state(self._server.id, ServerState.UPDATING)
        return result

    def start(self) -> StartResult:
        """Start it again after the update (Updating -> Stopped -> Starting)."""
        server = self._sup._store.get_server(self._server.id)  # the update may change it
        self._sup._set_state(server.id, ServerState.STOPPED)
        return self._sup._start_locked(server, self._slot)


def _folder_state(folder: Path) -> dict[str, tuple[float, int]]:
    """Each file under ``folder`` (relative name) with its modified time and size."""
    state: dict[str, tuple[float, int]] = {}
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = Path(root) / name
            with contextlib.suppress(OSError):
                st = path.stat()
                state[str(path.relative_to(folder))] = (st.st_mtime, st.st_size)
    return state


def _short(answer: str, limit: int = 200) -> str:
    text = " ".join(answer.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _alive(proc: psutil.Process | None) -> bool:
    if proc is None:
        return False
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False
