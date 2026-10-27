-- 0003: Phase 4. Per-server backup schedule and retention (R-50) and which Discord events
-- post (R-61). Add-only. Backups themselves are the zip files in the backups folder (D-35);
-- the webhook URLs are secrets and live in secrets.toml (D-37).

ALTER TABLE servers ADD COLUMN backup_every_h REAL NOT NULL DEFAULT 0;    -- 0 = no schedule
ALTER TABLE servers ADD COLUMN backup_keep INTEGER NOT NULL DEFAULT 10;
ALTER TABLE servers ADD COLUMN notify_off TEXT NOT NULL DEFAULT '[]';     -- JSON list of events
