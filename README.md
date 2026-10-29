# Basecamp

Home base for the game servers you host for friends.

Basecamp is a small Windows app that runs on an always-on Windows PC (the "host PC"). It starts when the PC boots, before anyone logs in, and brings back up every game server you've marked "start on boot". You control it from a web page that works on desktop and phone, over Tailscale.

**Status:** Phases 1 to 4 built and tested on the dev PC: the core supervisor, the web API and the admin and friend pages, templates for Palworld, Enshrouded, V Rising, RuneScape: Dragonwilds and Windrose, SteamCMD installs and one-click updates, and save backups, crash auto-restart and Discord messages. Phase 5's config editor and settings page are built too; checking the game templates on the host PC is still open. Phase 6's self-update from GitHub Releases (launcher, signed releases, automatic rollback) is built and tested end to end against a local fake release source; the first real release waits for the signing key. Phase 6.5 added warnings to players before a stop, a live status message in Discord, backup downloads, an update-available badge and add-to-home-screen. Each running server has its own small host process, so games and their consoles carry on through Basecamp restarts and updates. See [docs/05-plan.md](docs/05-plan.md).

## What it does (v1)

- Keeps a list of game servers: Steam ones (installed and updated through SteamCMD) and non-Steam ones (any exe).
- Starts at boot, not at login, and starts every server marked **start on boot**. Several servers can run at once.
- Start, stop, restart from the web page. Stop is graceful when the game supports it (save, then quit), and can warn players a few minutes ahead, with a countdown and **Stop now**.
- **One-click update** for Steam servers: stop, back up the save, run SteamCMD, start again. An **update available** badge when Steam has a newer build.
- **Adding a game**: **Add server**, pick the game's template (the install folder defaults to SteamCMD's `steamapps\common\<game folder>`), or pick an existing install from the list of folders already there. A game without a template: leave it on **Detect** for an existing install, or fill in the form by hand with type **steam** and the Steam **App ID** of its dedicated server (SteamDB lists them; it's not the game's own ID). Then **Install** on the server's page downloads it with SteamCMD.
- **Live console**: watch server output and type commands into it (stdin, RCON, or Palworld's REST API). Stops and warnings to players use the same channels.
- **Copy join address** button: one click copies `<tailscale-ip>:<port>` (or a nicer line for Discord).
- **Config file editor**: each server lists its config files (JSON, INI, etc.); edit them in the page, with a backup kept on every save.
- **Crash auto-restart**, with a limit so a broken server doesn't loop forever.
- **Save backups**: zip each server's save folder on a schedule and before every update; keep the last N; download one from the page.
- **Discord webhook**: posts when a server comes up, goes down, crashes or updates, with the join address. Optionally keeps one live status message, edited in place.
- **Updates itself** from GitHub Releases: banner when a new version is out, one tap to install, automatic rollback if it fails.
- **Two views**: the admin view (you, full control) and a read-only **friend view** (what's running, join address, status). Both can be added to a phone's home screen.

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
- The join host, SteamCMD path and backup defaults are on the page's **Settings** screen (saved to `.\data\settings.toml`). Admin logins and the rest are set in that file by hand:

```toml
[basecamp]
join_host = "<tailscale-ip>"
admin_logins = ["you@example.com"]
# backups_root = "D:\\BasecampBackups"   # default: .\data\backups
```

- SteamCMD's path is `steamcmd_path` in `.\data\settings.toml` (default `%USERPROFILE%\Desktop\SteamCmd\steamcmd.exe`). To try installs and updates without downloading anything, point it at the fake: `steamcmd_path = '<repo>\tools\fake_steamcmd.py'`. Set a server's beta branch to `fail`, `hang`, `selfupdate` or `uptodate` to see those cases. For **Check for update** the fake says Steam has build 1001 (`FAKE_STEAMCMD_LATEST` changes it); the `fail` branch checks as "unknown".
- The Discord webhook is set on the page's **Settings** screen and kept in `.\data\secrets.toml`, never in the repo. To try messages without Discord, run `tools\fake_discord.py` (it prints each post and edit, and handles the live status message) and point the webhook at it by editing `secrets.toml` (the page only accepts discord.com URLs):

```toml
[discord]
webhook_url = "http://127.0.0.1:8159/api/webhooks/1/test"
```

- Self-update: `python -m basecamp` checks for releases but can't install them (no launcher). To try the whole flow, `tools\e2e_selfupdate.py --root C:\BasecampTest --work <scratch folder>` builds test releases with a throwaway key, serves them from [tools/fake_releases.py](tools/fake_releases.py) and runs them under the launcher ([docs/07-updates.md](docs/07-updates.md)). Releases are made with `scripts\release.ps1 <version>` (owner only; it publishes to GitHub after asking).

- RCON and REST: set a server's RCON or REST port and password on its form (the password goes to `.\data\secrets.toml`). To try them without a game, run `tools\fake_rcon.py` or `tools\fake_rest.py` (`--mode slow`, `drop`, ... for the bad cases) and point the server's port at it.

Tests run real processes against [tools/fake_server.py](tools/fake_server.py) and [tools/fake_steamcmd.py](tools/fake_steamcmd.py), [tools/fake_rcon.py](tools/fake_rcon.py) and [tools/fake_rest.py](tools/fake_rest.py), never real games or SteamCMD.
