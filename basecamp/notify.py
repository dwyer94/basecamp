"""Discord webhook messages (R-60..R-62).

One webhook URL for everything, and optionally one per server that replaces it (Q-07 is
still open, so both work). The URLs are secrets: they live in ``<data>\\secrets.toml``
(D-37), are never sent back to the page in full, and never go in logs.

    [discord]
    webhook_url = "https://discord.com/api/webhooks/..."

    [discord.servers]
    "3" = "https://discord.com/api/webhooks/..."   # server id 3 posts here instead

Sending never blocks anything (R-62): messages go on a queue, one worker thread posts
them, and a failure is logged and tried again after each of ``settings.notify_retry_s``.
Discord's 429 answers are honoured; other 4xx answers (a deleted webhook) aren't retried.
"""

from __future__ import annotations

import contextlib
import heapq
import itertools
import json
import logging
import os
import re
import threading
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from basecamp import __version__
from basecamp.db import Store
from basecamp.models import NotifyEvent, Server, ServerState
from basecamp.settings import Settings

log = logging.getLogger(__name__)

# What the page accepts. The notifier itself posts to any URL (tests use a local one).
WEBHOOK_URL = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
QUEUE_MAX = 200
_TIMEOUT_S = 10


def mask(url: str) -> str:
    """Enough to recognise a webhook, not enough to use it."""
    if not url:
        return ""
    head, _, token = url.rpartition("/")
    return f"{head}/{token[:4]}…" if token else url[:24] + "…"


# -- secrets.toml ----------------------------------------------------------------------


def load_secrets(settings: Settings) -> dict:
    try:
        with settings.secrets_path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.error("Can't read %s: %s", settings.secrets_path, e)
        return {}


def save_secrets(settings: Settings, data: dict) -> None:
    """Rewrite secrets.toml (comments in it are not kept)."""
    text = "# Basecamp secrets. Not in the repo; rewritten by Basecamp.\n" + _dump_toml(data)
    path = settings.secrets_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def webhooks(settings: Settings) -> tuple[str, dict[int, str]]:
    """(the main webhook URL, {server id: its own URL})."""
    discord = load_secrets(settings).get("discord", {})
    if not isinstance(discord, dict):
        return "", {}
    main = str(discord.get("webhook_url", "") or "")
    per_server = {}
    servers = discord.get("servers", {})
    if isinstance(servers, dict):
        for key, url in servers.items():
            with contextlib.suppress(ValueError):
                if url:
                    per_server[int(key)] = str(url)
    return main, per_server


def set_webhook(settings: Settings, url: str, server_id: int | None = None) -> None:
    """Set (or with "" remove) the main webhook, or one server's own."""
    data = load_secrets(settings)
    discord = data.setdefault("discord", {})
    if server_id is None:
        if url:
            discord["webhook_url"] = url
        else:
            discord.pop("webhook_url", None)
    else:
        servers = discord.setdefault("servers", {})
        if url:
            servers[str(server_id)] = url
        else:
            servers.pop(str(server_id), None)
        if not servers:
            discord.pop("servers")
    if not discord:
        data.pop("discord")
    save_secrets(settings, data)


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _dump_toml(data: dict, prefix: str = "") -> str:
    scalars, tables = [], []
    for key, value in data.items():
        if isinstance(value, dict):
            tables.append((key, value))
        else:
            scalars.append(f"{_toml_key(key)} = {_toml_value(value)}")
    out = ""
    if scalars:
        if prefix:
            out += f"\n[{prefix}]\n"
        out += "\n".join(scalars) + "\n"
    for key, value in tables:
        name = f"{prefix}.{_toml_key(key)}" if prefix else _toml_key(key)
        body = _dump_toml(value, name)
        out += body if body else f"\n[{name}]\n"
    return out


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # JSON string escapes are valid TOML basic strings
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"can't write {type(value).__name__} to secrets.toml")


# -- sending ---------------------------------------------------------------------------


@dataclass(order=True)
class _Message:
    due: float
    seq: int
    url: str = field(compare=False)
    text: str = field(compare=False)
    tries: int = field(default=0, compare=False)


