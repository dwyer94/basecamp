"""Per-server host process: owns one game server's process and console (R-04a, D-25).

Basecamp starts one host per running server, detached and outside its job. The host
starts the game with stdin/stdout pipes, writes its output to the rotated console log,
keeps the last lines for replay, and serves a control channel on 127.0.0.1 so any
Basecamp, including a newer one after a self-update, can attach, send commands, send
Ctrl+Break, Ctrl+C or kill. The host exits only after its game has exited, and leaves
``exit.json`` behind so a Basecamp that was down at the time can still see what happened.

Standard library only, and run as a script (``python -I host.py --spec spec.json``) from a
copy in the data folder, so it never depends on a Basecamp version folder that the
launcher may prune. basecamp/console.py imports its file helpers from here.

Control channel: TCP on 127.0.0.1 (ephemeral port), JSON lines. Every connection starts
with ``{"op": "hello", "token": ..., "protocol": N}``; the reply carries the protocol both
sides will use: ``min(N, PROTOCOL)``. Compatibility rule (docs/07-updates.md): a host
keeps speaking every older protocol version, and a client only uses ops that exist in the
negotiated version. Ops in protocol 1:

    hello                      -> {protocol, host_version, host_pid}
    status                     -> {game_pid, launcher_pid, running, exit_code, stop_requested,
                                   started_at, seq}
    attach  {after_seq}        -> {ok}, then a stream of events until the game exits:
                                  {"type": "line", seq, ts, source, text}
                                  {"type": "tracking", pid}       (launcher's real exe found)
                                  {"type": "exit", exit_code, stop_requested}
    send    {text}             write text + CRLF to the game's stdin
    note    {text}             add a Basecamp line ("[basecamp] ...") to the console
    stopping                   mark the coming exit as asked for (not a crash)
    ctrl_break                 Ctrl+Break to the game's console
    kill                       kill the game and its launcher, marks the exit as asked for

Protocol 2 adds:

    ctrl_c                     Ctrl+C to the game's console
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import ctypes
import hmac
import json
import logging
import os
import queue
import signal
import socketserver
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import IO, Any

PROTOCOL = 2

CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

log = logging.getLogger("basecamp.host")


# -- console log file and log-file tailing (shared with basecamp/console.py) ----------------

LINE_PREFIX = {"out": "", "in": "> ", "sys": "[basecamp] "}


def format_log_line(ts: float, source: str, text: str) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    return f"{stamp} {LINE_PREFIX.get(source, '')}{text}\n"


def decode_line(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


class RotatingFile:
    """Append-only text file rotated by size: console.log, console.log.1 .. .N."""

    def __init__(self, path: Path, max_bytes: int, backups: int):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._file: IO[str] | None = None
        self._rotate_failed = False

    def write(self, text: str) -> None:
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a", encoding="utf-8", newline="\n")
        self._file.write(text)
        self._file.flush()
        if self._file.tell() >= self.max_bytes:
            self._rotate()

    def _rotate(self) -> None:
        assert self._file is not None
        self._file.close()
        self._file = None
        try:
            for i in range(self.backups - 1, 0, -1):
                src = self.path.with_name(f"{self.path.name}.{i}")
                if src.exists():
                    os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
            if self.backups > 0:
                os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
            else:
                self.path.unlink()
        except OSError as e:  # e.g. someone has the file open in an editor
            if not self._rotate_failed:
                log.warning("Can't rotate %s: %s", self.path, e)
            self._rotate_failed = True

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


class FileTailer:
    """Follows a log file the game writes (log source = file), like ``tail -F``.

    Starts at the file's current end, so old lines aren't replayed. Copes with the file
    not existing yet, being truncated, or being replaced by a new file.
    """

    def __init__(self, path: Path, sink: Callable[[str], object], interval: float = 0.25):
        self.path = path
        self.sink = sink
        self.interval = interval
        self._stop = threading.Event()
        self._pos, self._ident = 0, None
        with contextlib.suppress(OSError):
            st = path.stat()
            self._pos, self._ident = st.st_size, (st.st_dev, st.st_ino)
        self._partial = b""
        self._thread = threading.Thread(target=self._run, name=f"tail {path.name}", daemon=True)

    def start(self) -> FileTailer:
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._poll()
            except OSError as e:
                log.debug("Tail %s: %s", self.path, e)
        with contextlib.suppress(OSError):
            self._poll()  # pick up the last lines written before the stop
        if self._partial:
            self.sink(decode_line(self._partial))
            self._partial = b""

    def _poll(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return
        ident = (st.st_dev, st.st_ino)
        if ident != self._ident or st.st_size < self._pos:
            self._ident, self._pos, self._partial = ident, 0, b""  # new or truncated file
        if st.st_size == self._pos:
            return
        with self.path.open("rb") as f:
            f.seek(self._pos)
            data = f.read()
        self._pos += len(data)
        *complete, self._partial = (self._partial + data).split(b"\n")
        for raw in complete:
            self.sink(decode_line(raw))


# -- Win32 bits (stdlib ctypes; no psutil here) ---------------------------------------------

SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0
STILL_ACTIVE = 259
TH32CS_SNAPPROCESS = 0x00000002
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_k32: Any = None
_HANDLER_TYPE: Any = None
_ctrl_handler: Any = None


def _kernel32() -> Any:
    global _k32, _HANDLER_TYPE
    if _k32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        _HANDLER_TYPE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        k.SetConsoleCtrlHandler.argtypes = [_HANDLER_TYPE, wintypes.BOOL]
        _k32 = k
    return _k32


class ProcHandle:
    """A process we can wait on and read the exit code of, even if it isn't our child."""

    def __init__(self, pid: int):
        k = _kernel32()
        self.pid = pid
        self.handle = k.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE, False, pid)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), f"can't open process {pid}")

    def wait(self, timeout_s: float) -> bool:
        return _kernel32().WaitForSingleObject(self.handle, int(timeout_s * 1000)) == WAIT_OBJECT_0

    def exit_code(self) -> int | None:
        code = wintypes.DWORD()
        if not _kernel32().GetExitCodeProcess(self.handle, ctypes.byref(code)):
            return None
        return None if code.value == STILL_ACTIVE else int(code.value)

    def alive(self) -> bool:
        return not self.wait(0)

    def terminate(self) -> None:
        _kernel32().TerminateProcess(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            _kernel32().CloseHandle(self.handle)
            self.handle = None


def process_create_time(pid: int) -> int | None:
    """Creation time as a FILETIME integer, or None if the process can't be opened."""
    k = _kernel32()
    handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        k.CloseHandle(handle)


def list_processes() -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) for every process."""
    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, wintypes.HANDLE(-1).value):
        return []
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return out


def descendants(root_pid: int, root_created: int | None) -> list[tuple[int, str]]:
    """(pid, name) of every process below root_pid, oldest generation first. Works after
    the root has exited; skips processes older than the root (a reused parent PID)."""
    by_parent: dict[int, list[tuple[int, str]]] = {}
    for pid, ppid, name in list_processes():
        by_parent.setdefault(ppid, []).append((pid, name))
    found, queue_, seen = [], [root_pid], {root_pid}
    while queue_:
        for pid, name in by_parent.get(queue_.pop(0), []):
            if pid in seen:
                continue
            seen.add(pid)
            created = process_create_time(pid)
            if root_created is not None and created is not None and created < root_created:
                continue
            found.append((pid, name))
            queue_.append(pid)
    return found


def install_ctrl_handler() -> None:
    """Swallow console control events, so a Ctrl+Break we send while attached to a game's
    console doesn't end the host itself. A Python-level SIGBREAK/SIGINT handler makes the C
    runtime's console handler report the event as handled."""
    global _ctrl_handler
    for name in ("SIGBREAK", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda _signum, _frame: None)
    _kernel32()
    _ctrl_handler = _HANDLER_TYPE(lambda _event: True)


