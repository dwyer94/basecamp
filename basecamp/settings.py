"""Paths, port and tunables.

The data folder comes from ``BASECAMP_DATA_DIR`` (prod: ``C:\\BasecampData``), else ``.\\data``.
Optional overrides live in ``<data>\\settings.toml`` under ``[basecamp]``. Secrets never go here;
they belong in ``<data>\\secrets.toml`` (read by the modules that need them, from Phase 2 on).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# migrations/ ships next to the basecamp/ package, in the repo and in release zips (docs/07).
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# Built-in game templates (R-13), shipped the same way; <data>/templates/ can add or override.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
DEFAULT_STEAMCMD = r"%USERPROFILE%\Desktop\SteamCmd\steamcmd.exe"
# What the Settings page may change (D-52). The rest is a settings.toml edit, picked up
# without a restart except for RESTART_SETTINGS (D-127).
PAGE_SETTINGS = ("join_host", "steamcmd_path", "default_backup_every_h", "default_backup_keep",
                 "update_beta", "update_auto_hour", "discord_live_status", "friend_control",
                 "friend_control_logins")
# Who besides the admins may start, stop and restart servers from the friend view (D-131).
FRIEND_CONTROL = ("off", "logins", "anyone")
# Read once at start (the web server's address, the DB's migrations, the console buffers
# and log rotation already set up): a change waits for Restart Basecamp. Every other
# setting is read each time it's used, so a reload applies it at once.
RESTART_SETTINGS = ("host", "port", "migrations_dir", "console_lines", "console_log_bytes",
                    "console_log_backups")
_save_lock = threading.Lock()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8150
    # R-02: gap between start-on-boot servers so they don't all hit the disk at once.
    boot_gap_s: float = 10.0
    # R-03: warn (never block) when free RAM is below this before a start.
    low_ram_mb: int = 2048
    # How long to look for a launcher game's real exe among its children.
    real_exe_timeout_s: float = 60.0
    # How long a new host gets to start its game and report its control port.
    host_start_timeout_s: float = 15.0
    # After the last stop step, how long to wait for a killed process to go.
    kill_wait_s: float = 10.0
    # R-23: in-memory lines per server, and the on-disk console log rotation.
    console_lines: int = 2000
    console_log_bytes: int = 10 * 1024 * 1024
    console_log_backups: int = 5
    migrations_dir: Path = MIGRATIONS_DIR
    templates_dir: Path = TEMPLATES_DIR
    # R-24: the host friends type in, e.g. the host PC's Tailscale IP. Set it in the data
    # folder's settings.toml; there is deliberately no default (the repo is public).
    join_host: str = ""
    # D-27: Tailscale logins (Tailscale-User-Login) that get the admin view. A request from
    # the PC itself (loopback, no Tailscale headers) is admin too; everyone else is a friend.
    admin_logins: tuple[str, ...] = ()
    # D-131: friends may start, stop and restart servers: "off", "logins" (the Tailscale
    # logins in friend_control_logins; works only if a friend's request carries their login,
    # Q-02) or "anyone" who reaches the page. One friend click per server per
    # friend_control_gap_s, so nobody can spam restarts.
    friend_control: str = "off"
    friend_control_logins: tuple[str, ...] = ()
    friend_control_gap_s: float = 60.0
    # R-05: an exit nobody asked for restarts after crash_restart_delay_s, unless it is the
    # crash_limit-th crash within crash_window_s: then the server stays Crashed.
    crash_restart_delay_s: float = 10.0
    crash_limit: int = 3
    crash_window_s: float = 600.0
    # R-53: where save backups go ("" = <data>\backups). Q-08: another drive is fine here.
    backups_root: str = ""
    # How often the backup scheduler looks for servers that are due (R-50).
    backup_check_s: float = 60.0
    # R-62: waits between tries of a Discord message that failed (one try per entry, + 1).
    notify_retry_s: tuple[float, ...] = (5.0, 30.0, 120.0)
    # R-63: keep one message on the main webhook showing every server (Settings page), edited
    # at most once per live_status_every_s.
    discord_live_status: bool = False
    live_status_every_s: float = 10.0
    # R-31: steamcmd.exe ("" = %USERPROFILE%\Desktop\SteamCmd\steamcmd.exe), and how
    # long one SteamCMD run may take before it is killed.
    steamcmd_path: str = ""
    steamcmd_timeout_s: float = 3600.0
    # R-34: how often each installed Steam server's latest build is checked (0 = only on
    # "Check for update"), how often the scheduler looks, and how long one check may take.
    steam_check_h: float = 6.0
    steam_check_poll_s: float = 300.0
    steam_check_timeout_s: float = 120.0
    # Backup schedule and retention a new server starts with (R-50); each server can change
    # its own. Set on the Settings page.
    default_backup_every_h: float = 0.0
    default_backup_keep: int = 10
    # R-41: old copies kept per config file saved from the page.
    config_copies_keep: int = 20
    # New game from a template (D-130): how long the first start may take to reach Running,
    # and then how long to wait for the config files it should write.
    setup_start_timeout_s: float = 600.0
    setup_config_wait_s: float = 60.0
    # RCON and REST (D-92..D-97): how long one command may take, and whether a server's
    # rcon_host may be another PC (the password crosses the network unencrypted).
    remote_timeout_s: float = 5.0
    remote_console_allow_remote: bool = False
    # R-90..R-94: Basecamp's own updates. The repo is public, so no token. update_api is
    # only changed to point the dev PC at a local fake release server (tools/fake_releases.py).
    update_repo: str = "dwyer94/basecamp"
    update_api: str = "https://api.github.com"
    update_check_h: float = 6.0  # 0 = only "Check now"
    update_beta: bool = False  # pre-releases count too
    update_auto_hour: int = -1  # 0-23: install a new release at that hour; -1 = off

    @property
    def db_path(self) -> Path:
        return self.data_dir / "basecamp.db"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def server_log_dir(self, server_id: int) -> Path:
        return self.logs_dir / "servers" / str(server_id)

    @property
    def secrets_path(self) -> Path:
        """Webhook URLs and RCON/REST passwords. Never in the repo or settings.toml."""
        return self.data_dir / "secrets.toml"

    @property
    def backups_dir(self) -> Path:
        return Path(self.backups_root) if self.backups_root else self.data_dir / "backups"

    @property
    def steamcmd_exe(self) -> Path:
        raw = self.steamcmd_path.strip().strip('"') or DEFAULT_STEAMCMD
        return Path(os.path.expandvars(raw))

    def steam_log_dir(self, server_id: int) -> Path:
        """One log file per SteamCMD run (``<run id>.log``)."""
        return self.logs_dir / "steamcmd" / str(server_id)

    def server_backup_dir(self, server_id: int) -> Path:
        return self.backups_dir / str(server_id)

    @property
    def data_templates_dir(self) -> Path:
        return self.data_dir / "templates"

    @property
    def hosts_dir(self) -> Path:
        return self.data_dir / "hosts"

    def host_dir(self, server_id: int) -> Path:
        """spec.json (with the control token), host.json, exit.json, host.log."""
        return self.hosts_dir / str(server_id)

    def ensure_dirs(self) -> None:
        (self.logs_dir / "servers").mkdir(parents=True, exist_ok=True)

    def save_page_settings(self, values: dict[str, object]) -> None:
        """Write page-editable settings to settings.toml and apply them at once (D-52).

        Everything in Basecamp holds this one Settings object, so it is changed in place;
        readers take the value each time they need it. settings.toml is edited line by line
        so its comments and other keys stay as they are.
        """
        unknown = set(values) - set(PAGE_SETTINGS)
        if unknown:
            raise ValueError(f"Not a page setting: {', '.join(sorted(unknown))}")
        with _save_lock:
            path = self.data_dir / "settings.toml"
            old = path.read_text(encoding="utf-8") if path.is_file() else ""
            new = _set_toml_keys(old, "basecamp", values)
            parsed = tomllib.loads(new).get("basecamp", {})
            if any(parsed.get(k) != (list(v) if isinstance(v, tuple) else v)
                   for k, v in values.items()):  # never write a mangled file
                raise ValueError("settings.toml couldn't be updated safely; edit it by hand")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(new, encoding="utf-8")
            os.replace(tmp, path)
            for key, value in values.items():
                object.__setattr__(self, key, value)

    def apply_file(self, old: Settings, new: Settings) -> dict[str, list[str]]:
        """Apply what changed between two readings of settings.toml (D-127).

        Only keys whose value in the file changed are touched, so values set another way
        (the Settings page, tests) stay. Returns ``{"applied": [...], "restart": [...]}``:
        the keys now in effect, and the changed RESTART_SETTINGS, which keep their old
        value until Basecamp restarts. A key taken out of the file goes back to its default.
        """
        applied, restart = [], []
        with _save_lock:  # not halfway through save_page_settings
            for f in dataclasses.fields(self):
                value = getattr(new, f.name)
                if f.name == "data_dir" or getattr(old, f.name) == value:
                    continue
                if getattr(self, f.name) == value:  # back to what's running, or set already
                    continue
                if f.name in RESTART_SETTINGS:
                    restart.append(f.name)
                else:
                    object.__setattr__(self, f.name, value)
                    applied.append(f.name)
        return {"applied": applied, "restart": restart}

    @classmethod
    def read_file(cls, data_dir: Path) -> Settings:
        """settings.toml on its own (no overrides), with each value's type checked.
        ValueError if it isn't valid TOML or has an unknown key or a wrongly typed value."""
        try:
            return cls._build(data_dir, _file_values(data_dir), check_types=True)
        except TypeError as e:  # e.g. a list where a path or a number goes
            raise ValueError(f"settings.toml: {e}") from None

    @classmethod
    def load(cls, data_dir: Path | str | None = None, **overrides: object) -> Settings:
        """Build settings from the data folder's settings.toml, then explicit overrides."""
        if data_dir is None:
            data_dir = os.environ.get("BASECAMP_DATA_DIR") or Path.cwd() / "data"
        data_dir = Path(data_dir).resolve()
        values = _file_values(data_dir)
        values.update(overrides)
        return cls._build(data_dir, values)

    @classmethod
    def _build(cls, data_dir: Path, values: dict[str, object], *,
               check_types: bool = False) -> Settings:
        known = {f.name for f in dataclasses.fields(cls)} - {"data_dir"}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
        for key in ("migrations_dir", "templates_dir"):
            if key in values:
                values[key] = Path(values[key])  # type: ignore[arg-type]
        for key in ("admin_logins", "friend_control_logins"):
            if key in values:
                logins = values[key]
                if isinstance(logins, str):
                    logins = [logins]
                values[key] = tuple(str(x).strip() for x in logins)  # type: ignore[union-attr]
        if values.get("friend_control", "off") not in FRIEND_CONTROL:
            raise ValueError(f"friend_control should be one of {', '.join(FRIEND_CONTROL)}")
        if "notify_retry_s" in values:
            values["notify_retry_s"] = tuple(float(x) for x in values["notify_retry_s"])  # type: ignore[union-attr]
        if check_types:
            _check_types(cls, values)
        return cls(data_dir=data_dir, **values)  # type: ignore[arg-type]


