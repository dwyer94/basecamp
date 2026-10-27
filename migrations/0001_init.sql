-- 0001: servers, their ports and stop recipes (R-10..R-12, R-22).
-- Add-only from here on (docs/07-updates.md): later files add tables or columns with defaults.
-- Enum-like columns have no CHECK constraints on purpose: adding a value later would need a
-- table rebuild. The pydantic models in basecamp/models.py validate them instead.

CREATE TABLE servers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    kind            TEXT    NOT NULL DEFAULT 'custom',   -- steam | custom
    install_dir     TEXT    NOT NULL DEFAULT '',
    exe_path        TEXT    NOT NULL,
    args            TEXT    NOT NULL DEFAULT '',         -- command-line tail, as typed
    working_dir     TEXT    NOT NULL DEFAULT '',         -- '' = the exe's folder
    join_port       INTEGER,
    start_on_boot   INTEGER NOT NULL DEFAULT 0,
    boot_order      INTEGER NOT NULL DEFAULT 0,
    steam_app_id    INTEGER,
    steam_beta      TEXT    NOT NULL DEFAULT '',
    steam_validate  INTEGER NOT NULL DEFAULT 0,
    config_files    TEXT    NOT NULL DEFAULT '[]',       -- JSON list of paths
    save_dir        TEXT    NOT NULL DEFAULT '',
    log_source      TEXT    NOT NULL DEFAULT 'stdout',   -- stdout | file
    log_file        TEXT    NOT NULL DEFAULT '',
    rcon_host       TEXT    NOT NULL DEFAULT '127.0.0.1',
    rcon_port       INTEGER,                             -- password lives in secrets.toml
    real_exe_name   TEXT    NOT NULL DEFAULT '',         -- launcher games: the exe to track
    ready_pattern   TEXT    NOT NULL DEFAULT '',         -- output text that means "up"
    ready_after_s   REAL    NOT NULL DEFAULT 15,
    min_free_ram_mb INTEGER NOT NULL DEFAULT 0,
    notes           TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE server_ports (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    port      INTEGER NOT NULL,
    protocol  TEXT    NOT NULL DEFAULT 'udp',            -- udp | tcp
    label     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX server_ports_server ON server_ports(server_id);

-- Ordered steps; empty = the default recipe (Ctrl+Break, wait 30 s, kill).
CREATE TABLE stop_steps (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    action    TEXT    NOT NULL,                          -- stdin | rcon | ctrl_break
    command   TEXT    NOT NULL DEFAULT '',
    wait_s    REAL    NOT NULL DEFAULT 10
);
CREATE INDEX stop_steps_server ON stop_steps(server_id, seq);