_console_lock = threading.Lock()


def send_ctrl_event(pid: int, event: int = CTRL_BREAK_EVENT) -> bool:
    """Attach to the game's hidden console and raise Ctrl+Break (or Ctrl+C) on it. The host
    runs detached (no console of its own), so it can do this in-process."""
    k = _kernel32()
    with _console_lock:
        k.FreeConsole()
        if not k.AttachConsole(pid):
            return False
        k.SetConsoleCtrlHandler(_ctrl_handler, True)  # first in line while attached
        try:
            return bool(k.GenerateConsoleCtrlEvent(event, 0))
        finally:
            time.sleep(0.2)  # let our own handler see the event before we let go
            k.FreeConsole()
            k.SetConsoleCtrlHandler(_ctrl_handler, False)


# -- the host ---------------------------------------------------------------------------------


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Host:
    def __init__(self, spec: dict):
        self.spec = spec
        self.dir = Path(spec["host_dir"])
        self.token = spec["token"]
        self.lock = threading.Lock()
        self.lines: collections.deque[dict] = collections.deque(maxlen=int(spec["ring_lines"]))
        self.seq = 0
        self.streams: list[queue.Queue] = []
        self.file = RotatingFile(Path(spec["console_log"]), int(spec["console_log_bytes"]),
                                 int(spec["console_log_backups"]))
        self.popen: subprocess.Popen[bytes] | None = None
        self.stdin_lock = threading.Lock()
        self.launcher: ProcHandle | None = None
        self.launcher_created: int | None = None
        self.game: ProcHandle | None = None
        self.real_exe_name = spec.get("real_exe_name", "")
        self.stop_requested = False
        self.exit_code: int | None = None
        self.exited = threading.Event()
        self.started_at = time.time()
        self.server: socketserver.ThreadingTCPServer | None = None
        self.tailer: FileTailer | None = None
        self.pump_thread: threading.Thread | None = None

    # output ------------------------------------------------------------------

    def emit(self, text: str, source: str = "out") -> None:
        with self.lock:
            self.seq += 1
            line = {"type": "line", "seq": self.seq, "ts": time.time(), "source": source,
                    "text": text}
            self.lines.append(line)
            try:
                self.file.write(format_log_line(line["ts"], source, text))
            except OSError as e:
                log.warning("Can't write console log: %s", e)
            self._broadcast(line)

    def _broadcast(self, event: dict) -> None:
        for q in list(self.streams):
            try:
                q.put_nowait(event)
            except queue.Full:  # a stuck client: end its stream; it can re-attach with after_seq
                self.streams.remove(q)
                with q.mutex:
                    q.queue.clear()
                q.put_nowait(None)

    def pump(self) -> None:
        assert self.popen is not None and self.popen.stdout is not None
        try:
            for raw in iter(self.popen.stdout.readline, b""):
                self.emit(decode_line(raw))
        except (OSError, ValueError):
            pass

    # game ----------------------------------------------------------------------

    def start_game(self) -> None:
        spec = self.spec
        kwargs: dict = dict(cwd=spec["cwd"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
        # No new process group, and our own "ignore Ctrl+C" (from being started in one)
        # cleared first: the game inherits that flag, and with it set a ctrl_c stop step
        # never reaches the game (D-120). Ctrl+Break gets through either way.
        _kernel32().SetConsoleCtrlHandler(_HANDLER_TYPE(), False)  # NULL handler
        flags = CREATE_NO_WINDOW
        try:
            self.popen = subprocess.Popen(spec["cmdline"],
                                          creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except PermissionError:
            self.popen = subprocess.Popen(spec["cmdline"], creationflags=flags, **kwargs)
        self.launcher = ProcHandle(self.popen.pid)
        self.launcher_created = process_create_time(self.popen.pid)
        self.game = self.launcher
        self.emit(f"Started (pid {self.popen.pid})", "sys")
        self.pump_thread = threading.Thread(target=self.pump, name="pump", daemon=True)
        self.pump_thread.start()
        if spec.get("log_file"):
            self.tailer = FileTailer(Path(spec["log_file"]), self.emit).start()

    def look_for_real_exe(self) -> bool:
        assert self.launcher is not None
        wanted = self.real_exe_name.casefold()
        for pid, name in descendants(self.launcher.pid, self.launcher_created):
            if name.casefold() == wanted:
                try:
                    handle = ProcHandle(pid)
                except OSError:
                    continue
                self.game = handle
                self.real_exe_name = ""
                log.info("Tracking real exe %s (pid %s)", name, pid)
                self.emit(f"Tracking {name} (pid {pid})", "sys")
                with self.lock:
                    self._broadcast({"type": "tracking", "pid": pid})
                return True
        return False

    def wait_for_game(self) -> None:
        give_up = time.monotonic() + float(self.spec.get("real_exe_timeout_s", 60))
        next_look = 0.0
        while True:
            assert self.game is not None
            now = time.monotonic()
            if self.real_exe_name and now >= next_look:
                next_look = now + 0.5
                if not self.look_for_real_exe() and now > give_up:
                    log.warning("%s not found; tracking pid %s", self.real_exe_name,
                                self.game.pid)
                    self.real_exe_name = ""
            if not self.game.wait(0.2):
                continue
            if self.real_exe_name and self.game is self.launcher and self.look_for_real_exe():
                continue  # the launcher handed over to the real exe
            break

    def kill_all(self) -> None:
        """Kill the game, the launcher and anything they started."""
        victims: list[int] = []
        for proc in (self.launcher, self.game):
            if proc is not None:
                victims.append(proc.pid)
                victims.extend(pid for pid, _ in descendants(proc.pid, None))
        for pid in dict.fromkeys(victims):
            with contextlib.suppress(OSError):
                handle = ProcHandle(pid)
                handle.terminate()
                handle.close()

    def finish(self) -> None:
        assert self.game is not None
        self.exit_code = self.game.exit_code()
        if self.launcher is not None and self.launcher is not self.game and self.launcher.alive():
            self.kill_all()
        if self.tailer is not None:
            self.tailer.stop()
        if self.popen is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.popen.wait(timeout=2)
            if self.game is self.launcher and self.exit_code is None:
                self.exit_code = self.popen.returncode
        if self.pump_thread is not None:
            self.pump_thread.join(timeout=2)  # last lines; EOF once every pipe holder is gone
        code = self.exit_code
        self.emit(f"Process exited (code {code})" if code is not None else "Process exited",
                  "sys")
        result = {"exit_code": code, "stop_requested": self.stop_requested,
                  "game_pid": self.game.pid, "started_at": self.started_at,
                  "ended_at": time.time()}
        write_json(self.dir / "exit.json", result)
        log.info("Game exited: %s", result)
        with self.lock:
            self._broadcast({"type": "exit", "exit_code": code,
                             "stop_requested": self.stop_requested})
            self.exited.set()
            for q in self.streams:
                q.put(None)  # end of stream
        # Give attached clients a moment to read the exit event.
        deadline = time.monotonic() + 2
        while self.streams and time.monotonic() < deadline:
            time.sleep(0.05)
        self.file.close()

    # control channel ---------------------------------------------------------------

    def status(self) -> dict:
        return {
            "game_pid": self.game.pid if self.game else None,
            "launcher_pid": self.launcher.pid if self.launcher else None,
            "running": not self.exited.is_set(),
            "exit_code": self.exit_code,
            "stop_requested": self.stop_requested,
            "started_at": self.started_at,
            "seq": self.seq,
        }

    def handle_op(self, op: str, msg: dict, wfile: IO[bytes]) -> dict | None:
        if op == "status":
            return {"ok": True, **self.status()}
        if op == "send":
            if self.popen is None or self.popen.stdin is None:
                return {"ok": False, "error": "no stdin"}
            with self.stdin_lock:
                try:
                    self.popen.stdin.write((str(msg["text"]) + "\r\n").encode("utf-8"))
                    self.popen.stdin.flush()
                except (OSError, ValueError) as e:
                    return {"ok": False, "error": f"stdin closed: {e}"}
            if msg.get("echo", True):
                self.emit(str(msg["text"]), "in")
            return {"ok": True}
        if op == "note":
            self.emit(str(msg["text"]), "sys")
            return {"ok": True}
        if op == "stopping":
            self.stop_requested = True
            return {"ok": True}
        if op in ("ctrl_break", "ctrl_c"):
            if self.game is None or self.exited.is_set():
                return {"ok": False, "error": "not running"}
            event = CTRL_C_EVENT if op == "ctrl_c" else CTRL_BREAK_EVENT
            return {"ok": send_ctrl_event(self.game.pid, event)}
        if op == "kill":
            self.stop_requested = True
            self.kill_all()
            return {"ok": True}
        if op == "attach":
            self.stream(int(msg.get("after_seq", 0)), wfile)
            return None
        return {"ok": False, "error": f"unknown op {op!r}"}

    def stream(self, after_seq: int, wfile: IO[bytes]) -> None:
        q: queue.Queue = queue.Queue(maxsize=10000)
        with self.lock:
            backlog = [ln for ln in self.lines if ln["seq"] > after_seq]
            if self.game is not None and self.game is not self.launcher:
                backlog.insert(0, {"type": "tracking", "pid": self.game.pid})
            if self.exited.is_set():
                backlog.append({"type": "exit", "exit_code": self.exit_code,
                                "stop_requested": self.stop_requested})
                backlog.append(None)
            else:
                self.streams.append(q)
        try:
            send(wfile, {"ok": True})
            for event in backlog:
                if event is None:
                    return
                send(wfile, event)
            while True:
                event = q.get()
                if event is None:
                    return
                send(wfile, event)
        except OSError:
            pass  # client went away
        finally:
            with self.lock:
                if q in self.streams:
                    self.streams.remove(q)

    def serve(self) -> int:
        host = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                try:
                    hello = json.loads(self.rfile.readline() or b"{}")
                    if (hello.get("op") != "hello"
                            or not hmac.compare_digest(str(hello.get("token", "")), host.token)):
                        send(self.wfile, {"ok": False, "error": "bad hello"})
                        return
                    protocol = min(int(hello.get("protocol", 1)), PROTOCOL)
                    send(self.wfile, {"ok": True, "protocol": protocol,
                                      "host_version": host.spec.get("basecamp_version", ""),
                                      "host_pid": os.getpid()})
                    for raw in self.rfile:
                        msg = json.loads(raw)
                        reply = host.handle_op(str(msg.get("op")), msg, self.wfile)
                        if reply is None:
                            return
                        send(self.wfile, reply)
                except (OSError, ValueError) as e:
                    log.debug("Control connection: %s", e)
                except Exception:
                    log.exception("Control connection failed")

        socketserver.ThreadingTCPServer.daemon_threads = True
        socketserver.ThreadingTCPServer.allow_reuse_address = False
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, name="control", daemon=True).start()
        return self.server.server_address[1]


def send(wfile: IO[bytes], data: dict) -> None:
    wfile.write((json.dumps(data) + "\n").encode("utf-8"))
    wfile.flush()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Basecamp game server host")
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    spec_path = Path(args.spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    host_dir = Path(spec["host_dir"])
    logging.basicConfig(
        filename=host_dir / "host.log", level=logging.INFO, encoding="utf-8",
        format="%(asctime)s %(levelname)-7s host %(process)d: %(message)s")
    install_ctrl_handler()
    host = Host(spec)
    port = host.serve()
    try:
        host.start_game()
    except OSError as e:
        log.error("Can't start the game: %s", e)
        write_json(host_dir / "host.json", {"protocol": PROTOCOL, "host_pid": os.getpid(),
                                            "port": port, "error": f"can't start: {e}"})
        return 1
    assert host.popen is not None
    write_json(host_dir / "host.json", {"protocol": PROTOCOL, "host_pid": os.getpid(),
                                        "port": port, "game_pid": host.popen.pid})
    log.info("Hosting %s: game pid %s, control port %s", spec.get("name"), host.popen.pid, port)
    host.wait_for_game()
    host.finish()
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except Exception:
        log.exception("Host failed")
        code = 1
    os._exit(code)