class SettingsFile:
    """Picks up edits to settings.toml without a restart (D-127): on **Reload settings**,
    and when the file's modified time or size changes, looked at no more than every
    ``every_s`` (the web app asks on each request). A file that doesn't load keeps the
    settings as they were and shows why."""

    def __init__(self, settings: Settings,
                 on_reload: Callable[[str, dict[str, list[str]]], None] | None = None,
                 every_s: float = 3.0) -> None:
        self._settings = settings
        self._on_reload = on_reload
        self._every_s = every_s
        self._lock = threading.Lock()
        self._next = 0.0
        self._seen = self._signature()
        self.error = ""
        self.loaded_at = time.time()
        try:  # the file as last read: a reload applies only what changed since
            self._file = Settings.read_file(settings.data_dir)
        except (OSError, ValueError) as e:
            self._file = dataclasses.replace(settings)
            self.error = str(e)

    @property
    def path(self) -> Path:
        return self._settings.data_dir / "settings.toml"

    def _signature(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def due(self) -> bool:
        return time.monotonic() >= self._next

    def check(self) -> dict[str, list[str]] | None:
        """Reload if the file changed since it was last read; None if it didn't."""
        with self._lock:
            if not self.due():
                return None
            self._next = time.monotonic() + self._every_s
            if self._signature() == self._seen:
                return None
        return self.reload("settings.toml")

    def reload(self, who: str) -> dict[str, list[str]]:
        """Read it now. ``who`` is for the audit log ("settings.toml" for a change seen)."""
        with self._lock:
            self._seen = self._signature()
            try:
                new = Settings.read_file(self._settings.data_dir)
                result = self._settings.apply_file(self._file, new)
                self._file = new
            except (OSError, ValueError) as e:
                self.error = str(e)
                log.warning("settings.toml not applied: %s", e)
                result = {"applied": [], "restart": [], "error": [self.error]}
            else:
                self.error = ""
                self.loaded_at = time.time()
                if result["applied"] or result["restart"]:
                    log.info("settings.toml reloaded: applied %s; after a restart %s",
                             result["applied"] or "nothing", result["restart"] or "nothing")
        if self._on_reload is not None:
            self._on_reload(who, result)
        return result

    def view(self) -> dict:
        pending = []
        for key in RESTART_SETTINGS:
            now, after = getattr(self._settings, key), getattr(self._file, key)
            if after != now:
                pending.append({"key": key, "now": str(now), "after": str(after)})
        return {"error": self.error, "loaded_at": self.loaded_at, "restart_pending": pending,
                "restart_keys": list(RESTART_SETTINGS), "every_s": self._every_s}


def _file_values(data_dir: Path) -> dict[str, object]:
    """The ``[basecamp]`` table of ``<data>\\settings.toml`` ({} if there's no file)."""
    toml_path = data_dir / "settings.toml"
    if not toml_path.is_file():
        return {}
    try:
        with toml_path.open("rb") as f:
            return dict(tomllib.load(f).get("basecamp", {}))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"settings.toml isn't valid TOML: {e}") from None


