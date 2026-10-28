"""RCON and REST channels to a game (D-92..D-97): passwords and kept connections.

The passwords live in ``<data>\\secrets.toml`` with the Discord webhooks, per server:

    [rcon.servers]
    "4" = "..."
    [rest.servers]
    "1" = "..."

They are never logged, never in an audit entry or the API (only whether one is set), and
never in templates. The supervisor sends stop steps, stop warnings and console commands
through ``RemoteConsoles.send``; a failure raises ``RemoteError`` and the supervisor logs
it and goes on (a stop is never blocked by RCON or REST, D-96).
"""

from __future__ import annotations

import logging
import threading

from basecamp import notify
from basecamp.models import Server, StopAction
from basecamp.rcon import RconClient, RconError
from basecamp.rest import RestClient, RestError
from basecamp.settings import Settings

log = logging.getLogger(__name__)

CHANNELS = ("rcon", "rest")


class RemoteError(Exception):
    pass


def passwords(settings: Settings, channel: str) -> dict[int, str]:
    table = notify.load_secrets(settings).get(channel, {})
    servers = table.get("servers", {}) if isinstance(table, dict) else {}
    out = {}
    if isinstance(servers, dict):
        for key, value in servers.items():
            try:
                if value:
                    out[int(key)] = str(value)
            except ValueError:
                continue
    return out


def set_password(settings: Settings, channel: str, server_id: int, password: str) -> None:
    """Set (or with "" remove) one server's RCON or REST password."""
    if channel not in CHANNELS:
        raise ValueError(channel)
    data = notify.load_secrets(settings)
    table = data.setdefault(channel, {})
    servers = table.setdefault("servers", {})
    if password:
        servers[str(server_id)] = password
    else:
        servers.pop(str(server_id), None)
    if not servers:
        table.pop("servers")
    if not table:
        data.pop(channel)
    notify.save_secrets(settings, data)


class RemoteConsoles:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._mu = threading.Lock()
        # server id -> (port, password, client): a changed port or password gets a new one
        self._rcon: dict[int, tuple[str, int, str, RconClient]] = {}

    # -- what's configured ----------------------------------------------------------

    def configured(self, server: Server) -> dict[str, bool]:
        """Which channels can be used: a port in the server's form and a password."""
        return {
            "rcon": bool(server.rcon_port and passwords(self._settings, "rcon").get(server.id)),
            "rest": bool(server.rest_port and passwords(self._settings, "rest").get(server.id)),
        }

    def has_password(self, server_id: int) -> dict[str, bool]:
        return {ch: server_id in passwords(self._settings, ch) for ch in CHANNELS}

    def set_password(self, server_id: int, channel: str, password: str) -> None:
        set_password(self._settings, channel, server_id, password)
        self.forget(server_id)

    def default_channel(self, server: Server) -> str:
        """The console's channel when none is picked: RCON, else REST, else stdin."""
        ok = self.configured(server)
        return "rcon" if ok["rcon"] else "rest" if ok["rest"] else "stdin"

    # -- sending --------------------------------------------------------------------

    def send(self, server: Server, action: StopAction | str, command: str) -> str:
        """Run ``command`` over RCON or REST and return the answer. Raises RemoteError
        with a message fit for the console (never the password)."""
        channel = StopAction(action).value
        if channel not in CHANNELS:
            raise RemoteError(f"{channel} isn't RCON or REST")
        port = server.rcon_port if channel == "rcon" else server.rest_port
        if not port:
            raise RemoteError(f"no {channel.upper()} port in the server's settings")
        password = passwords(self._settings, channel).get(server.id)
        if not password:
            raise RemoteError(f"no {channel.upper()} password set for this server")
        host = server.rcon_host or "127.0.0.1"
        allow = self._settings.remote_console_allow_remote
        try:
            if channel == "rest":
                client = RestClient(host, port, password,
                                    timeout=self._settings.remote_timeout_s, allow_remote=allow)
                return client.command(command)
            return self._rcon_client(server.id, host, port, password).command(command)
        except (RconError, RestError) as e:
            raise RemoteError(str(e)) from None

    def forget(self, server_id: int) -> None:
        """Close a kept RCON connection (the server stopped, or its settings changed)."""
        with self._mu:
            entry = self._rcon.pop(server_id, None)
        if entry is not None:
            entry[3].close()

    def close(self) -> None:
        with self._mu:
            entries, self._rcon = list(self._rcon.values()), {}
        for entry in entries:
            entry[3].close()

    def _rcon_client(self, server_id: int, host: str, port: int,
                     password: str) -> RconClient:
        with self._mu:
            entry = self._rcon.get(server_id)
            if entry is not None and entry[:3] == (host, port, password):
                return entry[3]
            client = RconClient(host, port, password, timeout=self._settings.remote_timeout_s,
                                allow_remote=self._settings.remote_console_allow_remote)
            self._rcon[server_id] = (host, port, password, client)
        if entry is not None:
            entry[3].close()
        return client
