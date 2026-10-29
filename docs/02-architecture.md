# 02 · Architecture

## One process, one page

```
 phone / dev PC browser
        │  https://<prod-pc>.<tailnet>.ts.net   (Tailscale only)
        ▼
 tailscaled  ── "tailscale serve" ──►  127.0.0.1:8150
                                         │
                              ┌──────────┴───────────┐
                              │  Basecamp (Python)    │
                              │  FastAPI + Uvicorn    │
                              │  ├ Supervisor         │──► host process per server ──► game server
                              │  ├ SteamCMD runner    │──► steamcmd.exe (one at a time)
                              │  ├ Backup scheduler   │──► C:\BasecampData\backups
                              │  ├ Discord notifier   │──► discord.com webhook (internet)
                              │  └ SQLite store       │──► C:\BasecampData\basecamp.db
                              └───────────────────────┘
```

Basecamp is a single Python process, plus one small **host process** per running game server (`basecamp/host.py`). The host owns the game's stdin and stdout and outlives Basecamp, so a game never depends on Basecamp staying up (R-04a) and its console keeps working across Basecamp restarts and self-updates. Basecamp talks to each host over a local control channel. The web page is served by Basecamp; the live console and status use a WebSocket.

## How the page is reached (and who you are)

**Plan A (preferred, check in Phase 0): `tailscale serve`.** Basecamp binds to `127.0.0.1:8150` only. `tailscale serve` (run by the Tailscale service, already on the PC) publishes it on the tailnet at `https://<machine>.<tailnet>.ts.net` with a real HTTPS certificate. This gives three things for free:

1. **Loopback-only binding**, which matches the prod PC rules ([04-deployment.md](04-deployment.md)) and needs no new inbound firewall rule for Basecamp.
2. **HTTPS**, which the browser needs for the one-click copy button (`navigator.clipboard` only works on HTTPS or localhost). On plain `http://<tailscale-ip>:8150` the copy button would need an older fallback trick.
3. **Identity headers**: Tailscale adds `Tailscale-User-Login` to each request from a user's device, so Basecamp knows it's you (admin) or a friend without a password.

To check in Phase 0: that identity headers arrive with the prod PC carrying a Tailscale tag, that MagicDNS and HTTPS certificates are on in the tailnet, and that the tailnet policy lets friends reach port 443 on the prod PC (today they may only be allowed the game ports).

**Plan B (fallback):** bind to `<tailscale-ip>:8150` and `127.0.0.1:8150`, add a scoped inbound firewall rule, admin logs in with a password (Argon2 hash in the data folder, session cookie). Friend view needs no login. The copy button uses the `execCommand('copy')` fallback.

Either way: **admin** = your Tailscale login (Plan A) or a valid admin session (Plan B), and **localhost** on the prod PC also counts as admin so you can always get in from the PC itself. Everyone else gets the friend view.

**Built (Plan A, D-27):** `basecamp/auth.py`.

