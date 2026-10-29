"""Crash auto-restart (R-05): a supervisor listener.

When a server goes to Crashed (an exit nobody asked for, also one found after a Basecamp
restart), it is started again after ``crash_restart_delay_s``, unless that was its
``crash_limit``-th crash within ``crash_window_s``: then it stays Crashed until someone
presses Start. Giving up clears its count, so after that Start it gets its retries back.
Each crash, and giving up, posts to Discord (when those events are on for the server).

A pending restart is cancelled by Stop, Kill or Delete (``cancel``), and is skipped if the
server is no longer Crashed when the timer fires (someone pressed Start).
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque

from basecamp import activity
from basecamp.db import Store
from basecamp.models import NotifyEvent, ServerState
from basecamp.notify import Notifier
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor, SupervisorError

log = logging.getLogger(__name__)


class CrashPolicy:
    def __init__(self, store: Store, sup: Supervisor, settings: Settings,
                 notifier: Notifier | None = None):
        self._store = store
        self._sup = sup
        self._settings = settings
        self._notifier = notifier
        self._mu = threading.Lock()
        self._crashes: dict[int, deque[float]] = {}
        self._pending: dict[int, tuple[threading.Timer, float]] = {}
        sup.add_listener(self.on_state)

    def restart_at(self, server_id: int) -> float | None:
        """When a pending restart will happen (epoch seconds), or None."""
        with self._mu:
            entry = self._pending.get(server_id)
        return entry[1] if entry else None

    def cancel(self, server_id: int) -> bool:
        with self._mu:
            entry = self._pending.pop(server_id, None)
        if entry is None:
            return False
        entry[0].cancel()
        self._sup.console(server_id).system("Automatic restart cancelled")
        log.info("Server %s: automatic restart cancelled", server_id)
        return True

    def on_state(self, server_id: int, old: ServerState, new: ServerState) -> None:
        if new is not ServerState.CRASHED or old is ServerState.CRASHED:
            return
        try:
            server = self._store.get_server(server_id)
        except KeyError:
            return
        s = self._settings
        now = time.monotonic()
        with self._mu:
            times = self._crashes.setdefault(server_id, deque())
            times.append(now)
            while times and now - times[0] > s.crash_window_s:
                times.popleft()
            count = len(times)
            give_up = count >= s.crash_limit
            if give_up:
                times.clear()
        code = self._sup.status(server_id).last_exit_code
        how = f"exit code {code}" if code is not None else "exit code unknown"
        minutes = round(s.crash_window_s / 60)
        console = self._sup.console(server_id)
        if give_up:
            console.system(f"Crashed {count} times in {minutes} minutes; not restarting. "
                           "Press Start when it's fixed")
            log.error("%s crashed %d times in %d minutes; giving up", server.name, count,
                      minutes)
            activity.record(self._store, "crash.gave_up", server_id,
                            f"Crashed {count} times in {minutes} min ({how}); not restarting")
            self._notify(server_id, NotifyEvent.GAVE_UP,
                         f":warning: **{server.name}** crashed {count} times in {minutes} "
                         f"minutes ({how}). Not restarting it until someone presses Start.")
            return
        delay = s.crash_restart_delay_s
        console.system(f"Crashed ({how}); restarting in {delay:g} s "
                       f"(crash {count} of {s.crash_limit} allowed in {minutes} minutes)")
        log.warning("%s crashed (%s); restarting in %g s", server.name, how, delay)
        activity.record(self._store, "crash", server_id,
                        f"Crashed ({how}); restarting in {delay:g} s")
        self._notify(server_id, NotifyEvent.CRASHED,
                     f":boom: **{server.name}** crashed ({how}). Restarting in {delay:g} s.")
        timer = threading.Timer(delay, self._restart, args=(server_id,))
        timer.daemon = True
        timer.name = f"crash restart {server_id}"
        with self._mu:
            old_entry = self._pending.pop(server_id, None)
            self._pending[server_id] = (timer, time.time() + delay)
        if old_entry is not None:
            old_entry[0].cancel()
        timer.start()

    def _restart(self, server_id: int) -> None:
        with self._mu:
            entry = self._pending.get(server_id)
            if entry is None or entry[0] is not threading.current_thread():
                return  # cancelled or replaced
            del self._pending[server_id]
        if self._sup.state(server_id) is not ServerState.CRASHED:
            return  # started by hand meanwhile
        try:
            server = self._store.get_server(server_id)
            self._sup.console(server_id).system("Restarting after the crash")
            self._sup.start(server_id)
        except KeyError:
            return
        except (SupervisorError, OSError) as e:
            # Not started: it stays Crashed. Nothing would make a retry work by itself.
            log.error("Server %s: restart after crash failed: %s", server_id, e)
            self._sup.console(server_id).system(f"Restart after the crash failed: {e}")
            with self._mu:
                self._crashes.pop(server_id, None)
            activity.record(self._store, "start.failed", server_id,
                            f"Restart after the crash failed: {e}")
            self._notify(server_id, NotifyEvent.GAVE_UP,
                         f":warning: **{server.name}** crashed and couldn't be restarted: {e}")

    def _notify(self, server_id: int, event: NotifyEvent, text: str) -> None:
        if self._notifier is None:
            return
        with contextlib.suppress(KeyError):
            self._notifier.server_event(self._store.get_server(server_id), event, text)
