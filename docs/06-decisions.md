# 06 · Decisions and open questions

## Decisions

| ID | Decision | Date | Why |
|---|---|---|---|
| D-01 | Name: **Basecamp** | 2026-10-26 | Owner's pick |
| D-02 | Several servers can run at once, each with its own start-on-boot toggle | 2026-10-26 | Owner's pick |
| D-03 | Two roles: admin (full) and friend (read-only view) | 2026-10-26 | Owner's pick |
| D-04 | v1 extras: crash auto-restart, save backups, Discord webhook. Scheduled updates are Later | 2026-10-26 | Owner's pick |
| D-05 | Live console with commands (stdin or RCON) | 2026-10-26 | Owner's pick |
| D-06 | Python 3.13 + FastAPI + SQLite, plain HTML/JS UI | 2026-10-26 | Matches owner's other projects; no build step |
| D-07 | Run as a scheduled task at startup, own Python copy, own folders | 2026-10-26 | Starts before login; own runtime keeps it apart from other apps' firewall rules |
| D-08 | Bind to `127.0.0.1:8150`; reach it via `tailscale serve` (Plan A), fallback Plan B | 2026-10-26 | Loopback-only fits the rules; HTTPS for the copy button; identity without passwords |
| D-09 | Game servers outlive Basecamp restarts and are reattached, console included (via their host process, D-25). Only if a host itself dies is the game adopted bare, without console until restarted | 2026-10-26 | Updating Basecamp shouldn't kick friends off |
| D-11 | Runs as the owner's own account (not elevated, task set to "Do not store password"), no dedicated Windows user. Game installs, saves and SteamCMD stay where they are | 2026-10-26 | Nothing is on the public internet, so a separate user is extra hassle (moving saves, permissions) for little gain. Revisit if servers go public (playit.gg etc.) |
| D-12 | Basecamp updates itself from GitHub Releases via a small launcher that swaps version folders and rolls back if the new one isn't healthy | 2026-10-26 | Owner's idea; no manual copying to the prod PC |
| D-13 | Releases are built and signed (Ed25519) on the dev PC, not in GitHub Actions; Basecamp only installs signed zips | 2026-10-26 | Basecamp runs what it downloads; the key never leaves the dev PC, so a GitHub account breach can't push code to the prod PC |
| D-14 | Self-update installs on the admin's tap by default; auto-install at a set hour is an option | 2026-10-26 | Updates drop the live console of running servers until they restart, so you choose when |
| D-15 | **Public** GitHub repo. Release checks need no token | 2026-10-26 | Nothing sensitive in the code; simpler setup. Secrets, the data folder and the signing key never go in the repo |
| D-10 | Friend access: research only for now; recommend trying Tailscale device sharing first | 2026-10-26 | See [03-friend-access.md](03-friend-access.md) |
| D-16 | Supervisor uses threads (a watcher and an output pump per running server), not asyncio | 2026-10-26 | Blocking Windows process calls are simpler on threads; the Phase 2 API calls in from worker threads and gets state changes from `Supervisor.add_listener` |
| D-17 | Ctrl+Break is raised by attaching to the game's hidden console, not by `os.kill(pid, CTRL_BREAK_EVENT)`: normally by the server's host (D-25), which has no console of its own; the `basecamp/winctrl.py` helper process is the fallback for bare adopted games | 2026-10-26 | `CREATE_NO_WINDOW` gives each game its own hidden console, and Windows only delivers console events to the sender's own console. Basecamp under `pythonw.exe` has no console at all. The helper also works for adopted servers |
| D-18 | Stop recipe = ordered steps (`stdin`, `rcon`, `ctrl_break`), each "do it, then wait up to N s for the exit"; kill always follows. A step that can't be done (no stdin after adoption, RCON not wired yet) is skipped without waiting. No recipe = Ctrl+Break, 30 s, kill (R-22) | 2026-10-26 | Covers the R-22 example ("save, wait 10 s, quit, wait 60 s, Ctrl+Break, kill") and degrades sensibly for adopted servers |
| D-19 | Duplicate ports are refused at **start**, not when saving a server: a port is busy if another active Basecamp server lists it (even before that game has bound it) or the OS says it's bound (exclusive bind test plus the netstat table) | 2026-10-26 | Two configs of the same game on one port are fine as long as only one runs |
| D-20 | Phase 1: an unexpected exit ends in **Crashed**; auto-restart (R-05) comes in Phase 4 on top of the state listener | 2026-10-26 | Keeps the crash policy in one place, built with the Discord notifier |
| D-21 | Hosts (D-25) and the games they start are started with `CREATE_BREAKAWAY_FROM_JOB` when the job allows it, else without | 2026-10-26 | Task Scheduler runs tasks in a job object; breaking away keeps games alive if the Basecamp task is stopped (D-09). Check on the prod PC in Phase 0 (Q-10) |
| D-22 | Launcher games: the template's real exe name is looked for among the launcher's descendants (by parent PID scan, which still works after the launcher exits) for up to 60 s; until then the launcher is tracked. The host does this (ctypes Toolhelp snapshot), so it works while Basecamp is down | 2026-10-26 | Palworld-style launchers exit soon after starting the real server |
| D-23 | No `CHECK` constraints on enum-like columns; the pydantic models validate them. RCON passwords are not in the DB (they go in `secrets.toml`) | 2026-10-26 | A new enum value would otherwise need a table rebuild, which add-only migrations forbid. Secrets rule (R-82) |
| D-24 | Dev PC venv uses CPython 3.13 fetched by `uv` (the PC only has 3.14 installed) | 2026-10-26 | Matches the release target (`--python-version 3.13`, docs/07) |
| D-25 | **One host process per running server** (`basecamp/host.py`, standard library only). Basecamp starts it detached and outside its job; the host starts the game, owns its pipes, writes the console log, keeps a replay buffer, and serves a control channel on `127.0.0.1` (ephemeral port, per-run random token stored in the data folder). It exits only after its game, leaving `exit.json`. Basecamp reattaches via `state.json` (host PID + exe + create time, port, token); an exit while Basecamp was down is reported on reattach as Crashed unless the stop was asked for. Host gone but game alive: adopt the bare game as before | 2026-10-26 | Owner's call (R-04a): a game must never depend on Basecamp's pipes, and the console must survive Basecamp restarts and self-updates. TCP loopback over a named pipe: simpler with the standard library, and binding to `127.0.0.1` needs no firewall rule |
| D-26 | Host compatibility: the hello negotiates `min(Basecamp's protocol, host's protocol)`. Protocol changes are additive only (new ops or fields, never removed or changed); each side keeps speaking every older version; a client only uses ops in the negotiated version. A Basecamp that meets a host below its `MIN_HOST_PROTOCOL` treats the game as bare. Hosts run from a copy in `<data>\hosts\bin\` named by content hash, never from a version folder | 2026-10-26 | A host lives as long as its game, so it can be several Basecamp releases old, or newer after a rollback. The launcher prunes old version folders (docs/07) |
| D-27 | **Plan A confirmed** for auth: Basecamp binds to `127.0.0.1:8150`, `tailscale serve` publishes it over HTTPS, admin = owner's `Tailscale-User-Login` (or localhost), everyone else = friend view. Plan B (password) not built | 2026-10-26 | Q-02 test from the owner's phone and both PCs: login header present, page secure, clipboard copy works |

## Open questions

| # | Question | Answer by |
|---|---|---|
| Q-01 | Which games do you run today, where are they installed, and on which ports? | Owner, Phase 0 |
| Q-02 | ~~Do identity headers work through `tailscale serve` on the tagged prod PC?~~ **Yes** from the owner's devices (D-27). Still untested: a friend's device (does it load, and does the header carry their login?) | Friend check before Phase 4 |
| Q-03 | Do people you share a device with count against your Tailscale user limit? | Phase 0 test with one friend |
| Q-04 | Do your games run correctly headless (no one logged in) with output piped? | Phase 0 test |
| Q-05 | Any non-Steam servers today? How do you update them? | Owner |
| Q-06 | Do any games need a real Steam login (not anonymous) to download? | Phase 0 |
| Q-07 | One Discord channel for everything, or one per game? | Owner, before Phase 4 |
| Q-08 | Backups on the same disk OK for v1? (Copying off the PC is Later.) | Owner, before Phase 4 |
| Q-09 | ~~Does each game keep running when Basecamp exits and its pipes break?~~ Mostly moot since D-25: the host owns the pipes, so Basecamp exiting no longer breaks them. Still worth one check: does a game survive its **host** dying (the rare fallback in D-25)? | Phase 0 (low priority) |
| Q-10 | Does the "At startup" task's job object allow breakaway (D-21), and does ending the task kill the hosts or game servers? (R-04a says it must not) | Phase 0 test |