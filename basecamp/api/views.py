"""What each kind of visitor sees of a server (R-70, R-71).

The friend view is built field by field from an allow-list, never by trimming the admin
view, so a field added later can't leak to friends by accident.
"""

from __future__ import annotations

import threading

import psutil

from basecamp.models import Server, ServerState
from basecamp.settings import Settings
from basecamp.supervisor import ServerStatus

# Everything a friend can get for a server, here and on the status WebSocket (tested).
FRIEND_FIELDS = ("id", "name", "state", "uptime_s", "join_address")


def join_address(settings: Settings, server: Server) -> str | None:
    """R-24: ``<join host>:<join port>``, or None until both are set."""
    if not settings.join_host or server.join_port is None:
        return None
    return f"{settings.join_host}:{server.join_port}"


def discord_line(settings: Settings, server: Server) -> str | None:
    address = join_address(settings, server)
    if address is None:
        return None
    return f"{server.name} is up \u2192 {address}"


def _uptime(status: ServerStatus) -> float | None:
    if status.state is not ServerState.RUNNING or status.uptime_s is None:
        return None
    return round(status.uptime_s)


def friend_view(settings: Settings, server: Server, status: ServerStatus) -> dict:
    view = {
        "id": server.id,
        "name": server.name,
        "state": status.state.value,
        "uptime_s": _uptime(status),
        "join_address": join_address(settings, server),
    }
    assert tuple(view) == FRIEND_FIELDS
    return view


class CpuSampler:
    """CPU percent per process needs two samples; keep one psutil.Process per PID."""

    def __init__(self) -> None:
        self._procs: dict[int, psutil.Process] = {}
        self._lock = threading.Lock()

    def percent(self, pid: int | None) -> float | None:
        if pid is None:
            return None
        with self._lock:
            proc = self._procs.get(pid)
            try:
                if proc is None:
                    proc = self._procs[pid] = psutil.Process(pid)
                    proc.cpu_percent(None)  # first call is always 0.0
                    return None
                return round(proc.cpu_percent(None) / (psutil.cpu_count() or 1), 1)
            except psutil.Error:
                self._procs.pop(pid, None)
                return None

    def forget_others(self, keep: set[int]) -> None:
        with self._lock:
            for pid in set(self._procs) - keep:
                del self._procs[pid]


def admin_view(settings: Settings, server: Server, status: ServerStatus,
               cpu: CpuSampler | None = None) -> dict:
    view = friend_view(settings, server, status)
    view.update(
        config=server.model_dump(mode="json", exclude={"id"}),
        discord_line=discord_line(settings, server),
        pid=status.pid,
        adopted=status.adopted,
        has_console=status.has_console,
        rss_mb=round(status.rss_bytes / 2**20) if status.rss_bytes else None,
        cpu_percent=cpu.percent(status.pid) if cpu else None,
        last_exit_code=status.last_exit_code,
    )
    return view