def _check_types(cls: type, values: dict[str, object]) -> None:
    """A reloaded value must have its setting's type (an int will do for a float), so a
    typo like ``port = "8150x"`` is refused instead of breaking things later."""
    defaults = {f.name: f.default for f in dataclasses.fields(cls)}
    for key, value in values.items():
        want = defaults.get(key)
        if isinstance(want, bool) or isinstance(value, bool):
            ok = isinstance(value, bool) and isinstance(want, bool)
        elif isinstance(want, float):
            ok = isinstance(value, int | float)
            if ok:
                values[key] = float(value)  # type: ignore[arg-type]
        else:  # lists and paths are already tuples and Paths here
            ok = isinstance(value, type(want))
        if not ok:
            raise ValueError(f"{key} should be {type(want).__name__}, not {value!r}")


_HEADER = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(#.*)?$")


def _set_toml_keys(text: str, table: str, values: dict[str, object]) -> str:
    """``text`` with ``key = value`` lines set under ``[table]`` (added if missing)."""
    lines = text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        m = _HEADER.match(line)
        if m and start is None and m[1] == table:
            start = i
        elif m and start is not None:
            end = i
            break
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{table}]")
        start = len(lines) - 1
    if end is None:
        end = len(lines)
    for key, value in values.items():
        line = f"{key} = {_toml_value(value)}"
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for i in range(start + 1, end):
            if pattern.match(lines[i]):
                lines[i] = line
                break
        else:
            last = start  # after the table's last non-blank line
            for i in range(start + 1, end):
                if lines[i].strip():
                    last = i
            lines.insert(last + 1, line)
            end += 1
    return "\n".join(lines) + "\n"


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # JSON escapes are valid TOML strings
    if isinstance(value, list | tuple) and all(isinstance(x, str) for x in value):
        return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in value) + "]"
    raise TypeError(f"can't write {type(value).__name__} to settings.toml")
