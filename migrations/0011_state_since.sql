-- 0011: batch 4, item 13. When each server's state last changed, and how many crashes
-- Basecamp gave up after (D-142), so "Stopped since 23:40" and "gave up after 3 tries"
-- survive a Basecamp restart. One row per server; the row goes with the server. Add-only:
-- an older Basecamp ignores the table.

CREATE TABLE server_status (
    server_id     INTEGER PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
    state         TEXT    NOT NULL DEFAULT '',     -- the state it changed to
    since         REAL    NOT NULL DEFAULT 0,      -- when, epoch seconds
    gave_up_after INTEGER NOT NULL DEFAULT 0       -- crashes before Basecamp stopped retrying
);
