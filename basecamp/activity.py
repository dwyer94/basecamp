"""The Activity feed (batch 3, D-134): the audit log as a feed, with hard failures that
count towards a red badge until they are dismissed or the server runs normally again.

Basecamp's own events (crashes, update results, scheduled backups, Discord failures) are
audit rows too, written by ``who = "basecamp"`` (or ``"schedule"``), so one table is the
record. Friends get the feed from an allow-list of actions, filtered on the server, and
each row from its own allow-list of fields (D-30): never the owner's events, logins or
details that could hold a path or a secret.
"""

from __future__ import annotations

from datetime import datetime

from basecamp.db import AuditEntry, Store

SYSTEM = "basecamp"  # who, for Basecamp's own events
SYSTEM_WHOS = frozenset({SYSTEM, "schedule", "auto-install", "settings.toml"})

# Hard failures (the red badge): crashes, giving up restarting, failed updates and
# installs, failed scheduled runs, failed backups and Discord messages.
FAILURES = (
    "crash", "crash.gave_up", "start.failed", "steam.update.failed", "steam.install.failed",
    "backup.failed", "schedule.restart.failed", "schedule.update.failed", "notify.failed",
)
# What clears a server's open failures: running normally again (a good start), a good
# update or install, a backup made. Failures with no server clear only on Dismiss.
_RUNNING_CLEARS = ("crash", "crash.gave_up", "start.failed", "schedule.restart.failed")
_CLEARED_BY = {
    "steam.update.ok": ("steam.update.failed", "steam.install.failed",
                        "schedule.update.failed"),
    "steam.install.ok": ("steam.install.failed", "steam.update.failed"),
    "backup": ("backup.failed",),
    "backup.scheduled": ("backup.failed",),
}

# What a friend sees of the feed: server events only, never the owner's (settings, config,
# templates, Basecamp updates, logins, Discord, backups restored or downloaded).
FRIEND_ACTIONS = (
    "start", "stop", "stop.now", "restart", "kill", "countdown.cancel",
    "friend.start", "friend.stop", "friend.restart", "friend.update", "friend.command",
    "friend.cancel",
    "console", "update", "install", "steam.cancel",
    "steam.update.ok", "steam.update.failed", "steam.update.cancelled",
    "steam.install.ok", "steam.install.failed", "steam.install.cancelled",
    "crash", "crash.gave_up", "start.failed",
    "backup", "backup.scheduled", "backup.failed",
    "schedule.restart", "schedule.update", "schedule.restart.skipped",
    "schedule.update.skipped", "schedule.restart.failed", "schedule.update.failed",
)
# Rows whose detail a friend may read: what was typed in the console (the owner decided
# friends see the console, D-134). Every other row is told from its action alone.
_FRIEND_DETAIL = frozenset({"console", "friend.command"})
FRIEND_ITEM_FIELDS = ("id", "ts", "who", "action", "server_id", "server", "detail", "kind",
                      "failure")


def kind(action: str) -> str:
    """The Activity filter a row belongs to."""
    if action in ("crash", "crash.gave_up", "start.failed", "kill"):
        return "crash"
    if action.startswith("friend."):
        return "friend"
    if action.startswith(("steam.", "update", "install", "schedule.update")):
        return "update"
    if action.startswith("backup"):
        return "backup"
    if action.startswith("notify"):
        return "discord"
    if action.startswith(("config", "console", "settings", "template", "server.", "setup",
                          "rcon.", "rest.")):
        return "config"
    return "server"


def record(store: Store, action: str, server_id: int | None, detail: str,
           who: str = SYSTEM) -> int:
    """Write one of Basecamp's own events, clearing what it makes good."""
    entry = store.add_audit(who, action, server_id, detail)
    clears = _CLEARED_BY.get(action)
    if clears and server_id is not None:
        store.clear_audit(clears, SYSTEM, server_id=server_id)
    return entry


def running_again(store: Store, server_id: int) -> None:
    """The server reached Running: its crash and start failures stop counting."""
    store.clear_audit(_RUNNING_CLEARS, SYSTEM, server_id=server_id)


def open_failures(store: Store) -> int:
    return store.count_open(FAILURES)


def dismiss(store: Store, who: str, entry_id: int | None = None) -> int:
    return store.clear_audit(FAILURES, who, entry_id=entry_id)


def _ts(entry: AuditEntry) -> float:
    try:
        return datetime.fromisoformat(entry.ts).timestamp()
    except ValueError:
        return 0.0


def admin_item(entry: AuditEntry, names: dict[int, str]) -> dict:
    failure = entry.action in FAILURES
    return {
        "id": entry.id, "ts": _ts(entry), "who": entry.who, "action": entry.action,
        "server_id": entry.server_id,
        "server": names.get(entry.server_id) if entry.server_id is not None else None,
        "detail": entry.detail, "kind": kind(entry.action), "failure": failure,
        "open": failure and entry.cleared_at is None,
        "cleared_by": entry.cleared_by if failure else "",
    }


def friend_who(entry: AuditEntry) -> str:
    """A friend sees a friend's login, "schedule" or "basecamp", and "owner" for every
    admin: the admin logins stay private."""
    if entry.action.startswith("friend."):
        return entry.who
    if entry.who in SYSTEM_WHOS:
        return entry.who
    return "owner"


def friend_item(entry: AuditEntry, names: dict[int, str]) -> dict:
    item = {
        "id": entry.id,
        "ts": _ts(entry),
        "who": friend_who(entry),
        "action": entry.action,
        "server_id": entry.server_id,
        "server": names.get(entry.server_id) if entry.server_id is not None else None,
        "detail": entry.detail if entry.action in _FRIEND_DETAIL else "",
        "kind": kind(entry.action),
        "failure": entry.action in FAILURES,
    }
    assert tuple(item) == FRIEND_ITEM_FIELDS
    return item


def feed(store: Store, is_admin: bool, *, limit: int = 200, server_id: int | None = None,
         before: int | None = None, failures_only: bool = False) -> list[dict]:
    names = {s.id: s.name for s in store.list_servers()}
    if is_admin:
        rows = store.list_audit(limit, server_id, before=before,
                                actions=FAILURES if failures_only else None)
        return [admin_item(e, names) for e in rows]
    allowed = tuple(a for a in FRIEND_ACTIONS if not failures_only or a in FAILURES)
    rows = store.list_audit(limit, server_id, before=before, actions=allowed)
    return [friend_item(e, names) for e in rows]
