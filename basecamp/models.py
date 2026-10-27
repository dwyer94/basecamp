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
    notes: str = ""

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
