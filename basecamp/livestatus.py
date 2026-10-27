"""The live status message in Discord (R-63, D-63).

Optionally (``discord_live_status``, off by default, on the Settings page) Basecamp keeps
one message on the main webhook that lists every server, its state and join address. The
first one is posted with ``?wait=true`` so Discord answers with its id; after that it is
edited in place (``PATCH <webhook>/messages/<id>``). A 404 means someone deleted it: a new
one is posted. The id is kept in ``<data>\\live-status.json`` with a hash of the webhook,
so a new webhook gets a new message. Event messages (notify.py) still post as before.

One worker thread does the sending, at most once per ``live_status_every_s`` (changes in
between are folded into the next edit), so nothing waits on Discord (R-62). Failures are
logged and tried again with a back-off; the webhook URL never goes in a log.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from basecamp import __version__, notify
from basecamp.db import Store
from basecamp.models import ServerState
from basecamp.settings import Settings
from basecamp.supervisor import Supervisor

log = logging.getLogger(__name__)

_TIMEOUT_S = 10
_MAX_BACKOFF_S = 900.0
_ICON = {
    ServerState.RUNNING: ":green_circle:",
    ServerState.STARTING: ":yellow_circle:",
    ServerState.STOPPING: ":yellow_circle:",
    ServerState.UPDATING: ":arrows_counterclockwise:",
    ServerState.STOPPED: ":black_circle:",
    ServerState.CRASHED: ":red_circle:",
}
_COUNTDOWN = {"stop": "stopping", "restart": "restarting", "update": "updating"}


class LiveStatus:
    def __init__(self, settings: Settings, store: Store, sup: Supervisor):
        self._settings = settings
        self._store = store
        self._sup = sup
        self._path = settings.data_dir / "live-status.json"
        self._cond = threading.Condition()
        self._dirty = False
        self._closed = False
        self._last_body: str | None = None  # what the message shows now (sans timestamp)
        self._thread: threading.Thread | None = None
        self.edits = 0  # for tests and the page
        self.posts = 0
        self.error = ""  # the last failure, "" after a success
        sup.add_listener(self.poke)
        sup.add_change_listener(self.poke)  # a stop countdown

    # -- triggers ------------------------------------------------------------------------

    def poke(self, *_: object) -> None:
        """Something the message shows may have changed (supervisor listener, settings)."""
        if not self._settings.discord_live_status:
            return
        with self._cond:
            if self._closed:
                return
            self._dirty = True
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="live status",
                                                daemon=True)
                self._thread.start()
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify()

    def view(self) -> dict:
        return {"on": self._settings.discord_live_status, "edits": self.edits,
                "posts": self.posts, "error": self.error,
                "has_message": bool(self._load().get("message_id"))}

    # -- the message ---------------------------------------------------------------------

    def render(self) -> str:
        """The message body: one line per server (no timestamp, so it can be compared)."""
        lines = []
        for server in self._store.list_servers():
            status = self._sup.status(server.id)
            line = f"{_ICON.get(status.state, ':white_circle:')} **{server.name}** · " \
                   f"{status.state.value}"
            if status.warning:
                line += (f", {_COUNTDOWN.get(status.warning['kind'], 'stopping')} "
                         f"<t:{round(status.warning['stop_at'])}:R>")
            address = notify.join_address(self._settings, server)
            if address and status.state in (ServerState.RUNNING, ServerState.STARTING):
                line += f" → `{address}`"
            lines.append(line)
        return "\n".join(lines) or "No servers yet."

    # -- worker --------------------------------------------------------------------------

    def _work(self) -> None:
        next_at = 0.0
        failures = 0
        while True:
            with self._cond:
                while not self._closed and not self._dirty:
                    self._cond.wait()
                if self._closed:
                    return
                wait = next_at - time.monotonic()
                if wait > 0:
                    self._cond.wait(wait)
                    continue
                self._dirty = False
            try:
                ok = self._sync()
            except Exception:
                log.exception("Live status update failed")
                ok = False
            every = max(self._settings.live_status_every_s, 0.0)
            if ok:
                failures = 0
                next_at = time.monotonic() + every
            else:
                failures += 1
                base = max(every, self._settings.notify_retry_s[0]
                           if self._settings.notify_retry_s else 30.0)
                next_at = time.monotonic() + min(base * 2 ** (failures - 1), _MAX_BACKOFF_S)
                with self._cond:
                    self._dirty = True  # try again after the back-off

    def _sync(self) -> bool:
        """Bring the message up to date. False on a failure worth retrying."""
        if not self._settings.discord_live_status:
            return True
        url, _ = notify.webhooks(self._settings)
        if not url:
            return True
        body = self.render()
        stored = self._load()
        hook = hashlib.sha256(url.encode()).hexdigest()[:16]
        msg_id = stored.get("message_id") if stored.get("webhook") == hook else None
        if msg_id and body == self._last_body:
            return True
        content = f"**Game servers** · updated <t:{int(time.time())}:R>\n{body}"
        if msg_id:
            status, _, error = _request("PATCH", f"{url}/messages/{msg_id}", content)
            if status == 200:
                self._last_body, self.error = body, ""
                self.edits += 1
                return True
            if status != 404:
                return self._failed(f"edit: {error}")
            log.info("The live status message was deleted; posting a new one")
        status, answer, error = _request("POST", f"{url}?wait=true", content)
        new_id = str(answer.get("id", "")) if isinstance(answer, dict) else ""
        if status != 200 or not new_id.isdigit():
            return self._failed(f"post: {error or 'no message id in the answer'}")
        self._save({"message_id": new_id, "webhook": hook})
        self._last_body, self.error = body, ""
        self.posts += 1
        return True

    def _failed(self, error: str) -> bool:
        if error != self.error:
            log.warning("Live status message not updated (%s)", error)
        self.error = error
        return False

    def _load(self) -> dict:
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        return {}

    def _save(self, data: dict) -> None:
        try:
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("Can't write %s: %s", self._path, e)


def _request(method: str, url: str, content: str) -> tuple[int, dict | None, str]:
    """(HTTP status or 0, the JSON answer, an error text without the URL)."""
    body = json.dumps({"content": content[:2000], "username": "Basecamp",
                       "allowed_mentions": {"parse": []}}).encode()
    request = urllib.request.Request(url, data=body, method=method, headers={
        "Content-Type": "application/json", "User-Agent": f"Basecamp/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as res:
            raw = res.read()
            status = res.status
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return 0, None, str(getattr(e, "reason", e))
    try:
        answer = json.loads(raw) if raw else None
    except ValueError:
        answer = None
    return status, answer if isinstance(answer, dict) else None, ""
