"""Scheduled restarts and updates (batch 2, item 9; D-133).

Per server, a daily restart at ``restart_at`` and a daily update at ``update_at`` (HH:MM,
this PC's time; "" = off). Every ``schedule_check_s`` the scheduler looks for ones that are
due, and runs each the way the buttons do:

- a restart: only while the server is Running, with its stop warning (R-25), its stop
  recipe, then a start; Discord gets "stopped" and "up" as for a restart by hand,
- an update: only when the update-available badge says Steam has a newer build (R-34),
  queued in the SteamCMD queue like Update (backup, warning, stop, SteamCMD, start again,
  and its Discord messages).

While players are on (the player count, D-132), ``schedule_players`` says what happens:
``ignore`` goes ahead; ``skip`` skips that day; ``delay`` asks again every
``schedule_retry_min`` until nobody is on, and goes ahead anyway after
``schedule_delay_max_min``. A count that is unknown counts as nobody on, and the console
and audit log say so.

One that Basecamp missed (it was down at the time) still runs if it is less than
``schedule_window_min`` late; otherwise it waits for the next day. What was done when is
kept in ``<data>\\schedule.json``, so a Basecamp restart never runs one twice in a day.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from basecamp.db import Store
from basecamp.models import SchedulePlayers, Server, ServerKind, ServerState
from basecamp.players import Players
from basecamp.settings import Settings
from basecamp.steam import Kind, SteamError, SteamRunner
from basecamp.supervisor import Supervisor, SupervisorError

log = logging.getLogger(__name__)

WHO = "schedule"
KINDS = ("restart", "update")


class Schedules:
    def __init__(self, store: Store, sup: Supervisor, steam: SteamRunner, players: Players,
                 settings: Settings, *,
                 audit: Callable[[str, str, int | None, str], None] | None = None,
                 now: Callable[[], datetime] = datetime.now):
        self._store = store
        self._sup = sup
        self._steam = steam
        self._players = players
        self._settings = settings
        self._audit = audit or (lambda *_: None)
        self._now = now
        self._mu = threading.Lock()
        self._path = settings.data_dir / "schedule.json"
        # "<server id>:<kind>" -> {"done": "YYYY-MM-DD", "waiting_since": iso, "next": iso}
        self._state: dict[str, dict] = self._load()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the page -----------------------------------------------------------------------

    def view(self, server: Server) -> dict | None:
        """What the server page shows: the times, and a restart or update waiting for
        players to leave."""
        if not server.restart_at and not server.update_at:
            return None
        waiting = {}
        with self._mu:
            for kind in KINDS:
                entry = self._state.get(f"{server.id}:{kind}", {})
                if entry.get("waiting_since"):
                    waiting[kind] = entry["waiting_since"]
        return {"restart_at": server.restart_at, "update_at": server.update_at,
                "players": server.schedule_players.value, "waiting": waiting}

    # -- the loop -----------------------------------------------------------------------

    def start(self) -> None:
        def loop() -> None:
            while not self._closed.wait(self._settings.schedule_check_s):
                try:
                    self.tick()
                except Exception:
                    log.exception("Scheduler failed")

        self._thread = threading.Thread(target=loop, name="schedules", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()

    def tick(self) -> list[tuple[int, str, str]]:
        """One look at every server. Returns ``(server id, kind, what happened)`` for each
        schedule that was due."""
        now = self._now()
        out = []
        for server in self._store.list_servers():
            for kind in KINDS:
                at = server.restart_at if kind == "restart" else server.update_at
                if not at:
                    continue
                what = self._check(server, kind, at, now)
                if what:
                    out.append((server.id, kind, what))
        return out

    def _check(self, server: Server, kind: str, at: str, now: datetime) -> str:
        key = f"{server.id}:{kind}"
        today = now.date().isoformat()
        hour, minute = (int(x) for x in at.split(":"))
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        with self._mu:
            entry = dict(self._state.get(key, {}))
        waiting = entry.get("waiting_since")  # may be from before midnight
        if entry.get("done") == today or (not waiting and now < due):
            return ""
        if waiting and entry.get("next") and now < datetime.fromisoformat(entry["next"]):
            return ""
        if not waiting and now > due + timedelta(minutes=self._settings.schedule_window_min):
            self._done(key, today)  # Basecamp was down then; tomorrow
            log.info("%s: scheduled %s at %s missed (more than %g min late)", server.name,
                     kind, at, self._settings.schedule_window_min)
            return "missed"

        # Is there anything to do?
        state = self._sup.state(server.id)
        if kind == "restart" and state is not ServerState.RUNNING:
            self._done(key, today)
            return "not running"
        if kind == "update":
            view = self._steam.view(server) if server.kind is ServerKind.STEAM else None
            if not view or not view["update_available"]:
                self._done(key, today)
                return "no update"
        if self._steam.job(server.id) is not None or self._sup.counting_down(server.id):
            self._done(key, today)
            self._note(server, f"Scheduled {kind} skipped: an install, update or stop is "
                               "already on its way")
            return "busy"

        # Players on?
        note = ""
        if server.schedule_players is not SchedulePlayers.IGNORE and state is ServerState.RUNNING:
            count = self._players.fresh(server, max_age_s=0)  # ask now
            if count is not None and count.count:
                if server.schedule_players is SchedulePlayers.SKIP:
                    self._done(key, today)
                    self._note(server, f"Scheduled {kind} skipped today: {count.count} "
                                       f"player(s) on", audit=f"schedule.{kind}.skipped")
                    return "skipped"
                since = datetime.fromisoformat(waiting) if waiting else now
                if now < since + timedelta(minutes=self._settings.schedule_delay_max_min):
                    nxt = now + timedelta(minutes=self._settings.schedule_retry_min)
                    self._set(key, {"waiting_since": since.isoformat(timespec="seconds"),
                                    "next": nxt.isoformat(timespec="seconds")})
                    if not waiting:
                        self._note(server, f"Scheduled {kind} waits: {count.count} player(s) "
                                           f"on; asking again every "
                                           f"{self._settings.schedule_retry_min:g} min")
                    return "waiting"
                note = (f"; {count.count} player(s) still on after "
                        f"{self._settings.schedule_delay_max_min:g} min, so it goes ahead")
            elif count is None or count.count is None:
                note = "; the player count is unknown, so it goes ahead as if nobody's on"

        self._done(key, today)
        if kind == "restart":
            self._note(server, f"Scheduled restart ({at}){note}", audit="schedule.restart")
            threading.Thread(target=self._restart, args=(server,), daemon=True,
                             name=f"scheduled restart {server.id}").start()
            return "restart"
        try:
            self._steam.enqueue(server.id, Kind.UPDATE, WHO)
        except (SteamError, KeyError) as e:
            self._note(server, f"Scheduled update not queued: {e}",
                       audit="schedule.update.failed")
            return "failed"
        self._note(server, f"Scheduled update ({at}){note}", audit="schedule.update")
        return "update"

    def _restart(self, server: Server) -> None:
        try:
            self._sup.restart(server.id, warn=True)
        except SupervisorError as e:
            self._sup.console(server.id).system(f"Scheduled restart failed: {e}")
            self._audit(WHO, "schedule.restart.failed", server.id, str(e))
        except Exception:
            log.exception("Scheduled restart of %s failed", server.name)

    # -- state ----------------------------------------------------------------------------

    def _note(self, server: Server, text: str, audit: str = "") -> None:
        log.info("%s: %s", server.name, text)
        self._sup.console(server.id).system(text)
        if audit:
            self._audit(WHO, audit, server.id, text)

    def _done(self, key: str, today: str) -> None:
        self._set(key, {"done": today})

    def _set(self, key: str, entry: dict) -> None:
        with self._mu:
            self._state[key] = entry
            data = dict(self._state)
        try:
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("Can't write %s: %s", self._path, e)

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("Can't read %s: %s", self._path, e)
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(
            data, dict) else {}
