"""The web app: REST API, status and console WebSockets, and the two pages (Phase 2).

Who is asking comes from basecamp/auth.py. Admin-only routes use ``Depends(admin)``.
Friends can reach ``/``, ``/api/me``, ``GET /api/servers`` and ``/ws/status``, and get only
the friend fields (views.FRIEND_FIELDS).

Supervisor calls block (a stop can take minutes), so routes are plain ``def`` (run in the
thread pool), and stop/restart/kill run on their own thread and answer 202 at once; the
page follows along on the status WebSocket and the console.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from basecamp import __version__, auth, configs, firewall, notify, templates
from basecamp.api import views
from basecamp.backups import BackupError, Backups, save_folder
from basecamp.configs import ConfigFiles
from basecamp.console import ConsoleLine
from basecamp.crashes import CrashPolicy
from basecamp.db import Store
from basecamp.livestatus import LiveStatus
from basecamp.models import ACTIVE_STATES, NotifyEvent, ServerConfig, ServerKind, ServerState
from basecamp.notify import Notifier
from basecamp.players import Players
from basecamp.schedule import Schedules
from basecamp.selfupdate import SelfUpdater, UpdateError
from basecamp.settings import PAGE_SETTINGS, Settings, SettingsFile
from basecamp.setup import SetupError, Setups
from basecamp.steam import Kind, SteamError, SteamRunner
from basecamp.supervisor import InvalidState, PortInUse, Supervisor, SupervisorError

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STARTED_AT = time.time()  # tells the page a restart happened (D-126)
STATUS_INTERVAL_S = 5.0  # uptime, CPU and RAM refresh on the status WebSocket
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_CONSOLE_QUEUE_MAX = 5000  # a console client this far behind is dropped
mimetypes.add_type("application/manifest+json", ".webmanifest")  # add to home screen (R-72)


Channel = Literal["", "stdin", "rcon", "rest"]


class CommandIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    channel: Channel = ""  # "" = the server's default: RCON, else REST, else stdin


class RemoteIn(BaseModel):
    """RCON and REST passwords (D-97). Left out = unchanged; "" removes it."""

    rcon_password: str | None = Field(default=None, max_length=200)
    rest_password: str | None = Field(default=None, max_length=200)


class WebhookIn(BaseModel):
    webhook_url: str = Field(default="", max_length=300)  # "" removes it


class ConfigSaveIn(BaseModel):
    text: str = Field(max_length=configs.MAX_BYTES)
    # The file's hash as the page loaded it ("" = it didn't exist); a mismatch is a 409
    # unless force is set (the admin chose to overwrite what the game wrote).
    base_sha256: str = ""
    force: bool = False


_JOIN_HOST = r"^[A-Za-z0-9._\-\[\]:]*$"
_LOGIN = r"^ *[^\s\x00-\x1f\"'\\]* *$"  # a Tailscale login, e.g. pal@example.com or pal@github


class SettingsIn(BaseModel):
    """The Settings page (D-52). Fields left out are not changed."""

    join_host: str | None = Field(default=None, max_length=253, pattern=_JOIN_HOST)
    steamcmd_path: str | None = Field(default=None, max_length=400, pattern=r"^[^\x00-\x1f]*$")
    default_backup_every_h: float | None = Field(default=None, ge=0, le=24 * 30)
    default_backup_keep: int | None = Field(default=None, ge=1, le=1000)
    update_beta: bool | None = None
    update_auto_hour: int | None = Field(default=None, ge=-1, le=23)  # -1 = off
    discord_live_status: bool | None = None  # R-63
    friend_control: Literal["off", "logins", "anyone"] | None = None  # D-131
    friend_control_logins: list[Annotated[str, Field(max_length=200, pattern=_LOGIN)]] | None \
        = Field(default=None, max_length=100)
    # Also give every existing server the backup defaults.
    apply_backup_defaults: bool = False


class TemplateIn(BaseModel):
    """A template pasted or uploaded on the Templates page (D-129)."""

    text: str = Field(min_length=1, max_length=templates.MAX_BYTES)
    replace: bool = False  # override a built-in, or replace a data-folder template
    dry_run: bool = False  # only check it


class DraftIn(BaseModel):
    install_dir: str = ""
    template_id: str = ""  # "" = detect from the install's appmanifest (R-15)
    name: str = ""


class SetupIn(BaseModel):
    """New game from a template (D-130). No install folder = SteamCMD's default."""

    template_id: str = Field(min_length=1, max_length=63)
    install_dir: str = Field(default="", max_length=400)
    name: str = Field(default="", max_length=100)


class SetupValuesIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict, max_length=20)  # by place: "0", "1"


def _who(request: Request) -> auth.Who:
    return request.state.who  # set by the middleware in create_app


def _admin(request: Request) -> auth.Who:
    w: auth.Who = request.state.who
    if not w.is_admin:
        raise HTTPException(403, "Admins only")
    return w


# Route parameter types (module level, so FastAPI can resolve them).
Who = Annotated[auth.Who, Depends(_who)]
Admin = Annotated[auth.Who, Depends(_admin)]


def _console_line(line: ConsoleLine) -> dict:
    return {"seq": line.seq, "ts": line.ts, "source": line.source.value, "text": line.text}


