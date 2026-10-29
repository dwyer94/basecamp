-- 0009: batch 2, item 9. Daily scheduled restarts and updates (D-133): the time of day
-- (HH:MM, this PC's time; '' = off), and what to do while players are on. Add-only.

ALTER TABLE servers ADD COLUMN restart_at TEXT NOT NULL DEFAULT '';
ALTER TABLE servers ADD COLUMN update_at TEXT NOT NULL DEFAULT '';
ALTER TABLE servers ADD COLUMN schedule_players TEXT NOT NULL DEFAULT 'ignore';
