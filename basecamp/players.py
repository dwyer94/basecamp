"""Player counts (batch 2, item 8; D-132).

For each Running server whose settings say how (``players``, from its template), Basecamp
asks the game how many players are on: Palworld through its REST API (``metrics``), games
with RCON through a command and a pattern, and others through Steam's server query
(A2S_INFO) on their query port. One server at a time, every ``players_poll_s`` (a minute),
and once when a server comes up. Only the number is kept, never names.

A count that can't be had (no password, no answer, an answer the pattern doesn't match) is
``None``: "unknown". Stopped servers have no count. Scheduled restarts and updates ask
again just before they go (``fresh``), and treat unknown as "nobody on" (D-133).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from basecamp import a2s
from basecamp.db import Store
from basecamp.models import PlayerSource, Server, ServerState
from basecamp.remote import RemoteError
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Count:
    count: int | None  # None = unknown
    most: int | None
    checked_at: float
    error: str = ""

    def friend(self) -> dict:
        return {"count": self.count, "max": self.most}

    def admin(self, source: str) -> dict:
        return {**self.friend(), "checked_at": self.checked_at, "error": self.error,
                "source": source}


def read_rcon(answer: str, pattern: str) -> tuple[int, int | None]:
    """The count from an RCON answer: the pattern's first group (and second, the most
    players), or with no group the number of lines it matches. ValueError if no match."""
    regex = re.compile(pattern, re.MULTILINE)
    if regex.groups:
        m = regex.search(answer)
        if m is None or m[1] is None:
            raise ValueError("the answer didn't match the pattern")
        most = int(m[2]) if regex.groups >= 2 and m[2] else None
        return int(m[1]), most
    return sum(1 for line in answer.splitlines() if regex.search(line)), None


def read_rest(answer: str) -> tuple[int, int | None]:
    """Palworld's ``metrics`` answer (``currentplayernum``, ``maxplayernum``)."""
    try:
        data = json.loads(answer)
        count = data["currentplayernum"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("the REST API's metrics had no currentplayernum") from None
    most = data.get("maxplayernum")
    return int(count), int(most) if isinstance(most, int) else None


class Players:
    def __init__(self, store: Store, sup: Supervisor, settings: Settings):
        self._store = store
        self._sup = sup
        self._settings = settings
        self._mu = threading.Lock()
        self._query_lock = threading.Lock()  # one query at a time
        self._counts: dict[int, Count] = {}
        self._listeners: list[Callable[[], None]] = []
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_poll = 0.0  # when a poll round last finished (epoch seconds)
        sup.add_listener(self._on_state)

    def add_listener(self, fn: Callable[[], None]) -> None:
        """Called when a count changes."""
        self._listeners.append(fn)

    # -- queries --------------------------------------------------------------------

    def get(self, server: Server) -> Count | None:
        """The last count for a Running server that reports players; None otherwise."""
        if server.players is None or self._sup.state(server.id) is not ServerState.RUNNING:
            return None
        with self._mu:
            found = self._counts.get(server.id)
        return found or Count(None, None, 0.0, "not asked yet")

    def friend_view(self, server: Server) -> dict | None:
        found = self.get(server)
        return found.friend() if found else None

    def admin_view(self, server: Server) -> dict | None:
        found = self.get(server)
        return found.admin(server.players.source.value) if found and server.players else None

    def query(self, server: Server) -> Count:
        """Ask the game now (blocking, a few seconds at most) and keep the answer."""
        assert server.players is not None
        q = server.players
        with self._query_lock:
            try:
                if q.source is PlayerSource.A2S:
                    assert q.port is not None
                    count, most = a2s.query_info("127.0.0.1", q.port,
                                                 self._settings.players_timeout_s)
                elif q.source is PlayerSource.REST:
                    count, most = read_rest(self._sup.remote.send(server, "rest", "metrics"))
                else:
                    count, most = read_rcon(self._sup.remote.send(server, "rcon", q.command),
                                            q.pattern)
                found = Count(count, most, time.time())
            except (a2s.A2SError, RemoteError, ValueError) as e:
                found = Count(None, None, time.time(), str(e))
        with self._mu:
            before = self._counts.get(server.id)
            self._counts[server.id] = found
        if before is None or (before.count, before.most, before.error) != (
                found.count, found.most, found.error):
            if found.error and (before is None or before.error != found.error):
                log.info("%s: player count unknown: %s", server.name, found.error)
            self._changed()
        return found

    def fresh(self, server: Server, max_age_s: float = 30.0) -> Count | None:
        """A count no older than ``max_age_s``, asking again if need be. None if the server
        doesn't report players or isn't Running."""
        found = self.get(server)
        if found is None or time.time() - found.checked_at <= max_age_s:
            return found
        return self.query(server)

    # -- polling --------------------------------------------------------------------

    def poll_once(self) -> None:
        running = set()
        for server in self._store.list_servers():
            if server.players is None or self._sup.state(server.id) is not ServerState.RUNNING:
                continue
            running.add(server.id)
            self.query(server)
            if self._closed.is_set():
                return
        with self._mu:
            gone = set(self._counts) - running
            for sid in gone:
                del self._counts[sid]
        self.last_poll = time.time()
        if gone:
            self._changed()

    def start(self) -> None:
        def loop() -> None:
            while not self._closed.is_set():
                try:
                    self.poll_once()
                except Exception:
                    log.exception("Player count poll failed")
                self._wake.wait(self._settings.players_poll_s)
                self._wake.clear()

        self._thread = threading.Thread(target=loop, name="player counts", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        self._wake.set()

    def health(self) -> dict:
        """For the debug view: is the poller running, when it last polled, the counts."""
        with self._mu:
            counts = {sid: {"count": c.count, "max": c.most, "checked_at": c.checked_at,
                            "error": c.error}
                      for sid, c in self._counts.items()}
        return {"running": bool(self._thread and self._thread.is_alive()),
                "every_s": self._settings.players_poll_s, "last_poll": self.last_poll or None,
                "counts": counts}

    def _on_state(self, server_id: int, old: ServerState, new: ServerState) -> None:
        if new is ServerState.RUNNING or old is ServerState.RUNNING:
            self._wake.set()  # ask a server that just came up; drop one that went down

    def _changed(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                log.exception("Player count listener failed")
