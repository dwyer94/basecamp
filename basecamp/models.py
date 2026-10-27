"""Data models: servers, ports, stop recipes (R-10..R-12, R-22) and run states (R-21)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Protocol(StrEnum):
    UDP = "udp"
    TCP = "tcp"


class ServerKind(StrEnum):
    STEAM = "steam"
    CUSTOM = "custom"


class LogSource(StrEnum):
    STDOUT = "stdout"
    FILE = "file"


class StopAction(StrEnum):
    STDIN = "stdin"  # write `command` to the server's stdin
    RCON = "rcon"  # send `command` over RCON (Phase 2+; skipped until an RCON sender is wired)
    CTRL_BREAK = "ctrl_break"  # Ctrl+Break to the server's console


class ServerState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    UPDATING = "updating"
    CRASHED = "crashed"


class NotifyEvent(StrEnum):
    """Discord messages about one server (R-60). Each can be turned off per server (R-61)."""

    UP = "up"  # Starting -> Running, with the join address
    STOPPED = "stopped"  # stopped from Basecamp
    CRASHED = "crashed"  # an exit nobody asked for (and whether it restarts)
    GAVE_UP = "gave_up"  # too many crashes: left Crashed (R-05)
    BACKUP_FAILED = "backup_failed"
    UPDATE = "update"  # update started / finished / failed (Phase 3)


ACTIVE_STATES = frozenset(
    {ServerState.STARTING, ServerState.RUNNING, ServerState.STOPPING, ServerState.UPDATING}
)


class Port(BaseModel):
    port: int = Field(ge=1, le=65535)
    protocol: Protocol = Protocol.UDP
    label: str = ""


class StopStep(BaseModel):
    """One step of a graceful stop: do `action`, then wait up to `wait_s` for the exit."""

    action: StopAction
    command: str = ""
    wait_s: float = Field(default=10.0, ge=0)

    @model_validator(mode="after")
    def _command_needed(self) -> StopStep:
        if self.action in (StopAction.STDIN, StopAction.RCON) and not self.command.strip():
            raise ValueError(f"a {self.action} stop step needs a command")
        return self


class StopWarning(BaseModel):
    """R-25: tell players before a stop, restart or update that Basecamp starts. ``command``
    is sent (stdin or RCON) at each of ``minutes`` before the stop, with ``{minutes}`` and
    ``{seconds}`` filled in. No minutes = off (D-61)."""

    action: StopAction = StopAction.STDIN
    command: str = ""
    minutes: list[float] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _check(self) -> StopWarning:
        if self.action is StopAction.CTRL_BREAK:
            raise ValueError("a stop warning is sent by stdin or rcon")
        if any(not 0 < m <= 60 for m in self.minutes):
            raise ValueError("warning minutes must be more than 0 and at most 60")
        self.minutes = sorted(set(self.minutes), reverse=True)
        if self.minutes and not self.command.strip():
            raise ValueError("a stop warning needs a command")
        return self

    @property
    def lead_s(self) -> float:
        return self.minutes[0] * 60 if self.minutes else 0.0

    def text(self, minutes: float) -> str:
        """The command for ``minutes`` before the stop. Plain replace, so other braces in
        the command are left alone."""
        return (self.command.replace("{minutes}", f"{minutes:g}")
                .replace("{seconds}", str(round(minutes * 60))))


# R-22: used when a server has no recipe. Kill always follows the last step.
DEFAULT_STOP_RECIPE: tuple[StopStep, ...] = (
    StopStep(action=StopAction.CTRL_BREAK, wait_s=30),
)


class ServerConfig(BaseModel):
    """What the admin enters (R-10..R-12). Secrets such as the RCON password are not here."""

    name: str = Field(min_length=1, max_length=100)
    kind: ServerKind = ServerKind.CUSTOM
    install_dir: str = ""
    exe_path: str = Field(min_length=1)
    args: str = ""
    working_dir: str = ""
    ports: list[Port] = Field(default_factory=list)
    join_port: int | None = Field(default=None, ge=1, le=65535)
    start_on_boot: bool = False
    boot_order: int = 0
    steam_app_id: int | None = Field(default=None, ge=1)
    steam_beta: str = ""
    steam_validate: bool = False
    config_files: list[str] = Field(default_factory=list)
    save_dir: str = ""
    log_source: LogSource = LogSource.STDOUT
    log_file: str = ""
    rcon_host: str = "127.0.0.1"
    rcon_port: int | None = Field(default=None, ge=1, le=65535)
    real_exe_name: str = ""
    ready_pattern: str = ""
    ready_after_s: float = Field(default=15.0, ge=0)
    min_free_ram_mb: int = Field(default=0, ge=0)
    stop_recipe: list[StopStep] = Field(default_factory=list)
    stop_warning: StopWarning | None = None  # R-25: warn players first (None = off)
    notes: str = ""
    template_id: str = ""  # the game template it was made from, if any (R-13)
    backup_every_h: float = Field(default=0, ge=0, le=24 * 30)  # R-50; 0 = no schedule
    backup_keep: int = Field(default=10, ge=1, le=1000)
    notify_off: list[NotifyEvent] = Field(default_factory=list)  # R-61: events not posted

    @model_validator(mode="after")
    def _check(self) -> ServerConfig:
        if self.log_source is LogSource.FILE and not self.log_file.strip():
            raise ValueError("log_source 'file' needs log_file")
        return self

    def effective_stop_recipe(self) -> list[StopStep]:
        return list(self.stop_recipe) or list(DEFAULT_STOP_RECIPE)


class Server(ServerConfig):
    id: int
    created_at: str
    updated_at: str
