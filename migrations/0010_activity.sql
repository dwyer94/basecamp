-- 0010: batch 3. The Activity feed's red count (D-134): a hard failure counts until it is
-- cleared, by Dismiss (cleared_by = the login) or by the server running normally again
-- (cleared_by = 'basecamp'). Rows from before this version start cleared, so an upgrade
-- doesn't light up old failures. Add-only.

ALTER TABLE audit_log ADD COLUMN cleared_at TEXT;
ALTER TABLE audit_log ADD COLUMN cleared_by TEXT NOT NULL DEFAULT '';
UPDATE audit_log SET cleared_at = ts, cleared_by = 'before 0010';
CREATE INDEX IF NOT EXISTS audit_log_open ON audit_log (cleared_at, action);
CREATE INDEX IF NOT EXISTS audit_log_server ON audit_log (server_id, id);
