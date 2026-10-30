"""Read-only debug views for the owner (batch 4, item 11; docs/09-debug.md).

So a problem on the host PC can be looked at from the dev PC over Tailscale, as the owner's
login, without remoting in: Basecamp's own log and the launcher's, a server's console log,
health, the settings and recent errors. Admin only, every call audited (api/app.py).

Rules:
- Only Basecamp's own files, picked by name here (``LOGS``) or by server id. Nothing in a
  request is ever used as a path.
- Never a secret. Settings come from an allow-list (``SETTINGS``); secrets.toml only says
  which secrets are set. Every line of text that goes out passes ``Redactor``: the values
  in secrets.toml, Discord webhook URLs, and anything after a "password", "token" or
  "secret" key are replaced with ``<hidden>``.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from basecamp import notify
from basecamp.settings import Settings

MAX_LINES = 2000  # per call
DEFAULT_LINES = 200
_TAIL_BYTES = 4 * 1024 * 1024  # read at most this much of each file
HIDDEN = "<hidden>"

# The settings the debug view shows. An allow-list: a setting added later stays out until
# it is added here on purpose. None of these are secrets (those live in secrets.toml).
SETTINGS = (
    "host", "port", "boot_gap_s", "low_ram_mb", "real_exe_timeout_s", "host_start_timeout_s",
    "kill_wait_s", "console_lines", "console_log_bytes", "console_log_backups", "join_host",
    "admin_logins", "friend_control", "friend_control_logins", "friend_control_gap_s",
    "crash_restart_delay_s", "crash_limit", "crash_window_s", "backups_root",
    "backup_check_s", "notify_retry_s", "discord_live_status", "live_status_every_s",
    "steamcmd_path", "steamcmd_timeout_s", "steam_check_h", "steam_check_poll_s",
    "steam_check_timeout_s", "default_backup_every_h", "default_backup_keep",
    "config_copies_keep", "players_poll_s", "players_timeout_s", "schedule_check_s",
    "schedule_window_min", "schedule_retry_min", "schedule_delay_max_min",
    "setup_start_timeout_s", "setup_config_wait_s", "remote_timeout_s",
    "remote_console_allow_remote", "update_repo", "update_check_h", "update_beta",
    "update_auto_hour",
)

# "password=x", "-AdminPassword x", "token: x", '"secret": "x"' ...
_KEYED = re.compile(r"(?i)(\b[\w.-]*(?:password|passwd|pwd|token|secret)[\w.-]*\b[\"']?"
                    r"\s*(?:[=:]\s*|\s+))(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)")
_WEBHOOK = re.compile(r"(?i)https?://[^\s\"'<>]*/api/webhooks/[^\s\"'<>]+")
# basecamp.log's warnings and errors ("<date> <time> WARNING name: ...").
PROBLEM = re.compile(r"^\S+ \S+ (?:WARNING|ERROR|CRITICAL)\b")
# A log line's own time: "2026-10-29 19:48:01" (basecamp.log, launcher.log, console.log).
_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)")


class Redactor:
    """Blanks secrets out of text: the strings in secrets.toml, read when made (so a
    password set a moment ago is covered), Discord webhook URLs and keyed values."""

    def __init__(self, settings: Settings, extra: Iterable[str] = ()):
        found = set(_strings(notify.load_secrets(settings)))
        found.update(extra)
        self._secrets = sorted((s for s in found if len(s) >= 4), key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, HIDDEN)
        text = _WEBHOOK.sub(HIDDEN, text)
        return _KEYED.sub(lambda m: m.group(1) + HIDDEN, text)


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def parse_since(text: str | None) -> datetime | None:
    """``since`` from the query string: ISO date and time, this PC's local time
    (``2026-10-29T19:00``), or epoch seconds. ValueError if it's neither."""
    if not text:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromtimestamp(float(text))
    return datetime.fromisoformat(text).replace(tzinfo=None)


def tail(paths: list[Path], lines: int = DEFAULT_LINES, since: datetime | None = None,
         redact: Redactor | None = None, only: re.Pattern[str] | None = None) -> dict:
    """The last ``lines`` lines of a log and its rotated copies (``paths``, newest first),
    optionally only those from ``since`` on (a line without a time, such as a traceback,
    goes with the line before it) or matching ``only``. Reads at most a few MB per file."""
    lines = max(1, min(int(lines), MAX_LINES))
    found: list[str] = []
    files = []
    for path in paths:
        chunk = _read_end(path)
        if chunk is None:
            continue
        files.append(path.name)
        found = chunk + found
        if len(found) >= lines and since is None:
            break
        if since is not None and chunk and _stamp(chunk[0]) and _stamp(chunk[0]) < since:
            break  # this file starts before since: older files are all older
    if since is not None:
        kept, keep = [], False
        for line in found:
            when = _stamp(line)
            if when is not None:
                keep = when >= since
            if keep:
                kept.append(line)
        found = kept
    if only is not None:
        found = [line for line in found if only.search(line)]
    more = len(found) > lines
    found = found[-lines:]
    if redact is not None:
        found = [redact(line) for line in found]
    return {"files": files, "lines": found, "truncated": more}


def _read_end(path: Path) -> list[str] | None:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            raw = f.read()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if size > _TAIL_BYTES:
        text = text.split("\n", 1)[-1]  # a cut first line
    return text.splitlines()


def _stamp(line: str) -> datetime | None:
    m = _STAMP.match(line)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1))
    except ValueError:
        return None


def rotated(path: Path, backups: int) -> list[Path]:
    """``x.log``, ``x.log.1`` ... ``x.log.<backups>``, newest first."""
    return [path] + [path.with_name(f"{path.name}.{n}") for n in range(1, backups + 1)]


def settings_view(settings: Settings) -> dict:
    out: dict[str, object] = {}
    for key in SETTINGS:
        value = getattr(settings, key)
        out[key] = list(value) if isinstance(value, tuple) else value
    return out


def secrets_view(settings: Settings, server_ids: Iterable[int]) -> dict:
    """Which secrets are set, never what they are."""
    data = notify.load_secrets(settings)
    main, per_server = notify.webhooks(settings)
    out: dict[str, object] = {"discord_webhook": bool(main),
                              "server_webhooks": sorted(per_server)}
    for channel in ("rcon", "rest"):
        table = data.get(channel, {})
        servers = table.get("servers", {}) if isinstance(table, dict) else {}
        out[f"{channel}_passwords"] = sorted(
            int(k) for k, v in (servers.items() if isinstance(servers, dict) else ())
            if v and str(k).isdigit() and int(k) in set(server_ids))
    return out
