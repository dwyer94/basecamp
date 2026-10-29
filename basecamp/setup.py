"""New game from a template, in one flow (batch 2, item 6; D-130).

The admin picks a template on the page; Basecamp adds the server from it and then walks it
through the steps a new game needs, one after another:

1. ``install``: SteamCMD's first install, through the SteamCMD queue (skipped when it is
   already installed, or for a custom server),
2. ``first_start``: start it once so the game writes its config files, wait until it is up
   and the files are there, then stop it with its stop recipe (skipped when the files are
   already there, or the template lists none),
3. ``values``: the values the template marks as required (``required_values``, e.g.
   Dragonwilds' ``OwnerId``), typed in by the admin and written into the config file the
   way the config editor saves it, with its timestamped copy (skipped when there are none),
4. ``firewall``: the server's firewall indicator and the ``add-game-firewall-rule.ps1``
   command (D-128); the admin goes on when it's done,
5. ``ready``: ready to start.

Steps 1 and 2 run on their own thread; the others wait for the admin. Where it is is kept
in the ``setups`` table, so the page can be closed and the flow picked up again. A step that
fails stops there with the reason (``failed``) and is run again on **Try again**; a step
that was running when Basecamp stopped is marked failed on the next start.
"""

from __future__ import annotations

import json
import logging
import ntpath
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from basecamp import configs, templates
from basecamp.configs import ConfigFiles
from basecamp.db import Setup, Store
from basecamp.models import ACTIVE_STATES, Server, ServerKind, ServerState
from basecamp.settings import Settings
from basecamp.steam import Kind, SteamError, SteamRunner, installed_build
from basecamp.supervisor import Supervisor, SupervisorError

log = logging.getLogger(__name__)

STEPS = ("install", "first_start", "values", "firewall", "ready")
STEP_LABELS = {"install": "Install with SteamCMD", "first_start": "First start",
               "values": "Values the game needs", "firewall": "Firewall",
               "ready": "Ready to start"}
_POLL_S = 0.5
MAX_VALUE = 500


class SetupError(Exception):
    """Something the admin can fix; the page shows the text."""


# -- reading and writing one value (INI and JSON) ----------------------------------------

_SECTION = re.compile(r"^\s*\[(.*)\]\s*$")


def _key_line(line: str, key: str) -> re.Match[str] | None:
    m = re.match(r"^(\s*)([^=;#]+?)(\s*=\s*)(.*)$", line)
    if m and m[2].strip().lower() == key.lower():
        return m
    return None


def ini_get(text: str, section: str, key: str) -> str | None:
    current = ""
    for line in text.split("\n"):
        if (m := _SECTION.match(line)) is not None:
            current = m[1].strip()
            continue
        if current.lower() == section.strip().lower() and (k := _key_line(line, key)):
            return k[4].rstrip("\r")
    return None


def ini_set(text: str, section: str, key: str, value: str) -> str:
    """``text`` with ``key=value`` under ``[section]`` ("" = before the first section):
    an existing line is changed in place, else the key is added at the end of the section,
    else the section is added at the end of the file."""
    lines = text.split("\n")
    want = section.strip().lower()
    current = ""
    start: int | None = 0 if not want else None  # the section's header line (or 0)
    end = len(lines)
    for i, line in enumerate(lines):
        if (m := _SECTION.match(line)) is not None:
            if start is not None and current.lower() == want:
                end = i
                break
            current = m[1].strip()
            if current.lower() == want:
                start = i
            continue
        if current.lower() == want and (k := _key_line(line, key)) is not None:
            lines[i] = f"{k[1]}{k[2]}{k[3]}{value}"
            return "\n".join(lines)
    if start is None:  # no such section: add it at the end
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines += [f"[{section.strip()}]", f"{key}={value}", ""]
        return "\n".join(lines)
    if not want:  # before the first section
        end = next((i for i, line in enumerate(lines) if _SECTION.match(line)), len(lines))
        start = -1
    last = start  # after the section's last non-blank line
    for i in range(start + 1, end):
        if lines[i].strip():
            last = i
    lines.insert(last + 1, f"{key}={value}")
    return "\n".join(lines)


def _json_walk(data: object, key: str) -> tuple[dict, str]:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node, dict):
            raise SetupError(f"{key}: {part} isn't an object in the file")
        node = node.setdefault(part, {})
    if not isinstance(node, dict):
        raise SetupError(f"{key} isn't inside an object in the file")
    return node, parts[-1]


