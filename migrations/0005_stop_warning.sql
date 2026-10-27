-- 0005: Phase 6.5. Per-server warning to players before a stop, restart or update (R-25,
-- D-61): JSON {"action", "command", "minutes"}, or null for none. Add-only.

ALTER TABLE servers ADD COLUMN stop_warning TEXT NOT NULL DEFAULT 'null';
