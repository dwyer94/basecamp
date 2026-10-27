"""Paths, port and tunables.

The data folder comes from ``BASECAMP_DATA_DIR`` (prod: ``C:\\BasecampData``), else ``.\\data``.
Optional overrides live in ``<data>\\settings.toml`` under ``[basecamp]``. Secrets never go here;
they belong in ``<data>\\secrets.toml`` (read by the modules that need them, from Phase 2 on).
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# migrations/ ships next to the basecamp/ package, in the repo and in release zips (docs/07).
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# Built-in game templates (R-13), shipped the same way; <data>/templates/ can add or override.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
DEFAULT_STEAMCMD = r"%USERPROFILE%\Desktop\SteamCmd\steamcmd.exe"


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
    # R-05: an exit nobody asked for restarts after crash_restart_delay_s, unless it is the
    # crash_limit-th crash within crash_window_s: then the server stays Crashed.
    crash_restart_delay_s: float = 10.0
    crash_limit: int = 3
    crash_window_s: float = 600.0
    # R-53: where save backups go ("" = <data>ackups). Q-08: another drive is fine here.
    backups_root: str = ""
    # How often the backup scheduler looks for servers that are due (R-50).
    backup_check_s: float = 60.0
    # R-62: waits between tries of a Discord message that failed (one try per entry, + 1).
    notify_retry_s: tuple[float, ...] = (5.0, 30.0, 120.0)
    # R-31: steamcmd.exe ("" = %USERPROFILE%\Desktop\SteamCmd\steamcmd.exe), and how
    # long one SteamCMD run may take before it is killed.
    steamcmd_path: str = ""
    steamcmd_timeout_s: float = 3600.0

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
        """Webhook URLs (and later RCON passwords). Never in the repo or settings.toml."""
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

    @classmethod
    def load(cls, data_dir: Path | str | None = None, **overrides: object) -> Settings:
        """Build settings from the data folder's settings.toml, then explicit overrides."""
        if data_dir is None:
            data_dir = os.environ.get("BASECAMP_DATA_DIR") or Path.cwd() / "data"
        data_dir = Path(data_dir).resolve()
        values: dict[str, object] = {}
        toml_path = data_dir / "settings.toml"
        if toml_path.is_file():
            with toml_path.open("rb") as f:
                values.update(tomllib.load(f).get("basecamp", {}))
        values.update(overrides)
        known = {f.name for f in dataclasses.fields(cls)} - {"data_dir"}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
        for key in ("migrations_dir", "templates_dir"):
            if key in values:
                values[key] = Path(values[key])  # type: ignore[arg-type]
        if "admin_logins" in values:
            logins = values["admin_logins"]
            if isinstance(logins, str):
                logins = [logins]
            values["admin_logins"] = tuple(str(x).strip() for x in logins)  # type: ignore[union-attr]
        if "notify_retry_s" in values:
            values["notify_retry_s"] = tuple(float(x) for x in values["notify_retry_s"])  # type: ignore[union-attr]
        return cls(data_dir=data_dir, **values)  # type: ignore[arg-type]