def json_get(text: str, key: str) -> str | None:
    try:
        data = json.loads(text)
        node, last = _json_walk(data, key)
    except (ValueError, SetupError):
        return None
    value = node.get(last)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value)
    return None


def json_set(text: str, key: str, value: str) -> str:
    """``text`` with ``key`` (a.b.c) set. A number or true/false already there stays that
    type. The file is written again with the indent it had."""
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as e:
        raise SetupError(f"The file isn't valid JSON: {e}") from None
    node, last = _json_walk(data, key)
    old = node.get(last)
    new: object = value
    if isinstance(old, bool):
        if value.lower() not in ("true", "false"):
            raise SetupError(f"{key} should be true or false")
        new = value.lower() == "true"
    elif isinstance(old, int | float):
        try:
            new = int(value) if isinstance(old, int) and re.fullmatch(r"-?\d+", value) \
                else float(value)
        except ValueError:
            raise SetupError(f"{key} should be a number") from None
    node[last] = new
    m = re.search(r"\n([ \t]+)\S", text)
    indent = m[1] if m else "  "
    return json.dumps(data, indent=indent, ensure_ascii=False) + "\n"


def check_value(label: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise SetupError(f"{label}: needed")
    if len(value) > MAX_VALUE:
        raise SetupError(f"{label}: at most {MAX_VALUE} characters")
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise SetupError(f"{label}: one line, no control characters")
    return value


# -- the flow ----------------------------------------------------------------------------


def _norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


class Setups:
    def __init__(self, store: Store, sup: Supervisor, steam: SteamRunner,
                 config_files: ConfigFiles, settings: Settings, *,
                 audit: Callable[[str, str, int | None, str], None] | None = None,
                 cancel_restart: Callable[[int], None] | None = None):
        self._store = store
        self._sup = sup
        self._steam = steam
        self._configs = config_files
        self._settings = settings
        self._audit = audit or (lambda *_: None)
        self._cancel_restart = cancel_restart or (lambda _id: None)
        self._mu = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}
        self._closed = threading.Event()
        for setup in store.list_setups():  # nothing can carry on a step from before
            if setup.status == "running":
                store.update_setup(setup.server_id, status="failed",
                                   message="Basecamp restarted during this step. "
                                           "Press Try again.")

    # -- queries --------------------------------------------------------------------

    def running(self, server_id: int) -> bool:
        with self._mu:
            thread = self._threads.get(server_id)
            return thread is not None and thread.is_alive()

    def get(self, server_id: int) -> Setup:
        return self._store.get_setup(server_id)

    def view(self, setup: Setup) -> dict:
        server = self._store.get_server(setup.server_id)
        at = STEPS.index(setup.step) if setup.step in STEPS else len(STEPS)
        steps = [{"id": s, "label": STEP_LABELS[s],
                  "state": "done" if i < at else "current" if i == at else "todo"}
                 for i, s in enumerate(STEPS)]
        if server.kind is not ServerKind.STEAM:
            steps[0]["state"] = "skipped"
        return {"server_id": server.id, "name": server.name, "template_id": setup.template_id,
                "step": setup.step, "status": setup.status, "message": setup.message,
                "running": self.running(server.id), "steps": steps,
                "values": self._values_view(server, setup) if at >= 2 else [],
                "state": self._sup.state(server.id).value, "who": setup.who,
                "started_at": setup.started_at, "updated_at": setup.updated_at}

    def _values_view(self, server: Server, setup: Setup) -> list[dict]:
        out = []
        for i, value in enumerate(setup.required):
            current, error = None, ""
            try:
                n = self._config_index(server, value["file"])
                text = self._configs.read(server, n)["text"]
                current = (json_get(text, value["key"]) if templates.value_format(value["file"])
                           == "json" else ini_get(text, value.get("section", ""), value["key"]))
            except (SetupError, configs.ConfigError) as e:
                error = str(e)
            out.append({"n": i, "label": value["label"], "hint": value.get("hint", ""),
                        "file": ntpath.basename(value["file"]), "key": value["key"],
                        "section": value.get("section", ""), "current": current or "",
                        "error": error})
        return out

    # -- actions --------------------------------------------------------------------

    def begin(self, server: Server, required: list[dict], who: str) -> Setup:
        first = "install" if server.kind is ServerKind.STEAM else "first_start"
        setup = self._store.add_setup(server.id, server.template_id, first, "running",
                                      required, who)
        self._start(server.id)
        return setup

    def retry(self, server_id: int, who: str) -> Setup:
        setup = self._store.get_setup(server_id)
        if self.running(server_id):
            raise SetupError("This step is still running")
        if setup.status != "failed":
            raise SetupError("Nothing to try again")
        self._store.update_setup(server_id, status="running", message="")
        self._audit(who, "setup.retry", server_id, setup.step)
        self._start(server_id)
        return self._store.get_setup(server_id)

    def write_values(self, server_id: int, values: dict[str, str], who: str) -> Setup:
        """Write the required values into their config files (the config editor's save,
        with its copy of the old file), then go on to the firewall step."""
        setup = self._store.get_setup(server_id)
        server = self._store.get_server(server_id)
        if setup.step not in ("values", "firewall", "ready") or self.running(server_id):
            raise SetupError("Not at that step yet")
        if self._sup.state(server_id) in ACTIVE_STATES:
            raise SetupError("Stop the server first: games often write their config files "
                             "again when they stop")
        by_file: dict[int, list[tuple[dict, str]]] = {}
        for i, value in enumerate(setup.required):
            text = check_value(value["label"], str(values.get(str(i), "")))
            n = self._config_index(server, value["file"])
            by_file.setdefault(n, []).append((value, text))
        for n, items in by_file.items():
            try:
                current = self._configs.read(server, n)
                text = current["text"]
                for value, new in items:
                    if templates.value_format(value["file"]) == "json":
                        text = json_set(text, value["key"], new)
                    else:
                        text = ini_set(text, value.get("section", ""), value["key"], new)
                self._configs.save(server, n, text, current["sha256"])
            except configs.ConfigError as e:
                raise SetupError(f"{ntpath.basename(server.config_files[n])}: {e}") from None
        keys = ", ".join(v["key"] for v in setup.required)  # names only, as for settings
        self._audit(who, "setup.values", server_id, keys)
        if setup.step == "values":
            self._store.update_setup(server_id, step="firewall", status="waiting",
                                     message="Values written.")
        return self._store.get_setup(server_id)

    def next(self, server_id: int, who: str) -> Setup:
        """Firewall done (or skipped): ready to start."""
        setup = self._store.get_setup(server_id)
        if setup.step != "firewall" or self.running(server_id):
            raise SetupError("Not at the firewall step")
        self._store.update_setup(server_id, step="ready", status="waiting", message="")
        self._audit(who, "setup.next", server_id, "firewall")
        return self._store.get_setup(server_id)

    def finish(self, server_id: int) -> None:
        if self.running(server_id):
            raise SetupError("A step is still running")
        self._store.get_setup(server_id)
        self._store.delete_setup(server_id)

    def close(self) -> None:
        self._closed.set()

    # -- the automatic steps ----------------------------------------------------------

    def _start(self, server_id: int) -> None:
        with self._mu:
            thread = self._threads.get(server_id)
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(target=self._run, args=(server_id,),
                                      name=f"setup {server_id}", daemon=True)
            self._threads[server_id] = thread
        thread.start()

    def _run(self, server_id: int) -> None:
        try:
            while not self._closed.is_set():
                setup = self._store.get_setup(server_id)
                server = self._store.get_server(server_id)
                if setup.step == "install":
                    ok, message = self._install(server, setup)
                    after = "first_start"
                elif setup.step == "first_start":
                    ok, message = self._first_start(server, setup)
                    after = "values"
                elif setup.step == "values" and not setup.required:
                    ok, message, after = True, "", "firewall"
                else:
                    self._store.update_setup(server_id, status="waiting")
                    return
                if not ok:
                    self._store.update_setup(server_id, status="failed", message=message)
                    self._sup.console(server_id).system(f"New game setup stopped: {message}")
                    log.warning("Setup of %s stopped at %s: %s", server.name, setup.step,
                                message)
                    return
                self._store.update_setup(server_id, step=after, status="running",
                                         message=message)
        except KeyError:
            return  # the server (and its setup) was deleted
        except Exception:
            log.exception("Setup of server %s failed", server_id)
            self._store.update_setup(server_id, status="failed",
                                     message="Basecamp error; see basecamp.log")

    def _install(self, server: Server, setup: Setup) -> tuple[bool, str]:
        if server.kind is not ServerKind.STEAM:
            return True, ""
        build = installed_build(server)
        if build is not None:
            return True, f"Already installed (build {build})."
        try:
            job = self._steam.enqueue(server.id, Kind.INSTALL, setup.who or "setup")
        except SteamError as e:
            return False, str(e)
        self._audit(setup.who, "install", server.id, f"{server.name} (new game setup)")
        while self._steam.job(server.id) is not None:
            if self._closed.wait(_POLL_S):
                return False, "Basecamp is stopping"
        run = self._store.get_steam_run(job.run_id)
        if run.result != "ok":
            return False, f"Install {run.result}: {run.message}"
        return True, run.message

    def _files_to_wait_for(self, server: Server, setup: Setup) -> list[str]:
        """The config files the required values go in, else all of them (absolute)."""
        wanted = [v["file"] for v in setup.required] or server.config_files
        out: list[str] = []
        for raw in wanted:
            try:
                path = configs.entry_path(server, raw)
            except configs.NotAllowed:
                continue
            if _norm(path) not in {_norm(p) for p in out}:
                out.append(path)
        return out

    def _first_start(self, server: Server, setup: Setup) -> tuple[bool, str]:
        files = self._files_to_wait_for(server, setup)
        if not files:
            return True, "The template lists no config files, so no first start is needed."
        if all(Path(f).is_file() for f in files):
            return True, "The config files are there already, so no first start is needed."
        sid = server.id
        console = self._sup.console(sid)
        if self._sup.state(sid) not in ACTIVE_STATES:
            self._audit(setup.who, "start", sid, f"{server.name} (new game setup: first start)")
            console.system("New game setup: first start, to write the config files")
            try:
                self._sup.start(sid)
            except SupervisorError as e:
                return False, f"The first start failed: {e}"
        deadline = time.monotonic() + self._settings.setup_start_timeout_s
        while self._sup.state(sid) is ServerState.STARTING:
            if time.monotonic() > deadline:
                self._stop(sid)
                return False, (f"It wasn't up after {self._settings.setup_start_timeout_s:g} s, "
                               "so it was stopped. See its console.")
            if self._closed.wait(_POLL_S):
                return False, "Basecamp is stopping"
        if self._sup.state(sid) is not ServerState.RUNNING:
            self._cancel_restart(sid)
            code = self._sup.status(sid).last_exit_code
            return False, ("The first start ended before the server was up"
                           + (f" (exit code {code})" if code is not None else "")
                           + ". See its console.")
        deadline = time.monotonic() + self._settings.setup_config_wait_s
        while not all(Path(f).is_file() for f in files) and time.monotonic() < deadline:
            if self._sup.state(sid) is not ServerState.RUNNING:
                break
            if self._closed.wait(_POLL_S):
                return False, "Basecamp is stopping"
        console.system("New game setup: stopping it after the first start")
        if not self._stop(sid):
            return False, "Couldn't stop it after the first start. See its console."
        missing = [f for f in files if not Path(f).is_file()]
        if not missing:
            return True, "The first start wrote the config files."
        absent = [f for f in missing if not Path(f).parent.is_dir()]
        if absent:
            return False, ("The first start didn't write " + ", ".join(absent)
                           + ", and its folder doesn't exist. Check the template's notes; "
                             "you can make the folder and press Try again.")
        return True, ("The first start didn't write " + ", ".join(ntpath.basename(f)
                                                              for f in missing)
                      + "; it will be made with the values.")

    def _stop(self, server_id: int) -> bool:
        self._cancel_restart(server_id)
        try:
            self._sup.stop(server_id)
        except SupervisorError as e:
            log.warning("Setup: stop of %s failed: %s", server_id, e)
        return self._sup.state(server_id) not in ACTIVE_STATES

    def _config_index(self, server: Server, file: str) -> int:
        """The place of a required value's file on the server's config list."""
        try:
            want = _norm(configs.entry_path(server, file))
        except configs.NotAllowed as e:
            raise SetupError(str(e)) from None
        for n, raw in enumerate(server.config_files):
            try:
                if _norm(configs.entry_path(server, raw)) == want:
                    return n
            except configs.NotAllowed:
                continue
        raise SetupError(f"{ntpath.basename(file)} isn't on the server's config files list "
                         "any more; add it under Edit")