class Notifier:
    def __init__(self, settings: Settings, store: Store | None = None):
        self._settings = settings
        self._store = store
        self._heap: list[_Message] = []
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._closed = False
        self.sent = 0  # messages delivered (for tests and the page)
        self.failed = 0  # messages given up on

    # -- what to post ------------------------------------------------------------------

    def url_for(self, server_id: int | None) -> str:
        main, per_server = webhooks(self._settings)
        if server_id is not None and server_id in per_server:
            return per_server[server_id]
        return main

    def server_event(self, server: Server, event: NotifyEvent, text: str) -> bool:
        """Post about one server, unless that event is off for it (R-61) or no webhook is
        set. True if queued."""
        if event in server.notify_off:
            return False
        return self.post(text, server.id)

    def post(self, text: str, server_id: int | None = None) -> bool:
        url = self.url_for(server_id)
        if not url:
            log.debug("No Discord webhook set; not posting: %s", text)
            return False
        self._enqueue(url, text)
        return True

    def on_state(self, server_id: int, old: ServerState, new: ServerState) -> None:
        """Supervisor listener: "up" and "stopped". Crashes come from crashes.py, which
        knows whether it will restart. A reattach after a Basecamp restart (Stopped ->
        Running) posts nothing."""
        if self._store is None:
            return
        if old is ServerState.STARTING and new is ServerState.RUNNING:
            event = NotifyEvent.UP
        elif old is ServerState.STOPPING and new is ServerState.STOPPED:
            event = NotifyEvent.STOPPED
        else:
            return
        try:
            server = self._store.get_server(server_id)
        except KeyError:
            return
        if event is NotifyEvent.UP:
            address = join_address(self._settings, server)
            text = (f":green_circle: **{server.name}** is up → `{address}`" if address
                    else f":green_circle: **{server.name}** is up")
        else:
            text = f":red_circle: **{server.name}** stopped"
        self.server_event(server, event, text)

    # -- queue and worker --------------------------------------------------------------

    def _enqueue(self, url: str, text: str, due: float | None = None, tries: int = 0) -> None:
        with self._cond:
            if self._closed:
                return
            if len(self._heap) >= QUEUE_MAX:
                dropped = heapq.heappop(self._heap)
                log.warning("Discord queue full; dropped: %s", dropped.text)
            heapq.heappush(self._heap, _Message(due or time.monotonic(), next(self._seq), url,
                                                text, tries))
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="discord", daemon=True)
                self._thread.start()
            self._cond.notify()

    def pending(self) -> int:
        with self._cond:
            return len(self._heap)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify()

    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._closed and (not self._heap
                                            or self._heap[0].due > time.monotonic()):
                    wait = self._heap[0].due - time.monotonic() if self._heap else None
                    self._cond.wait(wait)
                if self._closed:
                    return
                msg = heapq.heappop(self._heap)
            self._deliver(msg)

    def _deliver(self, msg: _Message) -> None:
        ok, retry_after, error = send(msg.url, msg.text)
        if ok:
            self.sent += 1
            return
        delays = self._settings.notify_retry_s
        if retry_after is None or msg.tries >= len(delays):
            self.failed += 1
            log.error("Discord message not sent (%s): %s", error, msg.text)
            return
        wait = max(retry_after, delays[msg.tries])
        log.warning("Discord message failed (%s); trying again in %.0f s", error, wait)
        self._enqueue(msg.url, msg.text, time.monotonic() + wait, msg.tries + 1)


def send(url: str, text: str) -> tuple[bool, float | None, str]:
    """POST one message. Returns (sent, retry after seconds or None for "don't retry",
    error text)."""
    body = json.dumps({"content": text[:2000], "username": "Basecamp",
                       "allowed_mentions": {"parse": []}}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": f"Basecamp/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as res:
            res.read()
        return True, None, ""
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry = 5.0
            with contextlib.suppress(ValueError, TypeError, OSError):
                retry = float(json.loads(e.read() or b"{}").get("retry_after", retry))
            return False, retry, "rate limited"
        if 400 <= e.code < 500:
            return False, None, f"HTTP {e.code} (check the webhook URL)"
        return False, 0.0, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return False, 0.0, str(reason)


def join_address(settings: Settings, server: Server) -> str | None:
    """R-24: ``<join host>:<join port>``, or None until both are set."""
    if not settings.join_host or server.join_port is None:
        return None
    return f"{settings.join_host}:{server.join_port}"
