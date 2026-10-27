# Basecamp

Home base for the game servers you host for friends.

Basecamp is a small Windows app that runs on an always-on Windows PC (the "host PC"). It starts when the PC boots, before anyone logs in, and brings back up every game server you've marked "start on boot". You control it from a web page that works on desktop and phone, over Tailscale.

**Status:** Phases 1, 2 and 4 built and tested on the dev PC: the core supervisor, the web API and the admin and friend pages, templates for Palworld, Enshrouded, V Rising, RuneScape: Dragonwilds and Windrose, and save backups, crash auto-restart and Discord messages. Phase 3 (SteamCMD) is next. Each running server has its own small host process, so games and their consoles carry on through Basecamp restarts and updates. See [docs/05-plan.md](docs/05-plan.md).

## What it does (v1)

- Keeps a list of game servers: Steam ones (installed and updated through SteamCMD) and non-Steam ones (any exe).
- Starts at boot, not at login, and starts every server marked **start on boot**. Several servers can run at once.
- Start, stop, restart from the web page. Stop is graceful when the game supports it (save, then quit).
- **One-click update** for Steam servers: stop, back up the save, run SteamCMD, start again.
- **Live console**: watch server output and type commands into it (stdin, or RCON for games that use it).
- **Copy join address** button: one click copies `<tailscale-ip>:<port>` (or a nicer line for Discord).
- **Config file editor**: each server lists its config files (JSON, INI, etc.); edit them in the page, with a backup kept on every save.
- **Crash auto-restart**, with a limit so a broken server doesn't loop forever.
- **Save backups**: zip each server's save folder on a schedule and before every update; keep the last N.
- **Discord webhook**: posts when a server comes up, goes down, crashes or updates, with the join address.
- **Updates itself** from GitHub Releases: banner when a new version is out, one tap to install, automatic rollback if it fails.
- **Two views**: the admin view (you, full control) and a read-only **friend view** (what's running, join address, status).

## Docs

| File | What's in it |
|---|---|
| [docs/01-requirements.md](docs/01-requirements.md) | What the app must do, in detail, and what's out of scope for v1 |
| [docs/02-architecture.md](docs/02-architecture.md) | How it's built: process model, data, API, UI, SteamCMD, consoles |
| [docs/03-friend-access.md](docs/03-friend-access.md) | The Tailscale user-limit problem and the options for how friends connect |
| [docs/04-deployment.md](docs/04-deployment.md) | Installing on the host PC |
| [docs/05-plan.md](docs/05-plan.md) | Development plan, phase by phase, with done-criteria |
| [docs/06-decisions.md](docs/06-decisions.md) | Decisions made so far, and open questions |
| [docs/07-updates.md](docs/07-updates.md) | How Basecamp updates itself from GitHub Releases, with signing and rollback |
| [docs/08-templates.md](docs/08-templates.md) | Adding a game: the template format, finding the facts, testing and shipping it |

## Stack

Python 3.13, FastAPI + Uvicorn, SQLite, a plain HTML/JS front end (no build step). Runs as your own Windows account from a scheduled task at startup, using its own Python copy under `C:\Basecamp\` (see [docs/04-deployment.md](docs/04-deployment.md) for why).

## Development (dev PC)

```powershell
uv python install 3.13
uv venv --python 3.13 .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check .
```

`python -m basecamp` runs Basecamp with data in `.\data\` (or `BASECAMP_DATA_DIR`). It adopts servers left running, starts the start-on-boot ones, and serves the page on http://127.0.0.1:8150. From the PC itself you're the admin.

- `tools\dev_template.py` adds a "Fake server (dev)" template so you can try the page without a real game.
- Set the join address and admin logins in `.\data\settings.toml`:

```toml
[basecamp]
join_host = "<tailscale-ip>"
admin_logins = ["you@example.com"]
# backups_root = "D:\\BasecampBackups"   # default: .\data\backups
```

- The Discord webhook is set on the page's **Discord** screen and kept in `.\data\secrets.toml`, never in the repo. To try messages without Discord, point it at a local endpoint by editing `secrets.toml` (the page only accepts discord.com URLs):

```toml
[discord]
webhook_url = "http://127.0.0.1:<port>/api/webhooks/1/test"
```

Tests run real processes against [tools/fake_server.py](tools/fake_server.py), never real games.
