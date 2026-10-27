"""SQLite store and the add-only migrations runner (docs/07-updates.md)."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from basecamp.models import Port, Server, ServerConfig, StopStep

log = logging.getLogger(__name__)

_MIGRATION_NAME = re.compile(r"^(\d{4})_[\w-]+\.sql$")

# servers columns that map 1:1 to ServerConfig fields (ports and stop_recipe are stored
# separately, _JSON_COLUMNS as JSON).
_PLAIN_COLUMNS = (
    "name", "kind", "install_dir", "exe_path", "args", "working_dir", "join_port",
    "start_on_boot", "boot_order", "steam_app_id", "steam_beta", "steam_validate", "save_dir",
    "log_source", "log_file", "rcon_host", "rcon_port", "real_exe_name", "ready_pattern",
    "ready_after_s", "min_free_ram_mb", "notes", "template_id", "backup_every_h", "backup_keep",
)
_JSON_COLUMNS = ("config_files", "notify_off", "stop_warning")


@dataclass(frozen=True)
class AuditEntry:
    id: int
    ts: str
    who: str
    action: str
    server_id: int | None
    detail: str


@dataclass(frozen=True)
class SteamRun:
    id: int
    server_id: int
    kind: str  # install, update
    who: str
    result: str  # queued, running, ok, failed, cancelled, interrupted
    message: str
    build_before: str
    build_after: str
    backup: str
    was_running: int
    started_again: int
    queued_at: str
    started_at: str | None
    ended_at: str | None


_STEAM_RUN_FIELDS = frozenset(SteamRun.__dataclass_fields__) - {"id", "server_id", "queued_at"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def list_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    found = []
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise ValueError(f"Bad migration file name: {path.name}")
        found.append((int(match.group(1)), path))
    versions = [v for v, _ in found]
    if len(versions) != len(set(versions)):
        raise ValueError("Two migrations share a number")
    return found


def migrate(conn: sqlite3.Connection, migrations_dir: Path) -> list[int]:
    """Apply any migrations not yet in schema_migrations, each in its own transaction.

    A DB that already has newer migrations (after a rollback to an older Basecamp) is fine:
    migrations only add things, so the older code just ignores them.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    done = []
    for version, path in list_migrations(migrations_dir):
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.execute("BEGIN")
            # executescript would commit first; run statements one by one inside BEGIN.
            for statement in _split_sql(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, path.name, _now()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        log.info("Applied migration %s", path.name)
        done.append(version)
    newer = applied - {v for v, _ in list_migrations(migrations_dir)}
    if newer:
        log.info("DB has migrations this version doesn't know: %s", sorted(newer))
    return done


def _split_sql(sql: str) -> list[str]:
    statements, current = [], []
    for line in sql.splitlines():
        current.append(line)
        if sqlite3.complete_statement("\n".join(current)):
            statement = "\n".join(current).strip()
            if statement.rstrip(";").strip() and not _only_comments(statement):
                statements.append(statement)
            current = []
    rest = "\n".join(current).strip()
    if rest and not _only_comments(rest):
        raise ValueError("Migration ends with an incomplete statement")
    return statements


def _only_comments(text: str) -> bool:
    return all(not ln.strip() or ln.strip().startswith("--") for ln in text.splitlines())


class Store:
    """Thread-safe access to servers, their ports and stop recipes."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    @classmethod
    def open(cls, path: Path, migrations_dir: Path) -> Store:
        conn = connect(path)
        migrate(conn, migrations_dir)
        return cls(conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- servers --------------------------------------------------------------

    def add_server(self, config: ServerConfig) -> Server:
        now = _now()
        values = self._row_values(config)
        cols = [*values, "created_at", "updated_at"]
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                marks = ", ".join("?" * len(cols))
                cur = self._conn.execute(
                    f"INSERT INTO servers ({', '.join(cols)}) VALUES ({marks})",
                    [*values.values(), now, now],
                )
                server_id = cur.lastrowid
                assert server_id is not None
                self._write_children(server_id, config)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_server(server_id)

    def update_server(self, server_id: int, config: ServerConfig) -> Server:
        values = self._row_values(config)
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    f"UPDATE servers SET {', '.join(f'{c} = ?' for c in values)}, updated_at = ?"
                    " WHERE id = ?",
                    [*values.values(), _now(), server_id],
                )
                if cur.rowcount == 0:
                    raise KeyError(server_id)
                self._conn.execute("DELETE FROM server_ports WHERE server_id = ?", (server_id,))
                self._conn.execute("DELETE FROM stop_steps WHERE server_id = ?", (server_id,))
                self._write_children(server_id, config)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_server(server_id)

    def delete_server(self, server_id: int) -> None:
        """Remove Basecamp's record only; game files are never touched (R-14)."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))
        if cur.rowcount == 0:
            raise KeyError(server_id)

    def get_server(self, server_id: int) -> Server:
        with self._lock:
            row = self._conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
            if row is None:
                raise KeyError(server_id)
            return self._to_server(row)

    def list_servers(self) -> list[Server]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM servers ORDER BY id").fetchall()
            return [self._to_server(row) for row in rows]

    # -- audit log (R-83) -----------------------------------------------------

    def add_audit(self, who: str, action: str, server_id: int | None = None,
                  detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log (ts, who, action, server_id, detail) VALUES (?, ?, ?, ?, ?)",
                (_now(), who, action, server_id, detail),
            )

    def list_audit(self, limit: int = 200, server_id: int | None = None) -> list[AuditEntry]:
        sql = "SELECT * FROM audit_log"
        params: list[object] = []
        if server_id is not None:
            sql += " WHERE server_id = ?"
            params.append(server_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [AuditEntry(**dict(row)) for row in rows]

    # -- SteamCMD runs (R-30..R-32) ---------------------------------------------

    def add_steam_run(self, server_id: int, kind: str, who: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO steam_runs (server_id, kind, who, queued_at) VALUES (?, ?, ?, ?)",
                (server_id, kind, who, _now()),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def update_steam_run(self, run_id: int, **fields: object) -> None:
        unknown = set(fields) - _STEAM_RUN_FIELDS
        if unknown:
            raise ValueError(f"unknown steam_runs fields: {sorted(unknown)}")
        for key in ("started_at", "ended_at"):
            if fields.get(key) is True:
                fields[key] = _now()
        with self._lock:
            self._conn.execute(
                f"UPDATE steam_runs SET {', '.join(f'{c} = ?' for c in fields)} WHERE id = ?",
                [*fields.values(), run_id],
            )

    def get_steam_run(self, run_id: int) -> SteamRun:
        with self._lock:
            row = self._conn.execute("SELECT * FROM steam_runs WHERE id = ?",
                                     (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return SteamRun(**dict(row))

    def last_steam_run(self, server_id: int) -> SteamRun | None:
        """The newest finished run (not one that is queued or running now)."""
        sql = ("SELECT * FROM steam_runs WHERE server_id = ?"
               " AND result NOT IN ('queued', 'running')")
        params: list[object] = [server_id]
        with self._lock:
            row = self._conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        return SteamRun(**dict(row)) if row is not None else None

    def end_unfinished_steam_runs(self) -> int:
        """Runs left queued or running by a Basecamp that stopped: mark them interrupted."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE steam_runs SET result = 'interrupted', ended_at = ?,"
                " message = 'Basecamp stopped during the run'"
                " WHERE result IN ('queued', 'running')", (_now(),))
        return cur.rowcount

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _row_values(config: ServerConfig) -> dict[str, object]:
        data = config.model_dump(mode="json")
        values: dict[str, object] = {col: data[col] for col in _PLAIN_COLUMNS}
        for col in _JSON_COLUMNS:
            values[col] = json.dumps(data[col])
        return values

    def _write_children(self, server_id: int, config: ServerConfig) -> None:
        self._conn.executemany(
            "INSERT INTO server_ports (server_id, port, protocol, label) VALUES (?, ?, ?, ?)",
            [(server_id, p.port, p.protocol.value, p.label) for p in config.ports],
        )
        self._conn.executemany(
            "INSERT INTO stop_steps (server_id, seq, action, command, wait_s)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (server_id, seq, s.action.value, s.command, s.wait_s)
                for seq, s in enumerate(config.stop_recipe)
            ],
        )

    def _to_server(self, row: sqlite3.Row) -> Server:
        server_id = row["id"]
        ports = [
            Port(port=r["port"], protocol=r["protocol"], label=r["label"])
            for r in self._conn.execute(
                "SELECT * FROM server_ports WHERE server_id = ? ORDER BY id", (server_id,)
            )
        ]
        steps = [
            StopStep(action=r["action"], command=r["command"], wait_s=r["wait_s"])
            for r in self._conn.execute(
                "SELECT * FROM stop_steps WHERE server_id = ? ORDER BY seq", (server_id,)
            )
        ]
        data = {col: row[col] for col in _PLAIN_COLUMNS}
        data.update({col: json.loads(row[col]) for col in _JSON_COLUMNS})
        return Server(
            id=server_id,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            ports=ports,
            stop_recipe=steps,
            **data,
        )
