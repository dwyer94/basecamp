-- 0008: batch 2, item 8. How to ask a game for its player count (D-132), as JSON; null =
-- don't ask. Add-only.

ALTER TABLE servers ADD COLUMN players TEXT NOT NULL DEFAULT 'null';
