# 05 · Development plan

Build and test on the dev PC against `tools/fake_server.py`; deploy to the prod PC only in Phase 7, step by step with you. After that, updates ship as GitHub Releases. Each phase ends with something usable and its tests passing.

## Phase 0 · Checks before writing code (half a day)

Things that could change the design. Do these first, mostly by hand on the prod PC.

- [ ] **Tailscale serve + identity**: on the prod PC run `tailscale serve --bg --https=443 http://127.0.0.1:8150` against a 10-line test app that echoes request headers. From your phone: does the page load over HTTPS, and is `Tailscale-User-Login` present? From a friend's device too? → decides Plan A vs Plan B ([02-architecture.md](02-architecture.md)).
- [ ] **Tailnet policy**: which ports friends can reach on the prod PC today; would 443 need adding.
- [ ] **Headless game start**: start Palworld and Enshrouded from a scheduled task as your account with "Do not store password", reboot, don't log in. Do they run? Does output come through the pipe or only a log file? Does Ctrl+Break make them save and quit? If anything fails only in this mode, retry with the password stored.
- [ ] **Games survive Basecamp going away** (Q-09, Q-10): start a game through a Basecamp host from the test task, end the task and kill Basecamp; do the host and game keep running? (Low priority: kill the host too; does the game survive?)
- [ ] **SteamCMD from that same task**: anonymous `app_update` works, output captured.
- [ ] **Save locations** of every game you run today (for backups; list them in the templates).
- [ ] **Friend access**: count Tailscale users; test device sharing with one friend ([03-friend-access.md](03-friend-access.md)).
- [ ] `git init` with a `.gitignore` covering `data/`, `secrets.toml`, `*.key`; public repo on GitHub.

**Done when:** each box has an answer written into [06-decisions.md](06-decisions.md).

## Phase 1 · Core: servers and processes (no UI)

- Project skeleton: `pyproject.toml`, ruff, pytest, `basecamp/` package, settings, logging.
- SQLite store and models for servers, ports, stop recipes (R-10..R-12).
- `tools/fake_server.py`.
- Supervisor: start, graceful stop with recipes, kill, restart; state machine; per-server lock (R-20..R-22).
- Console capture: ring buffer, rotated log file, stdin send, log-file tailing (R-23).
- Real-exe tracking for launcher games (psutil children).
- Port-in-use check and low-RAM warning before start (R-03).
- `state.json` + orphan adoption on start (R-04).
- Start-on-boot servers launched in sequence at app start (R-02).

**Done when:** pytest starts/stops/restarts fake servers, kills a hung one, adopts one after Basecamp is killed, and refuses a duplicate port.

**Status:** built on the dev PC (2026-10-26), including the per-server host process (R-04a). Decisions D-16..D-26 in [06-decisions.md](06-decisions.md).

## Phase 2 · Web API and admin UI

- FastAPI routers for servers and actions; status WebSocket; console WebSocket (R-20, R-23).
- Auth module: Plan A (identity header + localhost) or Plan B (password + session), per Phase 0 (R-80).
- Admin dashboard and server page, phone-first: cards, status pills, Start/Stop/Restart, live console with command box.
- Add/edit server form; game templates for Palworld and Enshrouded first (R-13); import existing install (R-15).
- **Copy join address** and **Copy for Discord** buttons (R-24).
- Audit log (R-83).

**Done when:** from a phone browser on the dev PC's tailnet address you can add a fake server, start it, watch its console, send a command, copy its address, stop it.

## Phase 3 · SteamCMD updates and installs

- SteamCMD runner with queue, streaming output, timeout, cancel, self-update retry (R-31).
- Update flow: backup → stop → update → start if it was running; failure handling (R-30, R-32).
- First install into an empty folder (R-11).
- Show installed build ID from the appmanifest.

**Done when:** tests pass with a fake steamcmd; one real install + update of a small dedicated server works on the dev PC.

## Phase 4 · Backups, crash restart, Discord, friend view

- Backups: zip save folder, schedule, retention, restore with safety backup (R-50..R-52).
- Crash auto-restart with the 3-in-10-minutes limit (R-05).
- Discord webhook notifier with per-server event toggles and retry (R-60..R-62).
- Friend view page and trimmed API fields (R-70, R-71).

**Done when:** a fake server that crashes is restarted twice then marked Crashed with a Discord message; a backup is made, listed and restored; the friend view shows no admin data (test checks the JSON).

## Phase 5 · Config editor and templates

- Config files tab: open, edit, JSON check, versioned copies, "restart now" (R-40..R-43). Path guard test: can't read anything outside the server's list.
- Templates for the rest of the games you run (Valheim, Satisfactory, etc.), with save locations from Phase 0.
- Settings page: Discord webhook, backup defaults, Tailscale IP / join host.

**Done when:** you can change a Palworld setting from the phone, restart, and see it take effect.

## Phase 6 · Self-update from GitHub Releases

See [07-updates.md](07-updates.md).

- `launcher.py`: run current version, swap on exit code 3, health check, rollback, crash back-off, prune old versions.
- Migrations runner and pre-update DB backup.
- `scripts/release.ps1`: checks, version bump, tag, build zip with vendored deps, manifest, Ed25519 signature, `gh release create`.
- `selfupdate.py`: poll with ETag, beta channel, download, verify, unpack, banner + Discord, Install button, optional auto-install hour (R-90..R-94).
- Make the signing key on the dev PC and back it up.

**Done when:** on the dev PC, a local "prod-like" folder (`C:\BasecampTest\`) updates from a real release v0.1.0 → v0.2.0 with a fake server still running and adopted; a deliberately broken v0.3.0 (fails `/healthz`) rolls back to v0.2.0 by itself; a zip with a bad signature is refused.

## Phase 7 · Prod PC install (with you, one step at a time)

- `scripts/install.ps1`, `uninstall.ps1`, `add-game-firewall-rule.ps1` ([04-deployment.md](04-deployment.md)). Install pulls the latest release.
- Watchdog task (R-06).
- Import the first game (whichever is least risky) in place.
- `tailscale serve` set up.
- Reboot test without logging in.
- Update the owner's private network notes for the host PC.
- Import the remaining games one at a time.

**Done when:** after a cold reboot with nobody logged in, the on-boot servers are up, Discord got "Basecamp started", and the verify commands show nothing else on the PC changed.

## After v1

The Later list in [01-requirements.md](01-requirements.md), roughly in this order: player counts → update-available badge + scheduled updates → scheduled restarts → playit.gg join addresses (if chosen) → custom update commands → backups off the PC.

## Rough size

Phases 1-6 are around 3,500-4,500 lines of Python plus ~1,000 of HTML/JS. Phases 1, 3 and 6 hold most of the risk (Windows process handling, SteamCMD quirks, self-update and rollback); the UI is straightforward.
