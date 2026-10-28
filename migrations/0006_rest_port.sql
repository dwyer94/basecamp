-- 0006: RCON and REST channels (D-92..D-97). Palworld's REST API port; null = none.
-- Passwords are in secrets.toml, never here. Add-only.

ALTER TABLE servers ADD COLUMN rest_port INTEGER;