- The login header is trusted only from a loopback peer.
- A login in `admin_logins` (data-folder `settings.toml`) is admin; any other login is a friend. An edit to the file applies on the next request (it's looked at every 3 s, D-127), and an open status or console WebSocket whose login gained or lost admin is closed so the page reloads as what it is now.
- A request with no Tailscale or forwarding headers and a loopback `Host` is the PC itself, the local admin.
- Anything else, such as Serve traffic from a tagged device, is a friend (D-28).
- Cross-site requests and unknown `Host` names are refused (D-29).

## Code layout

```
launcher.py          stable, stdlib only: runs the current version, swaps on update, rolls back (docs/07)
basecamp/
  main.py            app start: run migrations, load config, start supervisor, start web server
  selfupdate.py      check GitHub Releases, download, verify signature, unpack, hand over to launcher
  settings.py        paths, port, data folder (env vars / settings.toml)
  db.py              SQLite: servers, events, audit log
  models.py          pydantic models: Server, Port, StopRecipe, ...
  supervisor.py      process lifecycle, state machine, reattach/adoption
  crashes.py         crash auto-restart policy, a supervisor listener (D-39)
  host.py            per-server host process (stdlib only): owns the game's pipes, console log, control channel
  hostclient.py      Basecamp's side of the host control channel
  winctrl.py         fallback Ctrl+Break / Ctrl+C helper (bare adopted games, old hosts)
  console.py         in-memory console per server (mirrors the host's lines), subscribers
  rcon.py            Source RCON client, stdlib (V Rising, Palworld and many others; D-92, D-93)
  rest.py            Palworld's REST API client, stdlib (D-95)
  remote.py          RCON/REST passwords (secrets.toml) and kept connections, per server (D-97)
  steam.py           SteamCMD runner and queue, install and update flow, build ID (D-41..D-47)
  backups.py         zip save folders, schedule, retention, restore (D-35, D-36)
  configs.py         config file guard (R-43), encodings, copies kept on save, restore (D-48..D-51)
  notify.py          Discord webhook: secrets.toml, queue, retry (D-37, D-38)
  livestatus.py      the live status message in Discord, edited in place (D-63)
  auth.py            who is asking: admin or friend (D-28, D-29)
  templates.py       game templates (built-in + data folder), appmanifest import, steamapps\common list
  firewall.py        firewall indicator: reads the inbound rules, matches them per port (read-only)
  api/app.py         FastAPI app: REST routes, status and console WebSockets, pages
  api/views.py       admin view vs friend view of a server (D-30)
  web/               index.html, friend.html, app.js, common.js, style.css (no build step)
migrations/          numbered SQL files, add-only
templates/           built-in game templates, one JSON per game (docs/08-templates.md)
tools/fake_server.py fake game server for tests: prints, reads stdin, binds a UDP port, can "crash"
tools/fake_steamcmd.py, fake_discord.py   fake SteamCMD (installs, updates, app_info_print) and webhook
tools/fake_rcon.py, fake_rest.py   fake RCON server and Palworld REST API: bad password, slow, dropped, multi-packet
tools/make_icons.py  draws the home-screen PNG icons in basecamp/web/ (R-72)
tests/
scripts/             release.ps1 + build_release.py and new-signing-key.ps1 (dev PC), later install / uninstall for the prod PC
CHANGES.md           release notes, one section per version
```

## Data

All under `C:\BasecampData\` (dev PC: `.\data\`):

| Path | What |
|---|---|
| `basecamp.db` | SQLite: servers, settings, event history, audit log |
| `secrets.toml` | Discord webhook URLs (D-37), RCON and REST passwords per server (D-97). Kept out of the repo |
| `boot.json` | The PC boot time Basecamp last started under, for the "started after a restart" message |
| `logs\basecamp.log` | The app's own log, rotated |
| `logs\servers\<id>\console.log` | Each server's output, rotated (10 MB × 5). SteamCMD output lands here too |
| `logs\steamcmd\<id>\<run>.log` | The output of one SteamCMD install or update (the page's **Output** link) |
| `backups\<id>\` | Save backups (`YYYYMMDD-HHMMSS-<reason>.zip`, D-35). `backups_root` in settings.toml can put them elsewhere |
| `config-history\<id>\<file>-<hash>\` | Old copies of a config file, one per save or restore, newest 20 kept (D-50) |
| `db-backups\` | `before-<version>.db`: copy of `basecamp.db` taken as Basecamp exits for a self-update, for rollback (newest 5, D-55) |
| `selfupdate.json` | Self-update state: the latest release seen and its ETag, the version unpacked and ready, what was posted to Discord, the last update and rollback (D-57) |
| `updates\` | Only without the launcher (dev runs): releases unpacked and checked, which can't be installed from there (D-60) |
| `steam-latest.json` | The last build check per server: app, branch, Steam's build ID or the error, when (D-65) |
| `live-status.json` | The live status message's id and a hash of the webhook it is on (D-63) |
| `state.json` | Running servers: host PID + exe + create time, control port and token, game PID + exe + create time. Written on every change, used to reattach after a Basecamp restart |
| `hosts\<id>\` | `spec.json` (what the host runs, with its control token), `host.json` (control port), `exit.json` (how the last run ended, until Basecamp collects it), `host.log` |
| `hosts\bin\` | Copies of `host.py` by content hash; running hosts run from here, never from a version folder |

Game installs stay wherever they are today (each server stores its own install folder). SteamCMD's path is a setting, default `%USERPROFILE%\Desktop\SteamCmd\steamcmd.exe`. New installs default to `C:\GameServers\<name>\`.

## Server state machine

```
 Stopped ──start──► Starting ──(process alive N s)──► Running
    ▲                  │                                │  │
    │                  └──exit──► (crash handling) ◄────┘  │ stop / update
    │                                  │                   ▼
    │        3 crashes in 10 min ──► Crashed           Stopping ──exit──► Stopped
    │                                                      │
    └────────────── update done ◄── Updating ◄─────────────┘ (if update)
```

- **Starting → Running** after the process has stayed up for 15 s (per-template; some games take longer), or sooner when a "ready" line from the template appears in the output (e.g. Palworld's "Running Palworld dedicated server").
- An exit Basecamp didn't ask for = crash. Wait 10 s, restart. Third crash in 10 minutes → **Crashed**, Discord alert, no more retries until you press start.
- Actions are per-server and serialised (a lock per server), so pressing restart twice or update during a stop can't tangle.

## Starting a process on Windows

```
 Basecamp ──spawn (detached, breakaway)──► host.py ──spawn (pipes)──► game server
     │                                        │  console.log (rotated), last 2,000 lines
     └──── control channel: 127.0.0.1:<ephemeral port>, token ────┘
```

- Basecamp writes `hosts\<id>\spec.json` (command line, folder, log source, real exe name, a fresh random token) and starts `pythonw -I hosts\bin\host-<hash>.py --spec ...` with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, plus `CREATE_BREAKAWAY_FROM_JOB` when the job allows it (D-21). The host is standard library only, so it doesn't need Basecamp's packages.
- The host starts the game with `Popen(stdin=PIPE, stdout=PIPE, stderr=STDOUT, creationflags=CREATE_NO_WINDOW)`. The game gets its own hidden console, so **Ctrl+Break** (or **Ctrl+C**) reaches just that server: the host attaches to that console and raises the event. Before starting it, the host turns its own "ignore Ctrl+C" back off (it has it from being started in a new process group) and doesn't start the game in a new group either, since both would leave the game ignoring Ctrl+C (D-120). A Windows GUI exe (many Unreal `-Win64-Shipping.exe` servers) gets no console from this; with `-log` it opens its own, which is the one the host attaches to.
- The host pushes each output line into its ring buffer, the rotated `console.log`, and any attached Basecamp. Basecamp mirrors the lines into its in-memory console for the WebSocket.
- Some games buffer output when it isn't a real console, or don't write to stdout at all and log to a file instead (Enshrouded writes `logs\enshrouded_server.log`). The **log source** setting handles this: the host tails the file instead.
- Some games (notably Unreal ones like Palworld) start a child process and the launcher exits. The template marks the **real** process exe name, and the host tracks that one (found among the launcher's descendants), so a launcher exiting isn't taken for a crash, even while Basecamp is down.
- The host writes `host.json` with its control port, then waits for the game. When the game exits it writes `exit.json` (exit code, and whether the stop was asked for), tells any attached Basecamp, and exits itself. It never exits before its game.
- CPU and RAM per server come from `psutil`, sampled every 5 s.

### Host control channel

TCP on `127.0.0.1` only, on an ephemeral port (49152+, so it never collides with fixed app ports), JSON lines. Each connection starts with a `hello` carrying the per-run token (kept in the data folder, never in the repo) and the protocol version Basecamp speaks; the host answers with the version both will use, `min(Basecamp's, host's)`. Ops in protocol 1: `status`, `attach` (replay the buffer, then stream lines, "tracking" and "exit" events), `send` (a stdin line), `note` (a Basecamp line in the console), `stopping` (the coming exit is asked for), `ctrl_break`, `kill`. Protocol 2 adds `ctrl_c`; Basecamp doesn't send it to a protocol-1 host and uses the winctrl helper instead. Compatibility rules are in [07-updates.md](07-updates.md#host-processes-and-updates).

## Orphaned servers (Basecamp restarts, games keep going)

A game only stops when it's stopped from Basecamp, crashes, or the PC shuts down (R-04a). If Basecamp crashes, is updated or its task is ended, the hosts and games keep running: hosts are detached, outside Basecamp's job, and own the game's pipes. On start, Basecamp reads `state.json` and for each server:

1. **Host alive** (same PID **and** exe path **and** create time, so a reused PID isn't mistaken for it) and answering with the token: **reattach**. Status Running; the host replays its buffer, so the console shows output from before the restart and goes on live; stdin commands, Ctrl+Break and kill work as before.
2. **Run ended while Basecamp was down** (`exit.json` is there): report it now. **Crashed** with the exit code, unless the host recorded that a stop was asked for (then Stopped). Crash handling (R-05) and Discord then run as usual.
3. **Host gone, game still alive** (matched by PID + exe + create time): adopt the **bare** game. Status Running, CPU/RAM, RCON and file-based logs work; there's no stdin or stdout, so the console says "restart to get it back". Stop uses Ctrl+Break through the `winctrl` helper, then kill. The same happens if a host dies while Basecamp is running.
4. **Nothing left**: if the PC restarted since the run began, it's quietly Stopped (start-on-boot brings it back); otherwise it's reported as **Crashed** with an unknown exit code.

## SteamCMD

Built in Phase 3 (`basecamp/steam.py`).

- One runner thread, one queue (R-31). Command:
  `steamcmd.exe +force_install_dir "<install>" +login anonymous +app_update <appid> [-beta <branch>] [validate] +quit`
- **Update** (R-30, R-32), holding the server's action lock throughout (D-41):
  1. back up the save with `Backups.make(server, "pre-update")` (R-51). No save folder set, or not created yet: a console note and on. A backup that fails stops the update with nothing changed (D-42);
  2. the server's stop warning, if it has one (R-25, D-61): it keeps running while players are warned; cancelling here leaves it running;
  3. stop it with its own stop recipe (R-04a): Stopping, then **Updating**;
  4. run SteamCMD;
  5. start it again only if it was running (or starting) before. On failure it stays Stopped, the run is marked failed with SteamCMD's message and output, and the backup is kept.
- **Install** (R-11): a stopped server whose install folder is missing or empty. Same command, no backup, not started afterwards (D-46).
- SteamCMD's output goes into the server's console (shown in green), so the console WebSocket streams it; the phase and download percent go out on the status WebSocket (D-45). Each run also has its own log file.
- Success = exit code 0 and `Success! App '<appid>' fully installed` (or "already up to date"). SteamCMD sometimes updates itself first and exits (code 7, "Update complete, launching Steamcmd"); the runner runs it once more (D-44).
- Installed build ID is read from `<install>\steamapps\appmanifest_<appid>.acf` and shown on the cards and the server page. For a folder in a library's `steamapps\common` without one (SteamCMD run without `+force_install_dir`), the library's `steamapps\appmanifest_*.acf` whose `installdir` is that folder counts instead.
- **Default install folder** (D-125): a new Steam server's is `<folder of steamcmd.exe>\steamapps\common\<template's steam_installdir, else its id, else the server name>`. `GET /api/steam/library` lists that `common` folder's direct children (no links), each with the app ID whose manifest names it and the server already using it, so the Add form can import an existing install from a list. It takes no path. Without SteamCMD at `steamcmd_path`, there's no default and no list.
- **Build checks** (R-34, D-65) use the same queue: `steamcmd +login anonymous +app_info_update 1 +app_info_print <appid> +quit`, reading `depots > branches > <branch> > buildid` (branch = the server's beta branch, else `public`). Every `steam_check_h` (6) hours for installed Steam servers, and on **Check for update**. A check takes no lock and never makes the server busy. Different from the installed build = **update available**; a failed check = "unknown".
- Timeout 60 min per run (`steamcmd_timeout_s`); **Cancel update** takes a queued run off the queue, or kills the SteamCMD process tree (D-44).
- Every run is a row in `steam_runs` (who, result, builds before and after, backup). A failed or interrupted run marks the server until a later run succeeds; Start still works, after a confirm (D-43).
- The SteamCMD path is `steamcmd_path` in the data folder's `settings.toml` (D-47). Tests and the dev PC use `tools/fake_steamcmd.py`.

## Console commands: stdin, RCON and REST

Three channels, used by the console's command box, stop recipe steps and stop warnings (D-94):

- **stdin**: the host writes the command plus `\r\n` to the game's pipe. Works after a Basecamp restart too; not for a bare-adopted game whose host is gone, and not for games whose exe has no console (Windows GUI programs such as Dragonwilds' server).
- **RCON** (`basecamp/rcon.py`): Source RCON over TCP to `<rcon host>:<rcon port>`, by default `127.0.0.1`. V Rising, Palworld (deprecated there) and many others; not Satisfactory (its own HTTPS API, Later) or Valheim (no RCON without mods). One connection per server is kept and logged in once; a dead one is reconnected once; a timeout is never retried (D-92). Multi-packet answers are read until a packet that isn't full and a short quiet spell; packet id 0 answers count (Palworld, D-93).
- **REST** (`basecamp/rest.py`): Palworld's REST API, HTTP basic auth on `<rcon host>:<rest port>`, with commands written as lines (`announce <text>`, `save`, `shutdown <s> [text]`, `info`, ...; D-95).
- RCON and REST only talk to this PC unless `remote_console_allow_remote = true` (D-92). Each command has `remote_timeout_s` (5 s).
- Passwords are per server in `secrets.toml` (`[rcon.servers]`, `[rest.servers]`), set from the server form through `PUT /api/servers/{id}/remote`; the page only learns whether one is set (D-97).
- The command box offers the channels the server has (a port and a password), defaulting to RCON, else REST, else stdin. The command and its answer show in the console.
- A failed RCON or REST step or warning is logged and noted in the console with the reason, and the stop goes on with the next step at once: it never blocks a stop (D-96).

## API

Built in Phases 2 to 5: everything here except `/api/events`.

| Method | Path | Who |
|---|---|---|
| GET | `/api/me` | everyone (who you are) |
| GET | `/api/servers` · `/api/servers/{id}` | friend (trimmed fields, D-30) / admin (full) |
| POST | `/api/servers` · PUT/DELETE `/api/servers/{id}` | admin |
| POST | `/api/servers/{id}/start` · `/stop` · `/restart` · `/kill` · `/update` · `/install` | admin (stop/restart/kill answer 202, D-32; update/install answer 202 with the run id and queue; start/stop/restart answer 409 while an install or update runs, D-41, or a stop warning counts down, D-62). `/stop?now=true` skips the warning, and during a countdown skips the rest of it |
| POST | `/api/servers/{id}/steam/cancel` · `/steam/check` (build check, 202) · GET `/api/servers/{id}/steam/runs/{run}/log` · GET `/api/steam` (SteamCMD path and queue) | admin |
| POST | `/api/servers/{id}/command` (`{text, channel}`: `stdin`, `rcon`, `rest`, or empty for the default) · GET `/api/servers/{id}/console?after=` | admin |
| PUT | `/api/servers/{id}/remote` (`rcon_password`, `rest_password`; the answer and the server view say only which are set, D-97) | admin |
| WS | `/ws/servers/{id}/console?after=` | admin (replay + live, send `{"text": ..., "channel": ...}`) |
| WS | `/ws/status` | everyone (status changes; friends get trimmed fields) |
| GET | `/api/servers/{id}/configs` (the list, with any guard refusal per entry) · GET/PUT `/api/servers/{id}/configs/{n}` (read: text, encoding, hash, copies; save: `{text, base_sha256, force}`, 400 bad JSON, 403 guard, 409 changed on disk) | admin |
| GET | `/api/servers/{id}/configs/{n}/copies/{name}` · POST `.../copies/{name}/restore` | admin |
| GET/POST | `/api/servers/{id}/backups` (list, back up now) · POST `/backups/{b}/restore` · DELETE `/backups/{b}` · GET `/backups/{b}/download` (the zip, streamed, D-64) | admin (restore: 409 unless stopped) |
| GET/PUT | `/api/notify` (main webhook, masked) · PUT `/api/servers/{id}/webhook` · POST `/api/notify/test` | admin |
| GET | `/api/templates` · POST `/api/drafts` (pre-filled form from a template or an install's appmanifest; no install folder = SteamCMD's default for the template, D-125) · GET `/api/steam/library` (folders in SteamCMD's `steamapps\common`) · POST `/api/templates` (`{text, replace, dry_run}`: add a template to the data folder, 400 with the reason if invalid, 409 to confirm an override or replace, D-129) · DELETE `/api/templates/{id}` (data folder only) | admin |
| GET/PUT | `/api/settings` (join host, SteamCMD path, backup defaults, `apply_backup_defaults`, `discord_live_status`; the masked Discord info and the live status message's state; read-only admin logins; `file`: the last settings.toml error and the changes waiting for a restart) · POST `/api/settings/reload` (read settings.toml now, D-127) | admin |
| GET | `/api/firewall` · POST `/api/firewall/check` (each server's ports against Windows Firewall's inbound rules, read-only, cached 60 s; check = read again, D-128) | admin |
| POST | `/api/restart` (Restart Basecamp through the launcher, 202; 409 without the launcher or while an install or update is queued or running, D-126) | admin |
| GET | `/api/events` · `/api/audit` | admin |
| GET | `/healthz` | localhost |

## UI

Two pages, both phone-first (one column, big buttons, works at 375 px wide):

- **Dashboard** (admin): a card per server: name, status pill, uptime, CPU/RAM, join address with **Copy** and **Copy for Discord**, buttons Start / Stop / Restart / Update. Tap a card for the server page: **Console**, **Config files**, **Firewall** (per port: open (Tailscale), too broad, blocked, missing with the command to add the rule, or unknown; **Check again**), **Backups** (back up now, restore, delete), **History**. The card has a **Firewall** badge with the worst port's status (D-128). A pending crash restart shows its time, with **Cancel restart**. A config file opens in an editor page: Save (JSON checked), Reload, wrap view, fields view for Palworld's `OptionSettings` line, **Restart now** after a save while running, earlier copies with View and Restore. A **Settings** page holds the join host, backup defaults, SteamCMD path and the Discord webhook (per-server webhooks under Advanced) with the live status message switch. A stop warning counting down shows on the card with the time left and **Stop now**; an **update available** (or "update: unknown") tag sits next to the status pill, and the server page's SteamCMD section has **Check for update**. Backups have **Download**.
- **Friend view**: the same cards with only name, status, join address + Copy.
- Confirm dialogs on Stop, Update and Restore. Live updates over the status WebSocket, no refreshing.
- Built with plain HTML/CSS/JS, no library and no CDN (D-34).
- Both pages link a web app manifest (`/static/manifest.webmanifest`) and icons, so "Add to home screen" opens Basecamp full screen like an app (R-72, D-66).

## Testing on the dev PC

- `tools/fake_server.py` acts like a game server: prints lines, answers `save`/`quit` on stdin, binds a UDP port, and can be told to crash or hang. Tests run the real supervisor against it.
- `tools/fake_rcon.py` and `tools/fake_rest.py` stand in for a game's RCON and REST API in tests (and by hand): wrong password, slow answers, dropped connections, multi-packet and id-0 answers.
- The real game servers were run under Basecamp on the dev PC once, to check the templates (D-98); tests never run them.
- SteamCMD runner tested with a fake `steamcmd.exe` (a script printing recorded output) plus one manual real run with a small free dedicated server.
- Dev server on `127.0.0.1:8150`, data in `.\data\`.
