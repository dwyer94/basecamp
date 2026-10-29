# 01 · Requirements

IDs (`R-xx`) are referenced from the plan. **v1** = first usable version. **Later** = on the list, not in v1.

## People

| Who | How they reach Basecamp | What they can do |
|---|---|---|
| **Admin** (you) | Browser on the prod PC, dev PC or phone, over Tailscale | Everything |
| **Friends** | Browser over Tailscale (only friends who are on the tailnet; see [03-friend-access.md](03-friend-access.md)) | Friend view; start, stop and restart only if the admin allows it (R-74) |

## Startup and uptime

- **R-01** Basecamp starts at **PC boot**, without anyone logging in (scheduled task "At startup", run whether the user is logged on or not).
- **R-02** On start, Basecamp starts every server whose **start on boot** toggle is on, one after another with a short gap (so they don't all hit the disk at once).
- **R-03** Several servers can run at the same time. Before starting one, Basecamp checks that its ports aren't already in use and warns (doesn't block) if free RAM looks too low.
- **R-04a** **A game server only stops when it's stopped from Basecamp** (or crashes, or the PC shuts down). Basecamp exiting, crashing, updating itself, or its scheduled task being ended must never take a game server down.
- **R-04** If Basecamp itself restarts while game servers keep running, it finds them again (by saved PID + exe path + start time) instead of starting duplicates. See "Orphaned servers" in [02-architecture.md](02-architecture.md).
- **R-05** **Crash auto-restart** (v1): if a server's process exits when Basecamp didn't ask it to, restart it after a short delay. Give up after 3 crashes in 10 minutes, mark it **Crashed**, and notify Discord.
- **R-06** A watchdog task re-launches Basecamp if it dies (a scheduled task every 5 minutes).

## Managing servers

- **R-10** Add a server with: name, type (**Steam** or **Custom**), install folder, exe path, launch arguments, working folder, ports (each with UDP/TCP and a label such as "game" or "query"), **join port** (the one friends type in), start-on-boot toggle.
- **R-11** Steam servers also have: Steam **app ID**, optional beta branch, "validate on update" toggle. Basecamp can do the **first install** too (run SteamCMD into an empty folder).
- **R-12** Optional per server: **config files** (list of paths), **save folder** (for backups), **log source** (stdout, or a log file path the game writes), **RCON** (host, port, password), **stop recipe** (see R-22), notes.
- **R-13** **Game templates** pre-fill the form for common games (Palworld, Enshrouded, Valheim, Satisfactory, etc.): app ID, default exe, args, ports, config file locations, save folder, stop recipe. Templates are plain JSON files in the repo, easy to add to; one in the data folder adds a game or overrides a built-in one without a release, and can be pasted or uploaded on the Templates page (D-31, D-129, [08-templates.md](08-templates.md)).
- **R-14** Edit and delete servers. Deleting removes Basecamp's record only; it never deletes game files (a separate "delete files too" option is **Later**).
- **R-15** Import an existing install: point at a folder you already have, and Basecamp reads `steamapps/appmanifest_*.acf` to fill in the app ID. The Add form lists the folders in SteamCMD's `steamapps\common` to pick from, and a new Steam server's install folder defaults to `steamapps\common\<game folder>` (batch 1, D-125).

- **R-16** **New game from a template, in one flow** (batch 2, D-130): pick the template → install folder (SteamCMD's default, R-15) → SteamCMD first install through the queue → a first start to write its config files, then a stop → the values the template marks as required (`required_values`, e.g. Dragonwilds' `OwnerId`), written into the config file as the editor saves it (R-41) → the firewall check (D-128) → ready to start. Each step shows its progress, the flow can be left and picked up again, and a step that fails stops there with the reason and can be tried again.

## Running servers

- **R-20** Start, stop, restart, per server. Also "stop all" and "start all on-boot servers".
- **R-21** States: **Stopped**, **Starting**, **Running**, **Stopping**, **Updating**, **Crashed**. Shown on the dashboard with uptime, CPU and RAM per server.
- **R-22** **Graceful stop**, as a per-server recipe: e.g. send `save` (stdin or RCON), wait 10 s, send `shutdown`/`quit`, wait up to 60 s for exit, then Ctrl+Break, then kill. Other steps: Ctrl+C, and waiting for the game's own autosave (for games that don't save on exit, D-121). Default recipe if none is set: Ctrl+Break, wait 30 s, kill.
- **R-23** **Live console**: shows server output live (last 2,000 lines kept in memory, full log on disk, rotated). Admin can type a command and send it via stdin or RCON.
- **R-25** **Warn players before a stop** (Phase 6.5): per server, a command sent by stdin or RCON with the minutes left filled in (e.g. `broadcast Restarting in {minutes} min`), at a lead time and reminders (e.g. 5 and 1 min; none = off). It runs before stops, restarts and updates started from Basecamp, never before a crash restart. The page shows a countdown with **Stop now**, which skips the rest; **Kill** skips it too.
- **R-26** **Scheduled restart** (batch 2, D-133): per server, daily at a time (off by default), only while it runs, with the stop warning (R-25) and the graceful stop; Discord posts as for a restart by hand. While players are on (R-73) it can go ahead, skip that day, or wait until they've left (up to 2 h); an unknown count counts as nobody on, and the console and audit log say so.
- **R-24** **Copy join address**: one button copies `<tailscale-ip>:<join port>`. A second button copies a Discord-ready line, e.g. `Palworld is up → <tailscale-ip>:8211 (password in pins)`. The address shown uses whichever connection method is configured ([03-friend-access.md](03-friend-access.md)), so it changes if you add a relay later.

## Updates

