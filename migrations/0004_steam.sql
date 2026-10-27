-- 0004: Phase 3. One row per SteamCMD install or update (R-11, R-30..R-32), so a failed
-- update stays marked and its output can be shown after a Basecamp restart. Add-only.

CREATE TABLE steam_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id     INTEGER NOT NULL,                -- no foreign key: runs outlive servers
    kind          TEXT    NOT NULL,                -- 'install' or 'update'
    who           TEXT    NOT NULL DEFAULT '',     -- who pressed the button
    result        TEXT    NOT NULL DEFAULT 'queued', -- queued, running, ok, failed, cancelled, interrupted
    message       TEXT    NOT NULL DEFAULT '',
    build_before  TEXT    NOT NULL DEFAULT '',
    build_after   TEXT    NOT NULL DEFAULT '',
    backup        TEXT    NOT NULL DEFAULT '',     -- the pre-update backup's file name
    was_running   INTEGER NOT NULL DEFAULT 0,
    started_again INTEGER NOT NULL DEFAULT 0,
    queued_at     TEXT    NOT NULL,                -- UTC ISO 8601
    started_at    TEXT,
    ended_at      TEXT
);

CREATE INDEX steam_runs_server ON steam_runs (server_id, id);
