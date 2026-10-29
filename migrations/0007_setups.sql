-- 0007: batch 2, item 6. A new game set up from a template in one flow (D-130): which step
-- it is on, so the flow can be left and picked up again. One row per server; the row goes
-- with the server. Add-only.

CREATE TABLE setups (
    server_id   INTEGER PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
    template_id TEXT    NOT NULL DEFAULT '',
    step        TEXT    NOT NULL,                  -- install, first_start, values, firewall, ready, done
    status      TEXT    NOT NULL,                  -- running, waiting, failed, done
    message     TEXT    NOT NULL DEFAULT '',
    required    TEXT    NOT NULL DEFAULT '[]',     -- the template's required_values, as JSON
    who         TEXT    NOT NULL DEFAULT '',
    started_at  TEXT    NOT NULL,                  -- UTC ISO 8601
    updated_at  TEXT    NOT NULL
);
