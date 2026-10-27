"""Windows process helpers: spawn, find, match, Ctrl+Break, kill, and pre-start checks."""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

from basecamp.models import Port, Protocol

log = logging.getLogger(__name__)

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACHED_PROCESS = 0x00000008

_WINCTRL = Path(__file__).with_name("winctrl.py")


@dataclass(frozen=True)
class ProcInfo:
    """What identifies a process across Basecamp restarts (R-04): a PID alone can be reused."""

    pid: int
    exe: str
    create_time: float

    @classmethod
    def of(cls, proc: psutil.Process) -> ProcInfo:
        return cls(proc.pid, proc.exe(), proc.create_time())


def host_python() -> Path:
    """The interpreter for host processes: the base one, not a venv's python.exe (which is
    itself a launcher process). Hosts are stdlib only, so they don't need site-packages."""
    return Path(getattr(sys, "_base_executable", None) or sys.executable)


def spawn_host(python: Path, script: Path, spec: Path) -> subprocess.Popen[bytes]:
    """Start a server's host process, detached from Basecamp (no console, own process
    group) and outside Basecamp's job if the job allows it, so ending Basecamp or its
    scheduled task can't take the host or its game down (R-04a, D-21)."""
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, cwd=str(spec.parent))
    args = [str(python), "-I", str(script), "--spec", str(spec)]
    try:
        return subprocess.Popen(args, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except PermissionError:
        log.info("Job doesn't allow breakaway; starting the host inside it")
        return subprocess.Popen(args, creationflags=flags, **kwargs)


def same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def match_process(pid: int, exe: str, create_time: float) -> psutil.Process | None:
    """The process with this PID, if it is still the same one (same exe and create time)."""
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return None
        if abs(proc.create_time() - create_time) > 0.01:
            return None
        if not same_path(proc.exe(), exe):
            return None
        return proc
    except (psutil.Error, OSError):
        return None


def send_ctrl_break(pid: int, timeout: float = 10.0) -> bool:
    """Ctrl+Break to the hidden console of `pid` via the winctrl helper. The fallback for
    when the server's host can't do it (host gone, bare adopted game)."""
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(_WINCTRL), str(pid)],
            creationflags=DETACHED_PROCESS, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("Ctrl+Break helper failed for pid %s: %s", pid, e)
        return False
    if result.returncode != 0:
        log.warning("Ctrl+Break to pid %s failed (helper exit %s)", pid, result.returncode)
    return result.returncode == 0


def kill_tree(*procs: psutil.Process | None, timeout: float = 5.0) -> None:
    """Kill these processes and everything below them."""
    victims: dict[int, psutil.Process] = {}
    for proc in procs:
        if proc is None:
            continue
        try:
            for child in proc.children(recursive=True):
                victims[child.pid] = child
        except psutil.Error:
            pass
        victims[proc.pid] = proc
    for proc in victims.values():
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(list(victims.values()), timeout=timeout)


def port_in_use(port: int, protocol: Protocol) -> bool:
    """True if something on this PC already holds the port (R-03)."""
    kind = socket.SOCK_DGRAM if protocol is Protocol.UDP else socket.SOCK_STREAM
    sock = socket.socket(socket.AF_INET, kind)
    try:
        # Exclusive bind fails if any socket holds the port, wildcard or specific address.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("0.0.0.0", port))
    except OSError:
        return True
    finally:
        sock.close()
    return _listed_in_use(port, protocol)


def _listed_in_use(port: int, protocol: Protocol) -> bool:
    # Second opinion from the OS tables, for sockets bound to one specific address.
    kind = "udp4" if protocol is Protocol.UDP else "tcp4"
    try:
        conns = psutil.net_connections(kind=kind)
    except (psutil.Error, OSError):
        return False
    for conn in conns:
        if not conn.laddr or conn.laddr.port != port:
            continue
        if protocol is Protocol.UDP or conn.status == psutil.CONN_LISTEN:
            return True
    return False


def busy_ports(ports: list[Port]) -> list[Port]:
    return [p for p in ports if port_in_use(p.port, p.protocol)]


def free_ram_mb() -> int:
    return int(psutil.virtual_memory().available // (1024 * 1024))
