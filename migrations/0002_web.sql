-- 0002: Phase 2 web API. Audit log of admin actions (R-83) and the template a server came
-- from (R-13). Add-only.

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,                 -- UTC ISO 8601
    who         TEXT    NOT NULL,                 -- Tailscale login, or 'localhost'
    action      TEXT    NOT NULL,                 -- 'start', 'server.update', 'console', ...
    server_id   INTEGER,                          -- no foreign key: entries outlive servers
    detail      TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX audit_log_ts ON audit_log (ts);

ALTER TABLE servers ADD COLUMN template_id TEXT NOT NULL DEFAULT '';
