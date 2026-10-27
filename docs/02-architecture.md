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
- A login in `admin_logins` (data-folder `settings.toml`) is admin; any other login is a friend.
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
  winctrl.py         fallback Ctrl+Break helper (bare adopted games)
  console.py         in-memory console per server (mirrors the host's lines), subscribers
  rcon.py            Source RCON client (Palworld, Satisfactory-style, many others)
  steam.py           SteamCMD runner, appmanifest parser, update queue
  backups.py         zip save folders, schedule, retention, restore (D-35, D-36)
  configs.py         read/write allowed config files, versioned copies
  notify.py          Discord webhook: secrets.toml, queue, retry (D-37, D-38)
  auth.py            who is asking: admin or friend (D-28, D-29)
  templates.py       game templates (built-in + data folder), appmanifest import
  api/app.py         FastAPI app: REST routes, status and console WebSockets, pages
  api/views.py       admin view vs friend view of a server (D-30)
  web/               index.html, friend.html, app.js, common.js, style.css (no build step)
migrations/          numbered SQL files, add-only
templates/           built-in game templates, one JSON per game (docs/08-templates.md)
tools/fake_server.py fake game server for tests: prints, reads stdin, binds a UDP port, can "crash"
tests/
scripts/             release.ps1 (dev PC), install / uninstall PowerShell for the prod PC
CHANGES.md           release notes, one section per version
```

## Data

All under `C:\BasecampData\` (dev PC: `.\data\`):

| Path | What |
|---|---|
| `basecamp.db` | SQLite: servers, settings, event history, audit log |
| `secrets.toml` | Discord webhook URLs (D-37), later RCON passwords. Kept out of the repo |
| `boot.json` | The PC boot time Basecamp last started under, for the "started after a restart" message |
| `logs\basecamp.log` | The app's own log, rotated |
| `logs\servers\<id>\console.log` | Each server's output, rotated (10 MB × 5) |
| `backups\<id>\` | Save backups (`YYYYMMDD-HHMMSS-<reason>.zip`, D-35). `backups_root` in settings.toml can put them elsewhere |
| `config-history\<id>\` | Old copies of config files saved from the editor |
| `db-backups\` | Copy of `basecamp.db` taken before each self-update, for rollback |
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
- The host starts the game with `Popen(stdin=PIPE, stdout=PIPE, stderr=STDOUT, creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)`. The game gets its own hidden console, so **Ctrl+Break** reaches just that server: the host attaches to that console and raises the event.
- The host pushes each output line into its ring buffer, the rotated `console.log`, and any attached Basecamp. Basecamp mirrors the lines into its in-memory console for the WebSocket.
- Some games buffer output when it isn't a real console, or don't write to stdout at all and log to a file instead (Enshrouded writes `logs\enshrouded_server.log`). The **log source** setting handles this: the host tails the file instead.
- Some games (notably Unreal ones like Palworld) start a child process and the launcher exits. The template marks the **real** process exe name, and the host tracks that one (found among the launcher's descendants), so a launcher exiting isn't taken for a crash, even while Basecamp is down.
- The host writes `host.json` with its control port, then waits for the game. When the game exits it writes `exit.json` (exit code, and whether the stop was asked for), tells any attached Basecamp, and exits itself. It never exits before its game.
- CPU and RAM per server come from `psutil`, sampled every 5 s.

### Host control channel

TCP on `127.0.0.1` only, on an ephemeral port (49152+, so it never collides with fixed app ports), JSON lines. Each connection starts with a `hello` carrying the per-run token (kept in the data folder, never in the repo) and the protocol version Basecamp speaks; the host answers with the version both will use, `min(Basecamp's, host's)`. Ops in protocol 1: `status`, `attach` (replay the buffer, then stream lines, "tracking" and "exit" events), `send` (a stdin line), `note` (a Basecamp line in the console), `stopping` (the coming exit is asked for), `ctrl_break`, `kill`. Compatibility rules are in [07-updates.md](07-updates.md#host-processes-and-updates).

## Orphaned servers (Basecamp restarts, games keep going)

A game only stops when it's stopped from Basecamp, crashes, or the PC shuts down (R-04a). If Basecamp crashes, is updated or its task is ended, the hosts and games keep running: hosts are detached, outside Basecamp's job, and own the game's pipes. On start, Basecamp reads `state.json` and for each server:

1. **Host alive** (same PID **and** exe path **and** create time, so a reused PID isn't mistaken for it) and answering with the token: **reattach**. Status Running; the host replays its buffer, so the console shows output from before the restart and goes on live; stdin commands, Ctrl+Break and kill work as before.
2. **Run ended while Basecamp was down** (`exit.json` is there): report it now. **Crashed** with the exit code, unless the host recorded that a stop was asked for (then Stopped). Crash handling (R-05) and Discord then run as usual.
3. **Host gone, game still alive** (matched by PID + exe + create time): adopt the **bare** game. Status Running, CPU/RAM, RCON and file-based logs work; there's no stdin or stdout, so the console says "restart to get it back". Stop uses Ctrl+Break through the `winctrl` helper, then kill. The same happens if a host dies while Basecamp is running.
4. **Nothing left**: if the PC restarted since the run began, it's quietly Stopped (start-on-boot brings it back); otherwise it's reported as **Crashed** with an unknown exit code.

## SteamCMD

- One runner, one queue. Command:
  `steamcmd.exe +force_install_dir "<install>" +login anonymous +app_update <appid> [-beta <branch>] [validate] +quit`
- Output streams to the page. Success = exit code 0 and `Success! App '<appid>' fully installed` (or "already up to date"). SteamCMD sometimes updates itself first and exits; the runner retries once if that happens.
- Installed build ID is read from `<install>\steamapps\appmanifest_<appid>.acf` and shown on the server page (the update-available check is **Later**).
- Timeout 60 min; cancel button kills the SteamCMD process tree.

## RCON and stdin commands

- **stdin**: the host writes the command plus `\r\n` to the game's pipe. Works after a Basecamp restart too; not for a bare-adopted game whose host is gone.
- **RCON**: Source RCON protocol over TCP to `127.0.0.1:<rcon port>`. Works for Palworld and many Source/Unreal-based servers. Not for Satisfactory (its own HTTPS API, Later) or Valheim (no RCON without mods). The console's command box sends via RCON if configured, else stdin.
- Stop recipes use the same two channels.

## API

Built in Phases 2 and 4: everything here except `/update`, `/install`, configs, settings and events, which come with their phases.

| Method | Path | Who |
|---|---|---|
| GET | `/api/me` | everyone (who you are) |
| GET | `/api/servers` · `/api/servers/{id}` | friend (trimmed fields, D-30) / admin (full) |
| POST | `/api/servers` · PUT/DELETE `/api/servers/{id}` | admin |
| POST | `/api/servers/{id}/start` · `/stop` · `/restart` · `/kill` · `/update` · `/install` | admin (stop/restart/kill answer 202, D-32) |
| POST | `/api/servers/{id}/command` · GET `/api/servers/{id}/console?after=` | admin |
| WS | `/ws/servers/{id}/console?after=` | admin (replay + live, send `{"text": ...}`) |
| WS | `/ws/status` | everyone (status changes; friends get trimmed fields) |
| GET/PUT | `/api/servers/{id}/configs/{n}` | admin |
| GET/POST | `/api/servers/{id}/backups` (list, back up now) · POST `/backups/{b}/restore` · DELETE `/backups/{b}` | admin (restore: 409 unless stopped) |
| GET/PUT | `/api/notify` (main webhook, masked) · PUT `/api/servers/{id}/webhook` · POST `/api/notify/test` | admin |
| GET | `/api/templates` · POST `/api/drafts` (pre-filled form from a template or an install's appmanifest) | admin |
| GET/PUT | `/api/settings` | admin |
| GET | `/api/events` · `/api/audit` | admin |
| GET | `/healthz` | localhost |

## UI

Two pages, both phone-first (one column, big buttons, works at 375 px wide):

- **Dashboard** (admin): a card per server: name, status pill, uptime, CPU/RAM, join address with **Copy** and **Copy for Discord**, buttons Start / Stop / Restart / Update. Tap a card for the server page: **Console**, **Backups** (back up now, restore, delete), **History**; later **Config files**. A pending crash restart shows its time, with **Cancel restart**. A **Discord** page sets the webhooks.
- **Friend view**: the same cards with only name, status, join address + Copy.
- Confirm dialogs on Stop, Update and Restore. Live updates over the status WebSocket, no refreshing.
- Built with plain HTML/CSS/JS, no library and no CDN (D-34).

## Testing on the dev PC

- `tools/fake_server.py` acts like a game server: prints lines, answers `save`/`quit` on stdin, speaks minimal RCON, binds a UDP port, and can be told to crash or hang. Tests run the real supervisor against it.
- SteamCMD runner tested with a fake `steamcmd.exe` (a script printing recorded output) plus one manual real run with a small free dedicated server.
- Dev server on `127.0.0.1:8150`, data in `.\data\`.