- **R-30** **One-click update** for Steam servers: back up save → graceful stop → SteamCMD `app_update` → start again (only if it was running before). Progress and SteamCMD output stream into the page.
- **R-31** Only one SteamCMD run at a time (SteamCMD doesn't like parallel runs); other updates queue.
- **R-32** If the update fails, don't start the server on half-updated files; mark it and show the SteamCMD output. The save backup from R-30 is kept.
- **R-33** Custom (non-Steam) servers: no update button in v1. **Later**: an optional per-server "update command" (a script you write).
- **R-34** **Update-available badge** (Phase 6.5): compare the installed build ID with Steam's for the server's branch, checked every 6 h and on **Check for update**, through the SteamCMD queue. A failed check shows "unknown".
- **R-35** **Scheduled update** (batch 2, D-133): per server, daily at a time (off by default), only when the update-available badge (R-34) says Steam has a newer build; the same steps as one-click update (R-30, R-31) and its Discord messages, and the same choice as R-26 while players are on.

## Config files

- **R-40** Each server lists its config files. The admin can open one in an editor in the page, edit and save.
- **R-41** On save, keep a timestamped copy of the old file (last 20). JSON is checked for syntax before saving; INI is saved as-is.
- **R-42** After saving, offer "restart now" if the server is running.
- **R-43** Guard: only files on the server's config list can be read or written; no browsing the whole disk from the web page.

## Backups

- **R-50** Per server: save folder, schedule (e.g. every 6 h), how many to keep (default 10).
- **R-51** Always back up before an update.
- **R-52** List backups and restore one (server must be stopped; current save is backed up first).
- **R-54** **Download a backup** from the page (admin only, Phase 6.5): the zip as it is, streamed from disk, audited. Only backups listed in that server's backups folder can be asked for.
- **R-53** Backups go to `C:\BasecampData\backups\<server>\` (the `backups_root` setting can move them, Q-08). **Later**: copy them to another drive or the dev PC.

## Discord

- **R-60** One webhook URL (optionally per server). Messages: server up (with join address), stopped, crashed, gave up restarting, update started/finished/failed, Basecamp started after a reboot.
- **R-61** Per-server toggle for which events post.
- **R-62** Webhook failures never block anything; they're logged and retried a few times.
- **R-63** **Live status message** (Phase 6.5, off by default, on the Settings page): one message on the main webhook showing every server's state and join address, edited in place (at most every 10 s), posted again if someone deletes it. Event messages post as before.

## Friend view

- **R-70** Read-only page: each server's name, status, join address with copy button, uptime, and player count where the game supports it (R-73).
- **R-71** No start/stop (unless R-74), no console, no config, no paths, no RCON passwords.
- **R-73** **Player count** (batch 2, D-132): for each running server whose game supports it, ask how many players are on (Palworld's REST API, an RCON command and pattern, or Steam's server query on a query port), about once a minute; "unknown" when it can't tell. On the admin and friend views; friends get the number only, never names.
- **R-74** **Friend permission** (batch 2, D-131): a setting, off by default: a list of friend logins, or anyone who reaches the page. Those friends get Start, Stop and Restart on the friend view and nothing else (no config, delete, update, console or passwords). Each click is audited with the login (or "unknown"); stops keep the warning (R-25); a server takes one friend click a minute. The login list works only if a friend's request carries their Tailscale login (Q-02, not yet checked); "anyone" works either way.
- **R-72** **Add to home screen** (Phase 6.5): both pages have a web app manifest and icons, so a phone opens Basecamp like an app. No offline mode (no service worker).

## Security

- **R-80** Admin actions need you to be the admin (Tailscale identity or password; see [02-architecture.md](02-architecture.md)).
- **R-81** Not reachable from the internet. Reachable only from the tailnet (and loopback on the prod PC).
- **R-82** Secrets (RCON passwords, webhook URL, admin password hash) stay in `C:\BasecampData\`, never in the repo, never sent to the friend view.
- **R-83** Every admin action is written to an audit log (who, what, when).

## Updating Basecamp itself

Details in [07-updates.md](07-updates.md).

- **R-90** Check the repo's GitHub Releases every 6 h and on "Check now". Show a banner with release notes and post to Discord when a newer version is out.
- **R-91** **Install** button: download, verify the Ed25519 signature and file hashes, back up the DB, swap to the new version via the launcher. Auto-install at a set hour is an option, off by default.
- **R-92** If the new version isn't healthy within 60 s, roll back to the previous version and DB copy automatically, and say so in the UI and on Discord.
- **R-93** Running game servers keep running through a Basecamp update.
- **R-94** The repo is public, so release checks and downloads need no token.

## Out of scope for v1 (Later list)

Ranked in [05-plan.md](05-plan.md), "After v1". Dropped: disk-space warnings and a PC-wide CPU/RAM graph (owner, 2026-10-28; a separate monitoring tool covers the PC). Multiple admins already work (`admin_logins` is a list); per-friend rights are the friend permission (R-74). Batch 2 moved the new-game flow (R-16), scheduled restarts and updates (R-26, R-35), the player count (R-73) and the friend permission (R-74) out of this list.

- Player names (only the count is shown, D-132)
- Update commands for custom servers (R-33)
- Steam login for games that need a real account (most dedicated servers are anonymous)
- Copying backups off the PC
- Mod management (Workshop, Thunderstore)
- A built-in relay for friends without Tailscale (see [03-friend-access.md](03-friend-access.md))
- A fresher UI: the page is a plain MVP
- Read-only debug endpoints for the admin: Basecamp's and the launcher's logs, console logs, health and internal state, settings without secrets (so the host PC can be checked from the dev PC)