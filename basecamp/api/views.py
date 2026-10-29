"""What each kind of visitor sees of a server (R-70, R-71; batch 3, D-135).

Each friend view is built field by field from its own allow-list, never by trimming the
admin view, so a field added later can't leak to friends by accident (D-30). Friends never
get config contents, install or exe paths, launch arguments, PIDs, exit codes or secrets.
"""

from __future__ import annotations

import threading

import psutil

from basecamp.models import Server, ServerState
from basecamp.notify import join_address
from basecamp.settings import Settings
from basecamp.supervisor import ServerStatus

# The dashboard card, here and on the status WebSocket (tested key by key).
# players: {"count", "max"} (count None = unknown), or None when the game doesn't report it
# or isn't running. The count only, never names (D-132).
# last_updated: {"at", "source"} when the game was last updated: "steam" (the app
# manifest's LastUpdated) or "basecamp" (its own last good update), else None.
# job: a queued or running install or update, {"kind", "phase", "progress", "position"}.
FRIEND_FIELDS = ("id", "name", "state", "uptime_s", "join_address", "players",
                 "cpu_percent", "rss_mb", "last_updated", "update_available", "job",
                 "stop_warning", "restart_at")
# The server page's details panel and Schedule tab, on top of the card.
FRIEND_DETAIL_FIELDS = (*FRIEND_FIELDS, "build_id", "latest_build", "schedule",
                        "last_backup", "quick_commands")
FRIEND_SCHEDULE_FIELDS = ("restart_at", "update_at", "players", "waiting")
FRIEND_JOB_FIELDS = ("kind", "phase", "progress", "position")
# The Firewall tab: each port's status, never the rule names, paths or commands.
FRIEND_FIREWALL_FIELDS = ("status", "ports")
FRIEND_FIREWALL_PORT_FIELDS = ("port", "protocol", "label", "status")


def discord_line(settings: Settings, server: Server) -> str | None:
    address = join_address(settings, server)
    if address is None:
        return None
    return f"{server.name} is up → {address}"


def _uptime(status: ServerStatus) -> float | None:
    if status.state is not ServerState.RUNNING or status.uptime_s is None:
        return None
    return round(status.uptime_s)


def _rss_mb(status: ServerStatus) -> int | None:
    return round(status.rss_bytes / 2**20) if status.rss_bytes else None


def _job(steam: dict | None) -> dict | None:
    job = steam.get("job") if steam else None
    if not job:
        return None
    view = {"kind": job.get("kind"), "phase": job.get("phase"),
            "progress": job.get("progress"), "position": job.get("position")}
    assert tuple(view) == FRIEND_JOB_FIELDS
    return view


def friend_view(settings: Settings, server: Server, status: ServerStatus,
                players: dict | None = None, *, cpu_percent: float | None = None,
                steam: dict | None = None, last_updated: dict | None = None,
                restart_at: float | None = None) -> dict:
    warning = status.warning
    view = {
        "id": server.id,
        "name": server.name,
        "state": status.state.value,
        "uptime_s": _uptime(status),
        "join_address": join_address(settings, server),
        "players": {"count": players.get("count"), "max": players.get("max")} if players
        else None,
        "cpu_percent": cpu_percent,
        "rss_mb": _rss_mb(status),
        "last_updated": {"at": last_updated["at"], "source": last_updated["source"]}
        if last_updated else None,
        "update_available": steam.get("update_available") if steam else None,
        "job": _job(steam),
        "stop_warning": {"kind": warning["kind"], "stop_at": warning["stop_at"]}
        if warning else None,
        "restart_at": restart_at,  # a crash restart is pending (R-05)
    }
    assert tuple(view) == FRIEND_FIELDS
    return view


def friend_schedule(schedule: dict | None) -> dict | None:
    if not schedule:
        return None
    view = {"restart_at": schedule["restart_at"], "update_at": schedule["update_at"],
            "players": schedule["players"], "waiting": dict(schedule["waiting"])}
    assert tuple(view) == FRIEND_SCHEDULE_FIELDS
    return view


def friend_detail(card: dict, steam: dict | None, schedule: dict | None,
                  last_backup: float | None, quick: list[dict]) -> dict:
    """The server page for a friend: the card (itself a friend view), plus the build,
    the schedule, when the last backup was made and the quick commands friends may tap."""
    latest = steam.get("latest") if steam else None
    view = {key: card[key] for key in FRIEND_FIELDS}
    view.update(
        build_id=steam.get("build_id") if steam else None,
        latest_build=latest.get("build_id") if latest else None,
        schedule=friend_schedule(schedule),
        last_backup=last_backup,
        quick_commands=[{"n": q["n"], "label": q["label"]} for q in quick if q["friend"]],
    )
    assert tuple(view) == FRIEND_DETAIL_FIELDS
    return view


def friend_firewall(status: dict) -> dict:
    view = {"status": status.get("status"), "ports": [
        {"port": p.get("port"), "protocol": p.get("protocol"), "label": p.get("label"),
         "status": p.get("status")} for p in status.get("ports", [])]}
    assert tuple(view) == FRIEND_FIREWALL_FIELDS
    assert all(tuple(p) == FRIEND_FIREWALL_PORT_FIELDS for p in view["ports"])
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
               cpu: CpuSampler | None = None, restart_at: float | None = None,
               steam: dict | None = None, remote: dict | None = None,
               players: dict | None = None, schedule: dict | None = None,
               last_updated: dict | None = None, last_line: str | None = None) -> dict:
    cpu_percent = cpu.percent(status.pid) if cpu else None
    view = friend_view(settings, server, status, players, cpu_percent=cpu_percent,
                       steam=steam, last_updated=last_updated, restart_at=restart_at)
    view.update(
        config=server.model_dump(mode="json", exclude={"id"}),
        discord_line=discord_line(settings, server),
        pid=status.pid,
        adopted=status.adopted,
        has_console=status.has_console,
        last_exit_code=status.last_exit_code,
        stop_warning=status.warning,  # a stop countdown: {"kind", "stop_at"} (R-25)
        steam=steam,  # build ID, a queued or running install/update, the last run (R-30)
        remote=remote,  # console channels and whether the RCON/REST passwords are set
        players_detail=players,  # with when it was asked, how, and why it's unknown
        schedule=schedule,  # daily restart and update times, and one waiting for players
        last_line=last_line,  # the console's last line, for a crashed server's card
    )
    return view


def host_view(is_admin: bool, boot_time: float | None = None) -> dict:
    """The dashboard summary's RAM free, for everyone; the PC's boot time for the admin."""
    mem = psutil.virtual_memory()
    view: dict = {"ram_free_mb": round(mem.available / 2**20),
                  "ram_total_mb": round(mem.total / 2**20)}
    if is_admin:
        view["boot_time"] = boot_time if boot_time is not None else psutil.boot_time()
    return view