class _Hub:
    """Wakes every status WebSocket when a server changes state (called from supervisor
    threads)."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._events: set[asyncio.Event] = set()

    def register(self) -> asyncio.Event:
        event = asyncio.Event()
        self._events.add(event)
        return event

    def unregister(self, event: asyncio.Event) -> None:
        self._events.discard(event)

    def poke(self, *_: object) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # loop shutting down
            loop.call_soon_threadsafe(self._wake)

    def _wake(self) -> None:
        for event in list(self._events):
            event.set()


def create_app(store: Store, sup: Supervisor, settings: Settings, *,
               notifier: Notifier | None = None, backups: Backups | None = None,
               crashes: CrashPolicy | None = None,
               steam: SteamRunner | None = None,
               config_files: ConfigFiles | None = None,
               updater: SelfUpdater | None = None,
               live: LiveStatus | None = None,
               settings_check_s: float = 3.0,
               fw: firewall.Firewall | None = None,
               setups: Setups | None = None,
               players: Players | None = None,
               schedules: Schedules | None = None) -> FastAPI:
    """The services are made by main.run; tests may leave them out (no crash restarts)."""
    hub = _Hub()
    cpu = views.CpuSampler()
    notifier = notifier or Notifier(settings, store)
    backups = backups or Backups(store, sup, settings, notifier.server_event)
    steam = steam or SteamRunner(store, sup, settings, backups, notifier)
    config_files = config_files or ConfigFiles(settings)
    updater = updater or SelfUpdater(settings, store, notifier)
    live = live or LiveStatus(settings, store, sup)
    players = players or Players(store, sup, settings)  # main.run starts its polling
    def settings_reloaded(who: str, result: dict[str, list[str]]) -> None:
        """Audit a settings.toml reload (D-127): key names only, never values."""
        if result.get("error"):
            detail = f"not applied: {result['error'][0]}"
        elif result["applied"] or result["restart"]:
            detail = "; ".join(part for part in (
                result["applied"] and "applied " + ", ".join(result["applied"]),
                result["restart"] and "after a restart " + ", ".join(result["restart"]))
                if part)
        elif who == "settings.toml":
            return  # saved by the Settings page, or touched without a change
        else:
            detail = "no changes"
        store.add_audit(who, "settings.reload", None, detail)
        log.info("audit: %s settings.reload %s", who, detail)
        if result["applied"]:
            hub.poke()  # join addresses, admin or friend
            live.poke()

    settings_file = SettingsFile(settings, settings_reloaded, settings_check_s)
    fw = fw or firewall.Firewall()

    def audit_as(who: str, action: str, server_id: int | None, detail: str) -> None:
        store.add_audit(who, action, server_id, detail)
        log.info("audit: %s %s server=%s %s", who, action, server_id, detail)

    setups = setups or Setups(store, sup, steam, config_files, settings, audit=audit_as,
                              cancel_restart=lambda sid: crashes.cancel(sid) if crashes
                              else None)
    schedules = schedules or Schedules(store, sup, steam, players, settings,
                                       audit=audit_as)  # main.run starts its loop
    sup.add_listener(hub.poke)
    sup.add_change_listener(hub.poke)
    steam.add_listener(hub.poke)
    players.add_listener(hub.poke)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        hub.loop = asyncio.get_running_loop()
        yield
        hub.loop = None

    app = FastAPI(title="Basecamp", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    # -- who is asking ----------------------------------------------------------

    @app.middleware("http")
    async def identify(request: Request, call_next: Callable) -> Any:
        if not auth.host_allowed(request.headers.get("host")):
            return JSONResponse({"detail": "Unknown host name"}, status_code=400)
        if request.method in _UNSAFE and not auth.check_origin(request.headers):
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
        # settings.toml edited by hand (D-127): admin_logins apply at once. Reload settings
        # reads it itself, so its answer says what changed.
        if settings_file.due() and request.url.path != "/api/settings/reload":
            await run_in_threadpool(settings_file.check)
        peer = request.client.host if request.client else None
        request.state.who = auth.identify(peer, request.headers, settings)
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # Ask each time (a 304 when unchanged), so after an update a phone never runs
            # a new app.js against a cached common.js.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def audit(w: auth.Who, action: str, server_id: int | None = None, detail: str = "") -> None:
        store.add_audit(w.label, action, server_id, detail)
        log.info("audit: %s %s server=%s %s", w.label, action, server_id, detail)

    def get_server(server_id: int):  # type: ignore[no-untyped-def]
        try:
            return store.get_server(server_id)
        except KeyError:
            raise HTTPException(404, "No such server") from None

    def admin_view(server, status) -> dict:  # type: ignore[no-untyped-def]
        restart_at = crashes.restart_at(server.id) if crashes else None
        return views.admin_view(settings, server, status, cpu, restart_at,
                                steam.view(server), remote_view(server),
                                players.admin_view(server), schedules.view(server))

    def remote_view(server) -> dict:  # type: ignore[no-untyped-def]
        """Which console channels work, and whether each password is set (never it)."""
        ok = sup.remote.configured(server)
        return {"channels": ["stdin", *[ch for ch in ("rcon", "rest") if ok[ch]]],
                "default": sup.remote.default_channel(server),
                "password_set": sup.remote.has_password(server.id)}

    def not_updating(server_id: int) -> None:
        """Start, stop, restart and restore wait for the server's lock, which an update
        holds for as long as SteamCMD runs: refuse them instead (kill still works)."""
        if steam.busy(server_id):
            raise HTTPException(409, "An install or update is running; cancel it first")

    def cancel_restart(server_id: int) -> None:
        if crashes is not None:
            crashes.cancel(server_id)

    def server_list(w: auth.Who) -> list[dict]:
        out = []
        servers = store.list_servers()
        pids = set()
        for server in servers:
            status = sup.status(server.id)
            if status.pid is not None:
                pids.add(status.pid)
            if w.is_admin:
                out.append(admin_view(server, status))
            else:
                out.append(views.friend_view(settings, server, status,
                                             players.friend_view(server)))
        cpu.forget_others(pids)
        return out

    # -- pages --------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index(w: Who) -> FileResponse:
        page = "index.html" if w.is_admin else "friend.html"
        return FileResponse(WEB_DIR / page, headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz(request: Request) -> dict:
        peer = request.client.host if request.client else None
        if peer not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "localhost only")
        return {"ok": True, "version": __version__}

    # -- who am I ---------------------------------------------------------------------

    @app.get("/api/me")
    def me(w: Who) -> dict:
        info: dict[str, Any] = {"login": w.login, "name": w.name, "is_admin": w.is_admin,
                                "can_control": auth.can_control(w, settings),
                                "version": __version__, "started_at": STARTED_AT}
        if w.is_admin:
            info["join_host"] = settings.join_host
            info["backup_defaults"] = {"backup_every_h": settings.default_backup_every_h,
                                       "backup_keep": settings.default_backup_keep}
        return info

    # -- servers ---------------------------------------------------------------------

    @app.get("/api/servers")
    def list_servers(w: Who) -> list[dict]:
        return server_list(w)

    @app.get("/api/servers/{server_id}")
    def read_server(server_id: int, w: Who) -> dict:
        server = get_server(server_id)
        status = sup.status(server_id)
        if w.is_admin:
            return admin_view(server, status)
        return views.friend_view(settings, server, status, players.friend_view(server))

    def save(config: ServerConfig, server_id: int | None = None) -> Any:
        try:
            if server_id is None:
                return store.add_server(config)
            return store.update_server(server_id, config)
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"A server called {config.name!r} already exists") from None
        except KeyError:
            raise HTTPException(404, "No such server") from None

    @app.post("/api/servers", status_code=201)
    def add_server(config: ServerConfig, w: Admin) -> dict:
        server = save(config)
        audit(w, "server.add", server.id, server.name)
        hub.poke()
        return admin_view(server, sup.status(server.id))

    @app.put("/api/servers/{server_id}")
    def update_server(server_id: int, config: ServerConfig, w: Admin) -> dict:
        get_server(server_id)
        server = save(config, server_id)
        audit(w, "server.update", server_id, server.name)
        hub.poke()
        return admin_view(server, sup.status(server_id))

    @app.delete("/api/servers/{server_id}", status_code=204)
    def delete_server(server_id: int, w: Admin) -> None:
        server = get_server(server_id)
        if sup.state(server_id) in ACTIVE_STATES:
            raise HTTPException(409, "Stop the server before deleting it")
        if steam.job(server_id) is not None:
            raise HTTPException(409, "Cancel its queued install or update first")
        if setups.running(server_id):
            raise HTTPException(409, "Its new game setup is running a step; wait for it")
        cancel_restart(server_id)
        store.delete_server(server_id)  # game files are never touched (R-14)
        audit(w, "server.delete", server_id, server.name)
        hub.poke()

    # -- actions ------------------------------------------------------------------

    def in_background(name: str, server_id: int, fn: Callable[[], object]) -> None:
        def run() -> None:
            try:
                fn()
            except SupervisorError as e:
                sup.console(server_id).system(f"{name.capitalize()} failed: {e}")
            except Exception:
                log.exception("%s of server %s failed", name, server_id)
                sup.console(server_id).system(f"{name.capitalize()} failed; see basecamp.log")

        threading.Thread(target=run, name=f"{name} {server_id}", daemon=True).start()

    @app.post("/api/servers/{server_id}/start")
    def start(server_id: int, w: Admin) -> dict:
        server = get_server(server_id)
        not_updating(server_id)
        audit(w, "start", server_id, server.name)
        try:
            result = sup.start(server_id)
        except PortInUse as e:
            raise HTTPException(409, str(e)) from None
        except InvalidState as e:
            raise HTTPException(409, str(e)) from None
        except SupervisorError as e:
            raise HTTPException(500, str(e)) from None
        return {"pid": result.pid, "warnings": result.warnings}

    def not_counting_down(server_id: int) -> None:
        if sup.counting_down(server_id):
            raise HTTPException(409, "It's already counting down to a stop; use Stop now or "
                                     "Kill")

    @app.post("/api/servers/{server_id}/stop", status_code=202)
    def stop(server_id: int, w: Admin, now: bool = False) -> dict:
        """Stop, after the server's stop warning (R-25). ``now`` skips the warning, and
        during a countdown (of a stop, restart or update) skips the rest of it."""
        server = get_server(server_id)
        if now and sup.stop_now(server_id):
            audit(w, "stop.now", server_id, server.name)
            return {"accepted": True, "skipped_warning": True}
        not_updating(server_id)
        not_counting_down(server_id)
        audit(w, "stop", server_id, server.name + (" (no warning)" if now else ""))
        cancel_restart(server_id)
        in_background("stop", server_id, lambda: sup.stop(server_id, warn=not now))
        return {"accepted": True}

    @app.post("/api/servers/{server_id}/restart", status_code=202)
    def restart(server_id: int, w: Admin, now: bool = False) -> dict:
        server = get_server(server_id)
        not_updating(server_id)
        not_counting_down(server_id)
        audit(w, "restart", server_id, server.name + (" (no warning)" if now else ""))
        in_background("restart", server_id, lambda: sup.restart(server_id, warn=not now))
        return {"accepted": True}

    @app.post("/api/servers/{server_id}/kill", status_code=202)
    def kill(server_id: int, w: Admin) -> dict:
        server = get_server(server_id)
        audit(w, "kill", server_id, server.name)
        cancel_restart(server_id)
        in_background("kill", server_id, lambda: sup.kill(server_id))
        return {"accepted": True}

    # -- friend start, stop, restart (D-131) ------------------------------------------

    friend_clicks: dict[int, float] = {}  # server id -> when a friend last clicked
    friend_lock = threading.Lock()

    def friend_action(server_id: int, w: auth.Who, action: str) -> Any:
        """Start, stop or restart from the friend view, for whoever the friend permission
        lets in. Nothing else: no kill, no Stop now, the stop warning always runs (R-25).
        One click per server per ``friend_control_gap_s``, whoever clicks."""
        if not auth.can_control(w, settings):
            raise HTTPException(403, "Not allowed")
        server = get_server(server_id)
        who = w.login or "unknown"
        not_updating(server_id)
        not_counting_down(server_id)
        state = sup.state(server_id)
        if action == "start":
            if state in ACTIVE_STATES:
                raise HTTPException(409, f"{server.name} is {state.value}")
            steam_view = steam.view(server)
            if steam_view and steam_view["failed"]:
                raise HTTPException(409, "Its last install or update failed; ask the admin")
        elif state not in (ServerState.STARTING, ServerState.RUNNING):
            raise HTTPException(409, f"{server.name} is {state.value}")
        with friend_lock:
            now = time.monotonic()
            last = friend_clicks.get(server_id)
            gap = settings.friend_control_gap_s
            if last is not None and now - last < gap:
                wait = round(gap - (now - last))
                raise HTTPException(429, f"Someone just did that; try again in {wait} s")
            friend_clicks[server_id] = now
        store.add_audit(who, f"friend.{action}", server_id, server.name)
        log.info("audit: %s friend.%s server=%s", who, action, server_id)
        if action == "start":
            try:
                sup.start(server_id)
            except SupervisorError as e:
                raise HTTPException(409, str(e)) from None
            return {"accepted": True}
        if action == "stop":
            cancel_restart(server_id)
            in_background("stop", server_id, lambda: sup.stop(server_id, warn=True))
        else:
            in_background("restart", server_id, lambda: sup.restart(server_id, warn=True))
        lead = server.stop_warning.lead_s if server.stop_warning else 0
        return {"accepted": True, "warning_s": lead if state is ServerState.RUNNING else 0}

    @app.post("/api/friend/servers/{server_id}/start", status_code=202)
    def friend_start(server_id: int, w: Who) -> dict:
        return friend_action(server_id, w, "start")

    @app.post("/api/friend/servers/{server_id}/stop", status_code=202)
    def friend_stop(server_id: int, w: Who) -> dict:
        return friend_action(server_id, w, "stop")

    @app.post("/api/friend/servers/{server_id}/restart", status_code=202)
    def friend_restart(server_id: int, w: Who) -> dict:
        return friend_action(server_id, w, "restart")

    def send(server_id: int, text: str, w: auth.Who, channel: str = "") -> None:
        server = get_server(server_id)
        channel = channel or sup.remote.default_channel(server)
        audit(w, "console", server_id, text if channel == "stdin" else f"({channel}) {text}")
        try:
            sup.send_command(server_id, text, channel)
        except SupervisorError as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/api/servers/{server_id}/command", status_code=204)
    def command(server_id: int, body: CommandIn, w: Admin) -> None:
        send(server_id, body.text, w, body.channel)

    @app.put("/api/servers/{server_id}/remote")
    def set_remote(server_id: int, body: RemoteIn, w: Admin) -> dict:
        """Set or remove the RCON and REST passwords, kept in secrets.toml (D-97)."""
        server = get_server(server_id)
        for channel, value in (("rcon", body.rcon_password), ("rest", body.rest_password)):
            if value is None:
                continue
            sup.remote.set_password(server_id, channel, value)
            audit(w, f"{channel}.password", server_id, "set" if value else "removed")
        return remote_view(server)

    @app.get("/api/servers/{server_id}/console")
    def console(server_id: int, w: Admin, after: int = 0) -> list[dict]:
        get_server(server_id)
        return [_console_line(line) for line in sup.console(server_id).lines(after)]

    # -- SteamCMD installs and updates (R-11, R-30..R-32) ------------------------------

    def queue_steam(server_id: int, kind: Kind, w: auth.Who) -> dict:
        server = get_server(server_id)
        audit(w, kind.value, server_id, server.name)
        try:
            job = steam.enqueue(server_id, kind, w.label)
        except SteamError as e:
            raise HTTPException(409, str(e)) from None
        except KeyError:
            raise HTTPException(404, "No such server") from None
        cancel_restart(server_id)  # the update starts it again if it was running
        return {"accepted": True, "run_id": job.run_id}

    @app.post("/api/servers/{server_id}/update", status_code=202)
    def update(server_id: int, w: Admin) -> dict:
        return queue_steam(server_id, Kind.UPDATE, w)

    @app.post("/api/servers/{server_id}/install", status_code=202)
    def install(server_id: int, w: Admin) -> dict:
        return queue_steam(server_id, Kind.INSTALL, w)

    @app.post("/api/servers/{server_id}/steam/check", status_code=202)
    def check_build(server_id: int, w: Admin) -> dict:
        """Ask Steam for the latest build (R-34), through the SteamCMD queue."""
        server = get_server(server_id)
        try:
            queued = steam.check(server_id, w.label)
        except SteamError as e:
            raise HTTPException(409, str(e)) from None
        if queued:
            audit(w, "steam.check", server_id, server.name)
        return {"accepted": True, "queued": queued}

    @app.post("/api/servers/{server_id}/steam/cancel")
    def cancel_steam(server_id: int, w: Admin) -> dict:
        server = get_server(server_id)
        audit(w, "steam.cancel", server_id, server.name)
        if not steam.cancel(server_id):
            raise HTTPException(409, "Nothing to cancel")
        return {"cancelled": True}

    @app.get("/api/steam")
    def steam_queue(w: Admin) -> dict:
        exe = settings.steamcmd_exe
        return {"steamcmd": str(exe), "steamcmd_found": exe.is_file(),
                "timeout_s": settings.steamcmd_timeout_s, "jobs": steam.jobs()}

    @app.get("/api/servers/{server_id}/steam/runs/{run_id}/log")
    def steam_log(server_id: int, run_id: int, w: Admin) -> PlainTextResponse:
        get_server(server_id)
        try:
            run = store.get_steam_run(run_id)
            if run.server_id != server_id:
                raise KeyError(run_id)
            text = steam.log_text(server_id, run_id)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "No output for that run") from None
        return PlainTextResponse(text)

    # -- backups (R-50..R-53) -----------------------------------------------------------

    def backup_error(e: Exception) -> HTTPException:
        if isinstance(e, FileNotFoundError):
            return HTTPException(404, "No such backup")
        return HTTPException(409, str(e))

    @app.get("/api/servers/{server_id}/backups")
    def list_backups(server_id: int, w: Admin) -> dict:
        server = get_server(server_id)
        source = save_folder(server)
        return {
            "backups": [b.as_dict() for b in backups.list(server_id)],
            "save_dir": str(source) if source else "",
            "save_dir_exists": bool(source and source.is_dir()),
            "folder": str(settings.server_backup_dir(server_id)),
            "every_h": server.backup_every_h,
            "keep": server.backup_keep,
        }

    @app.post("/api/servers/{server_id}/backups", status_code=201)
    def make_backup(server_id: int, w: Admin) -> dict:
        server = get_server(server_id)
        try:
            backup = backups.make(server, "manual")
        except BackupError as e:
            raise backup_error(e) from None
        removed = backups.prune(server)
        audit(w, "backup", server_id, backup.name)
        return {"backup": backup.as_dict(), "removed": removed}

    @app.post("/api/servers/{server_id}/backups/{name}/restore")
    def restore_backup(server_id: int, name: str, w: Admin) -> dict:
        server = get_server(server_id)
        not_updating(server_id)
        audit(w, "backup.restore", server_id, name)
        try:
            safety = backups.restore(server, name)
        except (BackupError, FileNotFoundError) as e:
            raise backup_error(e) from None
        removed = backups.prune(server)
        return {"restored": name, "safety_backup": safety.name if safety else None,
                "removed": removed}

    @app.get("/api/servers/{server_id}/backups/{name}/download")
    def download_backup(server_id: int, name: str, w: Admin) -> FileResponse:
        """The backup zip itself (R-54), streamed from disk. Only names the backups folder
        lists for this server; nothing else can be asked for (D-64)."""
        get_server(server_id)
        try:
            path = backups.path(server_id, name)
        except (BackupError, FileNotFoundError):
            raise HTTPException(404, "No such backup") from None
        folder = settings.server_backup_dir(server_id).resolve()
        if path.resolve().parent != folder:  # a link swapped in by hand
            raise HTTPException(404, "No such backup")
        audit(w, "backup.download", server_id, name)
        return FileResponse(path, media_type="application/zip", filename=name,
                            headers={"Cache-Control": "no-store"})

    @app.delete("/api/servers/{server_id}/backups/{name}", status_code=204)
    def delete_backup(server_id: int, name: str, w: Admin) -> None:
        get_server(server_id)
        try:
            backups.delete(server_id, name)
        except (BackupError, FileNotFoundError) as e:
            raise backup_error(e) from None
        audit(w, "backup.delete", server_id, name)

    # -- config files (R-40..R-43, D-48..D-51) -------------------------------------------

    def config_file(server_id: int, n: int, w: auth.Who, action: str, fn: Callable[[Any], dict],
                    detail: str = "") -> dict:
        """Runs ``fn(server)`` for config file ``n``, mapping errors and auditing it. A
        refused path is audited too (R-43)."""
        server = get_server(server_id)
        if not 0 <= n < len(server.config_files):
            raise HTTPException(404, "No such config file")
        name = server.config_files[n].replace("\\", "/").rsplit("/", 1)[-1]
        try:
            result = fn(server)
        except FileNotFoundError:
            raise HTTPException(404, "No such copy") from None
        except configs.NotAllowed as e:
            audit(w, "config.refused", server_id, f"{name}: {e}")
            raise HTTPException(403, str(e)) from None
        except configs.Conflict as e:
            raise HTTPException(409, str(e)) from None
        except configs.ConfigError as e:
            if action == "config.save":  # bad JSON, a character the encoding can't hold
                audit(w, "config.invalid", server_id, f"{name}: {e}")
            raise HTTPException(400, str(e)) from None
        audit(w, action, server_id, f"{name}{detail}")
        result["running"] = sup.state(server_id) in (ServerState.STARTING, ServerState.RUNNING)
        return result

    @app.get("/api/servers/{server_id}/configs")
    def list_configs(server_id: int, w: Admin) -> dict:
        server = get_server(server_id)
        return {"files": config_files.list(server), "keep": settings.config_copies_keep}

    @app.get("/api/servers/{server_id}/configs/{n}")
    def read_config(server_id: int, n: int, w: Admin) -> dict:
        return config_file(server_id, n, w, "config.read",
                           lambda s: config_files.read(s, n))

    @app.put("/api/servers/{server_id}/configs/{n}")
    def save_config(server_id: int, n: int, body: ConfigSaveIn, w: Admin) -> dict:
        base = None if body.force else body.base_sha256
        return config_file(server_id, n, w, "config.save",
                           lambda s: config_files.save(s, n, body.text, base),
                           " (overwrote changes on disk)" if body.force else "")

    @app.get("/api/servers/{server_id}/configs/{n}/copies/{name}")
    def read_config_copy(server_id: int, n: int, name: str, w: Admin) -> dict:
        return config_file(server_id, n, w, "config.read",
                           lambda s: config_files.read_copy(s, n, name), f", copy {name}")

    @app.post("/api/servers/{server_id}/configs/{n}/copies/{name}/restore")
    def restore_config(server_id: int, n: int, name: str, w: Admin) -> dict:
        return config_file(server_id, n, w, "config.restore",
                           lambda s: config_files.restore(s, n, name), f" from {name}")

    # -- settings page (D-52) -----------------------------------------------------------

    def settings_info() -> dict:
        exe = settings.steamcmd_exe
        return {**{key: getattr(settings, key) for key in PAGE_SETTINGS},
                "steamcmd": str(exe), "steamcmd_found": exe.is_file(),
                "admin_logins": list(settings.admin_logins),
                "config_copies_keep": settings.config_copies_keep,
                "friend_control_gap_s": settings.friend_control_gap_s,
                "backups_dir": str(settings.backups_dir),
                "settings_file": str(settings.data_dir / "settings.toml"),
                "notify": notify_info(), "update": updater.view(),
                "live_status": live.view(), "file": settings_file.view()}

    @app.get("/api/settings")
    def read_settings(w: Admin) -> dict:
        return settings_info()

    @app.put("/api/settings")
    def write_settings(body: SettingsIn, w: Admin) -> dict:
        values = body.model_dump(exclude_none=True, exclude={"apply_backup_defaults"})
        if "join_host" in values:
            values["join_host"] = values["join_host"].strip()
        if "steamcmd_path" in values:
            values["steamcmd_path"] = values["steamcmd_path"].strip().strip('"')
        if "friend_control_logins" in values:
            logins = [x.strip() for x in values["friend_control_logins"] if x.strip()]
            values["friend_control_logins"] = tuple(dict.fromkeys(logins))
        changed = {k: v for k, v in values.items() if getattr(settings, k) != v}
        if changed:
            try:
                settings.save_page_settings(changed)
            except (OSError, ValueError) as e:
                raise HTTPException(500, f"Couldn't save settings.toml: {e}") from None
            audit(w, "settings", None, ", ".join(sorted(changed)))  # names only
        updated = []
        if body.apply_backup_defaults:
            for server in store.list_servers():
                data = server.model_dump(exclude={"id", "created_at", "updated_at"})
                data.update(backup_every_h=settings.default_backup_every_h,
                            backup_keep=settings.default_backup_keep)
                store.update_server(server.id, ServerConfig.model_validate(data))
                updated.append(server.name)
            audit(w, "settings.backups_to_all", None,
                  f"every {settings.default_backup_every_h} h, keep "
                  f"{settings.default_backup_keep}: {len(updated)} server(s)")
        if changed or updated:
            hub.poke()  # join addresses on every page
            live.poke()  # and in the live status message
        return {**settings_info(), "changed": sorted(changed), "servers_updated": updated}

    @app.post("/api/settings/reload")
    def reload_settings(w: Admin) -> dict:
        """Read settings.toml now (D-127). A file that doesn't load changes nothing."""
        result = settings_file.reload(w.label)
        return {**settings_info(), "reloaded": result}

    # -- Discord (R-60..R-62) ---------------------------------------------------------

    def check_webhook(url: str) -> str:
        url = url.strip()
        if url and not notify.WEBHOOK_URL.match(url):
            raise HTTPException(400, "That isn't a Discord webhook URL "
                                     "(https://discord.com/api/webhooks/...)")
        return url

    def notify_info() -> dict:
        main, per_server = notify.webhooks(settings)
        return {"webhook": notify.mask(main), "configured": bool(main),
                "servers": {str(k): notify.mask(v) for k, v in per_server.items()},
                "events": [e.value for e in NotifyEvent],
                "sent": notifier.sent, "failed": notifier.failed,
                "pending": notifier.pending()}

    @app.get("/api/notify")
    def notify_settings(w: Admin) -> dict:
        return notify_info()

    @app.put("/api/notify")
    def set_main_webhook(body: WebhookIn, w: Admin) -> dict:
        url = check_webhook(body.webhook_url)
        notify.set_webhook(settings, url)
        audit(w, "notify.webhook", None, "set" if url else "removed")  # never the URL
        live.poke()  # a new webhook gets its own live status message
        return notify_info()

    @app.put("/api/servers/{server_id}/webhook")
    def set_server_webhook(server_id: int, body: WebhookIn, w: Admin) -> dict:
        get_server(server_id)
        url = check_webhook(body.webhook_url)
        notify.set_webhook(settings, url, server_id)
        audit(w, "notify.webhook", server_id, "set" if url else "removed")
        return notify_info()

    @app.post("/api/notify/test")
    def test_webhook(w: Admin, server_id: int | None = None) -> dict:
        url = notifier.url_for(server_id)
        if not url:
            raise HTTPException(409, "No webhook set")
        ok, _, error = notify.send(url, ":wave: Test message from Basecamp")
        audit(w, "notify.test", server_id, "sent" if ok else error)
        if not ok:
            raise HTTPException(502, f"Discord didn't take it: {error}")
        return {"sent": True}

    # -- templates and import (R-13, R-15) --------------------------------------------

    def load_templates() -> templates.TemplateSet:
        return templates.load_templates(settings.templates_dir, settings.data_templates_dir)

    @app.get("/api/templates")
    def list_templates(w: Admin) -> dict:
        loaded = load_templates()
        return {
            "templates": [
                {**t.template.model_dump(mode="json"), "source": t.source,
                 "overrides": t.overrides, "file": t.path.name}
                for t in loaded.templates.values()
            ],
            "errors": loaded.errors,
            "data_folder": str(settings.data_templates_dir),
        }

    def template_files(template_id: str) -> tuple[Path, bool]:
        """The data-folder file for ``template_id``, and whether a built-in has that id."""
        return (settings.data_templates_dir / f"{template_id}.json",
                (settings.templates_dir / f"{template_id}.json").is_file())

    @app.post("/api/templates")
    def add_template(body: TemplateIn, w: Admin) -> dict:
        """Add a template from the page (D-129): validated like a file (D-31), saved as
        ``<data>\\templates\\<id>.json``. Overriding a built-in or replacing a data-folder
        template needs ``replace``; ``dry_run`` only checks it."""
        if len(body.text.encode("utf-8")) > templates.MAX_BYTES:
            raise HTTPException(413, f"Too big: a template is at most "
                                     f"{templates.MAX_BYTES // 1024} KB")
        try:
            template = templates.parse(body.text)
        except ValueError as e:
            if not body.dry_run:
                audit(w, "template.refused", None, str(e)[:300])
            raise HTTPException(400, str(e)) from None
        path, built_in = template_files(template.id)
        conflict = "replace" if path.exists() else "override" if built_in else ""
        result = {"id": template.id, "name": template.name, "conflict": conflict,
                  "file": str(path), "unverified": template.unverified}
        if body.dry_run:
            return result
        if conflict and not body.replace:
            raise HTTPException(409, f"A data-folder template {template.id!r} is already "
                                     "there; confirm to replace it" if conflict == "replace"
                                else f"{template.id!r} is a built-in template; confirm to "
                                     "override it")
        try:
            templates.save_data_template(settings.data_templates_dir, template.id, body.text)
        except OSError as e:
            raise HTTPException(500, f"Couldn't save it: {e}") from None
        action = {"replace": "template.replace", "override": "template.override"}.get(
            conflict, "template.add")
        audit(w, action, None, template.id)
        return {**result, "saved": True}

    @app.delete("/api/templates/{template_id}", status_code=204)
    def delete_template(template_id: str, w: Admin) -> None:
        """Delete a data-folder template (a broken one too). Built-ins can't be deleted;
        deleting an override brings the built-in back. Servers keep their settings."""
        if not templates.ID_PATTERN.fullmatch(template_id):
            raise HTTPException(404, "No such template")
        path, built_in = template_files(template_id)
        if not path.is_file() or path.is_symlink():
            if built_in:
                raise HTTPException(409, "Built-in templates can't be deleted")
            raise HTTPException(404, "No such template in the data folder")
        if path.resolve().parent != settings.data_templates_dir.resolve():
            raise HTTPException(404, "No such template in the data folder")
        path.unlink()
        audit(w, "template.delete", None, template_id + (" (built-in is back)" if built_in
                                                         else ""))

    @app.get("/api/steam/library")
    def steam_library(w: Admin) -> dict:
        """The folders in SteamCMD's steamapps\\common, to import from (D-125). Takes no
        path: only that one folder is ever listed."""
        taken = [(s.name, s.install_dir) for s in store.list_servers()]
        return templates.list_library(settings.steamcmd_exe, taken)

    def default_install(template: templates.Template | None, name: str,
                        messages: list[str]) -> str:
        """``<SteamCMD folder>\\steamapps\\common\\<folder>`` for a new Steam server, or ""
        with a message saying why not (D-125)."""
        exe = settings.steamcmd_exe
        folder = templates.folder_name(template, name)
        if not exe.is_file():
            messages.append(f"SteamCMD isn't at {exe}, so there's no default install folder. "
                            "Set its path on the Settings page, or type a folder.")
            return ""
        if not folder:
            return ""
        return str(templates.common_dir(exe) / folder)

    def build_draft(template_id: str, install_dir: str, name: str) -> dict:
        """A pre-filled server form from a template, or from an existing install (R-15):
        ``{"draft", "messages", "unverified"}``, and the template used."""
        loaded = load_templates()
        messages: list[str] = []
        install = install_dir.strip().strip('"')
        manifests: list[templates.AppManifest] = []
        if not install and template_id in loaded.templates:
            chosen_template = loaded.get(template_id).template
            if chosen_template.kind is ServerKind.STEAM:
                install = default_install(chosen_template, name, messages)
                if install:
                    messages.append("Install folder: SteamCMD's default for this game. Change "
                                    "it if you want it somewhere else.")
        if install:
            folder = Path(install)
            if folder.is_dir():
                manifests = templates.manifests_for(folder)
            elif not template_id:
                raise HTTPException(400, f"Folder not found: {install}")
        if not template_id:
            if not install:
                raise HTTPException(400, "Pick a template or an install folder")
            if not manifests:
                raise HTTPException(400, "No steamapps\\appmanifest_*.acf in that folder, so "
                                         "the game can't be detected. Pick a template.")
            for manifest in manifests:
                match = loaded.by_app_id(manifest.app_id)
                if match is not None:
                    template_id = match.template.id
                    messages.append(f"Found {manifest.name or 'app'} (app {manifest.app_id}, "
                                    f"build {manifest.build_id or '?'}): using the "
                                    f"{match.template.name} template.")
                    break
            else:
                first = manifests[0]
                messages.append(f"Found app {first.app_id} ({first.name or 'unknown'}) but no "
                                "template for it. Fill in the rest by hand.")
                return {"draft": {"kind": "steam", "steam_app_id": first.app_id,
                                  "install_dir": install, "name": name or first.name,
                                  "exe_path": "", "ports": [], "stop_recipe": [],
                                  "config_files": []},
                        "messages": messages, "unverified": []}
        try:
            chosen = loaded.get(template_id)
        except KeyError:
            raise HTTPException(404, f"No template {template_id!r}") from None
        draft = templates.draft(chosen.template, install, name)
        draft.update(backup_every_h=settings.default_backup_every_h,
                     backup_keep=settings.default_backup_keep)
        if install and not Path(draft["exe_path"]).is_file():
            messages.append(f"{draft['exe_path']} doesn't exist (yet). For a new install, save "
                            "the server and press Install on its page; otherwise fix the exe path.")
        if chosen.template.unverified:
            messages.append("Not yet checked against a real install: "
                            + ", ".join(chosen.template.unverified) + ".")
        return {"draft": draft, "messages": messages, "unverified": chosen.template.unverified,
                "template": chosen.template}

    @app.post("/api/drafts")
    def make_draft(body: DraftIn, w: Admin) -> dict:
        """A pre-filled server form from a template, or from an existing install (R-15)."""
        result = build_draft(body.template_id, body.install_dir, body.name)
        result.pop("template", None)
        return result

    # -- new game from a template, in one flow (D-130) -----------------------------------

    def setup_or_404(server_id: int):  # type: ignore[no-untyped-def]
        try:
            return setups.get(server_id)
        except KeyError:
            raise HTTPException(404, "No setup for that server") from None

    def setup_call(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except SetupError as e:
            raise HTTPException(409, str(e)) from None
        except KeyError:
            raise HTTPException(404, "No setup for that server") from None

    @app.get("/api/setups")
    def list_setups(w: Admin) -> list[dict]:
        return [setups.view(s) for s in store.list_setups()]

    @app.post("/api/setups", status_code=201)
    def begin_setup(body: SetupIn, w: Admin) -> dict:
        """Add a server from a template and start the new-game flow: install, first start,
        the values it needs, the firewall, ready."""
        result = build_draft(body.template_id, body.install_dir, body.name)
        template: templates.Template | None = result.get("template")
        if template is None:
            raise HTTPException(404, f"No template {body.template_id!r}")
        draft = result["draft"]
        install = draft["install_dir"]
        if template.kind is ServerKind.STEAM:
            if not install:
                raise HTTPException(400, " ".join(result["messages"])
                                    or "Type an install folder")
            folder = Path(install)
            if folder.is_file():
                raise HTTPException(409, f"{install} is a file")
            if (folder.is_dir() and any(folder.iterdir())
                    and not any(m.app_id == template.steam_app_id
                                for m in templates.manifests_for(folder))):
                raise HTTPException(409, f"{install} isn't empty and has no install of app "
                                         f"{template.steam_app_id}. Pick an empty or new "
                                         "folder, or import it with Add server.")
        try:
            config = ServerConfig.model_validate(draft)
        except ValidationError as e:
            raise HTTPException(400, str(e)) from None
        server = save(config)
        required = [{**v.model_dump(), "file": templates.resolve_path(install, v.file)}
                    for v in template.required_values]
        audit(w, "server.add", server.id, f"{server.name} (new game from {template.id})")
        setup = setups.begin(server, required, w.label)
        audit(w, "setup.start", server.id, template.id)
        hub.poke()
        return {**setups.view(setup), "messages": result["messages"]}

    @app.get("/api/setups/{server_id}")
    def read_setup(server_id: int, w: Admin) -> dict:
        return setups.view(setup_or_404(server_id))

    @app.post("/api/setups/{server_id}/retry")
    def retry_setup(server_id: int, w: Admin) -> dict:
        setup_or_404(server_id)
        not_updating(server_id)
        return setups.view(setup_call(lambda: setups.retry(server_id, w.label)))

    @app.post("/api/setups/{server_id}/values")
    def setup_values(server_id: int, body: SetupValuesIn, w: Admin) -> dict:
        setup_or_404(server_id)
        return setups.view(setup_call(
            lambda: setups.write_values(server_id, body.values, w.label)))

    @app.post("/api/setups/{server_id}/next")
    def setup_next(server_id: int, w: Admin) -> dict:
        setup_or_404(server_id)
        return setups.view(setup_call(lambda: setups.next(server_id, w.label)))

    @app.post("/api/setups/{server_id}/finish")
    def finish_setup(server_id: int, w: Admin, start: bool = False) -> dict:
        """Leave the flow (the server stays as it is); with ``start``, start it too."""
        setup = setup_or_404(server_id)
        server = get_server(server_id)
        if start:
            not_updating(server_id)
        setup_call(lambda: setups.finish(server_id))
        audit(w, "setup.finish", server_id, f"at {setup.step}")
        out: dict[str, Any] = {"finished": True}
        if start:
            audit(w, "start", server_id, server.name)
            try:
                out["pid"] = sup.start(server_id).pid
            except SupervisorError as e:
                raise HTTPException(409, f"Setup closed, but it didn't start: {e}") from None
        hub.poke()
        return out

    # -- firewall indicator (D-128) -------------------------------------------------------

    def firewall_view(force: bool) -> dict:
        policy, error, at, took = fw.get(force)
        out = {}
        for server in store.list_servers():
            status = sup.status(server.id)
            running = firewall.running_exe(status.pid) if status.pid else ""
            target = firewall.target_for(server.exe_path, server.real_exe_name,
                                         server.install_dir, running)
            def rule_exe(server=server, running=running) -> str:  # type: ignore[no-untyped-def]
                if running:
                    return running
                if server.real_exe_name.strip():
                    return firewall.find_exe(server.real_exe_name, server.install_dir)
                return server.exe_path

            ports = [(p.port, p.protocol.value, p.label) for p in server.ports]
            out[str(server.id)] = firewall.server_status(policy, error, ports, target, rule_exe)
        return {"servers": out, "checked_at": at, "took_s": round(took, 2), "error": error,
                "notes": firewall.profile_notes(policy), "cache_s": fw.cache_s}

    @app.get("/api/firewall")
    def firewall_status(w: Admin) -> dict:
        """Each server's ports against Windows Firewall's inbound rules, read-only and
        cached (D-128). Not part of the status WebSocket: the rules take ~0.5 s to read."""
        return firewall_view(False)

    @app.post("/api/firewall/check")
    def firewall_check(w: Admin) -> dict:
        return firewall_view(True)

    # -- Basecamp's own updates (R-90..R-94) -------------------------------------------

    @app.get("/api/update")
    def update_info(w: Admin) -> dict:
        return updater.view()

    @app.post("/api/update/check", status_code=202)
    def update_check(w: Admin) -> dict:
        started = updater.check_async()
        if started:
            audit(w, "update.check")
        return updater.view()

    @app.post("/api/update/install", status_code=202)
    def update_install(w: Admin) -> dict:
        try:
            version = updater.install(w.label)
        except UpdateError as e:
            audit(w, "update.refused", None, str(e))
            raise HTTPException(409, str(e)) from None
        audit(w, "update.install", None, f"{updater.current} -> {version}")
        return updater.view()

    @app.post("/api/restart", status_code=202)
    def restart_basecamp(w: Admin) -> dict:
        """Restart Basecamp itself through the launcher (D-126). Game servers keep running
        (R-04a); the page waits for a new ``started_at`` on /api/me."""
        if any(j.get("kind") != Kind.CHECK.value for j in steam.jobs()):  # the queue is lost
            raise HTTPException(409, "An install or update is running or queued; wait for it "
                                     "or cancel it first")
        try:
            updater.restart(w.label)
        except UpdateError as e:
            audit(w, "basecamp.restart.refused", None, str(e))
            raise HTTPException(409, str(e)) from None
        audit(w, "basecamp.restart")
        return {"restarting": True, "started_at": STARTED_AT}

    @app.post("/api/update/dismiss")
    def update_dismiss(w: Admin) -> dict:
        updater.dismiss()
        audit(w, "update.dismiss")
        return updater.view()

    # -- audit log (R-83) ------------------------------------------------------------

    @app.get("/api/audit")
    def audit_log(w: Admin, server_id: int | None = None, limit: int = 200) -> list[dict]:
        entries = store.list_audit(min(max(limit, 1), 1000), server_id)
        return [e.__dict__ for e in entries]

    # -- WebSockets -----------------------------------------------------------------

    def ws_who(ws: WebSocket) -> auth.Who | None:
        if not auth.host_allowed(ws.headers.get("host")) or not auth.check_origin(ws.headers):
            return None
        peer = ws.client.host if ws.client else None
        return auth.identify(peer, ws.headers, settings)

    @app.websocket("/ws/status")
    async def status_ws(ws: WebSocket) -> None:
        w = ws_who(ws)
        if w is None:
            await ws.close(code=4403)
            return
        await ws.accept()
        event = hub.register()
        try:
            while True:
                event.clear()
                now = ws_who(ws)  # admin_logins may have changed (D-127)
                if now is None or now.is_admin != w.is_admin:
                    await ws.close(code=4401)  # the page reconnects as what it is now
                    return
                servers = await run_in_threadpool(server_list, w)
                await ws.send_json({"type": "servers", "servers": servers})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), STATUS_INTERVAL_S)
                await asyncio.sleep(0.1)  # coalesce bursts of state changes
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.unregister(event)

    @app.websocket("/ws/servers/{server_id}/console")
    async def console_ws(ws: WebSocket, server_id: int, after: int = 0) -> None:
        w = ws_who(ws)
        if w is None or not w.is_admin:
            await ws.close(code=4403)
            return
        try:
            store.get_server(server_id)
        except KeyError:
            await ws.close(code=4404)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[ConsoleLine | None] = asyncio.Queue()
        con = sup.console(server_id)

        def push(line: ConsoleLine) -> None:
            def put() -> None:
                if queue.qsize() > _CONSOLE_QUEUE_MAX:
                    queue.put_nowait(None)  # too far behind: drop the client
                else:
                    queue.put_nowait(line)
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(put)

        unsubscribe = con.subscribe(push)  # before the backlog, so nothing falls between

        async def sender() -> None:
            backlog = con.lines(after)
            last = backlog[-1].seq if backlog else after
            await ws.send_json({"type": "lines", "lines": [_console_line(x) for x in backlog]})
            while True:
                line = await queue.get()
                if line is None:
                    await ws.close(code=4408)
                    return
                if line.seq <= last:
                    continue
                last = line.seq
                await ws.send_json({"type": "line", **_console_line(line)})

        async def receiver() -> None:
            while True:
                msg = await ws.receive_json()
                text = str(msg.get("text", "")) if isinstance(msg, dict) else ""
                channel = str(msg.get("channel", "")) if isinstance(msg, dict) else ""
                if not text.strip():
                    continue
                if channel not in ("", "stdin", "rcon", "rest"):
                    await ws.send_json({"type": "error", "message": "Unknown channel"})
                    continue
                try:
                    await run_in_threadpool(send, server_id, text[:1000], w, channel)
                except HTTPException as e:
                    await ws.send_json({"type": "error", "message": e.detail})

        async def still_admin() -> None:
            while True:  # admin_logins may lose this login (D-127)
                await asyncio.sleep(2)
                now = ws_who(ws)
                if now is None or not now.is_admin:
                    await ws.close(code=4403)
                    return

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver()),
                 asyncio.create_task(still_admin())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            unsubscribe()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect,
                                         RuntimeError, ValueError):
                    await task

    @app.exception_handler(ValidationError)
    async def validation_error(_: Request, e: ValidationError) -> JSONResponse:
        return JSONResponse({"detail": e.errors(include_url=False)}, status_code=422)

    app.state.hub = hub
    app.state.backups = backups
    app.state.notifier = notifier
    app.state.steam = steam
    app.state.configs = config_files
    app.state.live = live
    app.state.settings_file = settings_file
    app.state.setups = setups
    app.state.players = players
    app.state.schedules = schedules
    return app
