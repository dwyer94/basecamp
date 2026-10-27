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
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from basecamp import __version__, auth, notify, templates
from basecamp.api import views
from basecamp.backups import BackupError, Backups, save_folder
from basecamp.console import ConsoleLine
from basecamp.crashes import CrashPolicy
from basecamp.db import Store
from basecamp.models import ACTIVE_STATES, NotifyEvent, ServerConfig
from basecamp.notify import Notifier
from basecamp.settings import Settings
from basecamp.steam import Kind, SteamError, SteamRunner
from basecamp.supervisor import InvalidState, PortInUse, Supervisor, SupervisorError

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATUS_INTERVAL_S = 5.0  # uptime, CPU and RAM refresh on the status WebSocket
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_CONSOLE_QUEUE_MAX = 5000  # a console client this far behind is dropped


class CommandIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class WebhookIn(BaseModel):
    webhook_url: str = Field(default="", max_length=300)  # "" removes it


class DraftIn(BaseModel):
    install_dir: str = ""
    template_id: str = ""  # "" = detect from the install's appmanifest (R-15)
    name: str = ""


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
               steam: SteamRunner | None = None) -> FastAPI:
    """The services are made by main.run; tests may leave them out (no crash restarts)."""
    hub = _Hub()
    cpu = views.CpuSampler()
    notifier = notifier or Notifier(settings, store)
    backups = backups or Backups(store, sup, settings, notifier.server_event)
    steam = steam or SteamRunner(store, sup, settings, backups, notifier)
    sup.add_listener(hub.poke)
    steam.add_listener(hub.poke)

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
        peer = request.client.host if request.client else None
        request.state.who = auth.identify(peer, request.headers, settings)
        return await call_next(request)

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
                                steam.view(server))

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
                out.append(views.friend_view(settings, server, status))
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
                                "version": __version__}
        if w.is_admin:
            info["join_host"] = settings.join_host
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
        return views.friend_view(settings, server, status)

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

    @app.post("/api/servers/{server_id}/stop", status_code=202)
    def stop(server_id: int, w: Admin) -> dict:
        server = get_server(server_id)
        not_updating(server_id)
        audit(w, "stop", server_id, server.name)
        cancel_restart(server_id)
        in_background("stop", server_id, lambda: sup.stop(server_id))
        return {"accepted": True}

    @app.post("/api/servers/{server_id}/restart", status_code=202)
    def restart(server_id: int, w: Admin) -> dict:
        server = get_server(server_id)
        not_updating(server_id)
        audit(w, "restart", server_id, server.name)
        in_background("restart", server_id, lambda: sup.restart(server_id))
        return {"accepted": True}

    @app.post("/api/servers/{server_id}/kill", status_code=202)
    def kill(server_id: int, w: Admin) -> dict:
        server = get_server(server_id)
        audit(w, "kill", server_id, server.name)
        cancel_restart(server_id)
        in_background("kill", server_id, lambda: sup.kill(server_id))
        return {"accepted": True}

    def send(server_id: int, text: str, w: auth.Who) -> None:
        get_server(server_id)
        audit(w, "console", server_id, text)
        try:
            sup.send_command(server_id, text)
        except SupervisorError as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/api/servers/{server_id}/command", status_code=204)
    def command(server_id: int, body: CommandIn, w: Admin) -> None:
        send(server_id, body.text, w)

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

    @app.delete("/api/servers/{server_id}/backups/{name}", status_code=204)
    def delete_backup(server_id: int, name: str, w: Admin) -> None:
        get_server(server_id)
        try:
            backups.delete(server_id, name)
        except (BackupError, FileNotFoundError) as e:
            raise backup_error(e) from None
        audit(w, "backup.delete", server_id, name)

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

    @app.post("/api/drafts")
    def make_draft(body: DraftIn, w: Admin) -> dict:
        """A pre-filled server form from a template, or from an existing install (R-15)."""
        loaded = load_templates()
        messages: list[str] = []
        install = body.install_dir.strip().strip('"')
        template_id = body.template_id
        manifests: list[templates.AppManifest] = []
        if install:
            folder = Path(install)
            if folder.is_dir():
                manifests = templates.read_manifests(folder)
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
                                  "install_dir": install, "name": body.name or first.name,
                                  "exe_path": "", "ports": [], "stop_recipe": [],
                                  "config_files": []},
                        "messages": messages, "unverified": []}
        try:
            chosen = loaded.get(template_id)
        except KeyError:
            raise HTTPException(404, f"No template {template_id!r}") from None
        draft = templates.draft(chosen.template, install, body.name)
        if install and not Path(draft["exe_path"]).is_file():
            messages.append(f"{draft['exe_path']} doesn't exist (yet). For a new install, save "
                            "the server and press Install on its page; otherwise fix the exe path.")
        if chosen.template.unverified:
            messages.append("Not yet checked against a real install: "
                            + ", ".join(chosen.template.unverified) + ".")
        return {"draft": draft, "messages": messages, "unverified": chosen.template.unverified}

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
                if not text.strip():
                    continue
                try:
                    await run_in_threadpool(send, server_id, text[:1000], w)
                except HTTPException as e:
                    await ws.send_json({"type": "error", "message": e.detail})

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
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
    return app
