# 05 · Development plan

Build and test on the dev PC against `tools/fake_server.py`; deploy to the prod PC only in Phase 7, step by step with you. After that, updates ship as GitHub Releases. Each phase ends with something usable and its tests passing.

## Phase 0 · Checks before writing code (half a day)

Things that could change the design. Do these first, mostly by hand on the prod PC.

- [x] **Tailscale serve + identity** (2026-10-26: works from your phone and both PCs → Plan A, D-27; friend device still to check): on the prod PC run `tailscale serve --bg --https=443 http://127.0.0.1:8150` against a 10-line test app that echoes request headers. From your phone: does the page load over HTTPS, and is `Tailscale-User-Login` present? From a friend's device too? → decides Plan A vs Plan B ([02-architecture.md](02-architecture.md)).
- [ ] **Tailnet policy**: which ports friends can reach on the prod PC today; would 443 need adding.
- [ ] **Headless game start**: start Palworld and Enshrouded from a scheduled task as your account with "Do not store password", reboot, don't log in. Do they run? Does output come through the pipe or only a log file? Does Ctrl+Break make them save and quit? If anything fails only in this mode, retry with the password stored.
- [ ] **Games survive Basecamp going away** (Q-09, Q-10): start a game through a Basecamp host from the test task, end the task and kill Basecamp; do the host and game keep running? (Low priority: kill the host too; does the game survive?) Dev PC, 2026-10-28, fake game under the real task: yes to all, after one fix (D-110); confirm on prod. The fake game also ran headless and stopped by Ctrl+Break under S4U (Q-04); real games still to try.
- [ ] **SteamCMD from that same task**: anonymous `app_update` works, output captured. Dev PC, 2026-10-28: an anonymous `app_info_print` from Basecamp under the task worked (no download tried).
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
- Auth module: Plan A (identity header + localhost, D-27). Admin logins are a setting (R-80).
- Admin dashboard and server page, phone-first: cards, status pills, Start/Stop/Restart, live console with command box.
- Add/edit server form; game templates for Palworld and Enshrouded first (R-13); import existing install (R-15).
- **Copy join address** and **Copy for Discord** buttons (R-24).
- Audit log (R-83).

**Done when:** from a phone browser on the dev PC's tailnet address you can add a fake server, start it, watch its console, send a command, copy its address, stop it.

**Status:** built on the dev PC (2026-10-26) and checked in a browser at `127.0.0.1:8150` (add from template, start, console, command, copy, stop). The tailnet-address check waits for `tailscale serve` (Phase 7, Q-11). Templates for all five of the owner's games, with unchecked fields marked ([08-templates.md](08-templates.md)). Decisions D-28..D-34.

## Phase 3 · SteamCMD updates and installs

- SteamCMD runner with queue, streaming output, timeout, cancel, self-update retry (R-31).
- Update flow: backup → stop → update → start if it was running; failure handling (R-30, R-32).
- First install into an empty folder (R-11).
- Show installed build ID from the appmanifest.

**Done when:** tests pass with a fake steamcmd; one real install + update of a small dedicated server works on the dev PC.

**Status:** built on the dev PC (2026-10-27) and tested with `tools/fake_steamcmd.py`; checked in a browser at `127.0.0.1:8150`: install into an empty folder, update of a running server (backup, stop, SteamCMD, started again, build 1000 → 1001 → 1002), a failed update (left stopped and marked, backup kept, output link), cancel during a hung SteamCMD, the self-update retry, phone width. Real SteamCMD on the dev PC (2026-10-27): a fresh SteamCMD updated itself first (exit 7, retried as D-44 says), then installed the Valheim dedicated server (app 896660, 2.1 GB, build 25909431) into an empty folder in under a minute, with download progress shown live; an update afterwards said "already up to date". Not seen yet: a real update that downloads a new build. Decisions D-41..D-47.

## Phase 4 · Backups, crash restart, Discord, friend view

- Backups: zip save folder, schedule, retention, restore with safety backup (R-50..R-52).
- Crash auto-restart with the 3-in-10-minutes limit (R-05).
- Discord webhook notifier with per-server event toggles and retry (R-60..R-62).
- Friend view page and trimmed API fields (R-70, R-71).

**Done when:** a fake server that crashes is restarted twice then marked Crashed with a Discord message; a backup is made, listed and restored; the friend view shows no admin data (test checks the JSON).

**Status:** built on the dev PC (2026-10-27), before Phase 3 (D-40), and checked in a browser at `127.0.0.1:8150` against a local fake webhook. Checked: the edit form's backup and Discord fields, back up now, a scheduled backup, restore with its safety backup, three crashes (two restarts, then Crashed, with the Discord messages), the Discord page and its test message, phone width. The friend view and its trimmed fields were done in Phase 2. Not tried: a real Discord channel. Decisions D-35..D-40.

## Phase 5 · Config editor and templates

- Config files tab: open, edit, JSON check, versioned copies, "restart now" (R-40..R-43). Path guard test: can't read anything outside the server's list.
- Check the built-in templates' `unverified` fields on the prod PC; templates for any other games, with save locations from Phase 0.
- Settings page: Discord webhook, backup defaults, Tailscale IP / join host.

**Done when:** you can change a Palworld setting from the phone, restart, and see it take effect.

**Status:** config editor and settings page built on the dev PC (2026-10-27), 173 tests, and checked in a browser at `127.0.0.1:8150` against `tools/fake_server.py` with fake config files: open a JSON file and a Palworld-style `PalWorldSettings.ini` (UTF-16 with a BOM, CRLF, one long `OptionSettings` line), edit it as text and as fields, a bad JSON save refused, a save while running offering **Restart now** (restarted through the normal path), restore of an older copy, the settings page saving to `settings.toml` and applying at once, phone width. Saved files kept their encoding, BOM and line endings byte for byte. **Not done:** the done-when check itself (a Palworld setting from the phone) needs the prod PC and the `tailscale serve` address (Phase 7). The template fields still marked `unverified` need the prod PC too; the owner has a read-only checklist for that in `private/`. Not tried: a symlinked config file (the dev PC can't make symlinks without Developer Mode; junctions are tested). Decisions D-48..D-52.

## Phase 6 · Self-update from GitHub Releases

See [07-updates.md](07-updates.md).

- `launcher.py`: run current version, swap on exit code 3, health check, rollback, crash back-off, prune old versions.
- Migrations runner and pre-update DB backup.
- `scripts/release.ps1`: checks, version bump, tag, build zip with vendored deps, manifest, Ed25519 signature, `gh release create`.
- `selfupdate.py`: poll with ETag, beta channel, download, verify, unpack, banner + Discord, Install button, optional auto-install hour (R-90..R-94).
- Make the signing key on the dev PC and back it up (owner: `scripts\new-signing-key.ps1`).

**Done when:** on the dev PC, a local "prod-like" folder (`C:\BasecampTest\`) updates from a real release v0.1.0 → v0.2.0 with a fake server still running and adopted; a deliberately broken v0.3.0 (fails `/healthz`) rolls back to v0.2.0 by itself; a zip with a bad signature is refused.

**Status:** built on the dev PC (2026-10-27): `launcher.py`, `basecamp/selfupdate.py`, `scripts/build_release.py`, `release.ps1`, `new-signing-key.ps1`, the dashboard banner and the Settings page's update section; 34 new tests, 207 in all (launcher swap, health check, rollback with the DB copy, back-off, pruning; signature, manifest, zip paths, migrations on a copy, ETag, beta channel, install hand-over, auto-install, API). The done-when ran against a local fake release source instead of GitHub (`tools/e2e_selfupdate.py`, throwaway key, real vendored dependencies, launcher on the base Python): v0.1.0 → v0.2.0 in about 2 s with the fake game keeping its PID and console and the new migration applied; a broken v0.3.0 (`/healthz` answers 500) rolled back to v0.2.0 and its DB copy by itself after 60 s with the game still running; a v0.4.0 with a bad signature was refused with nothing unpacked; 21/21 checks. The run also caught a bug, now fixed: the broken v0.3.0 announced its own update before its health check had failed (D-57). **Waiting on the owner:** making the real signing key (`scripts\new-signing-key.ps1`) and backing it up, and a go to publish a real v0.1.0 (and v0.2.0) on GitHub to prove that path. Not tried: GitHub's own redirects and rate limits, auto-install at its hour in a live run (unit-tested), a launcher started by Task Scheduler under `pythonw.exe` (Phase 7). Decisions D-53..D-60.

## Phase 6.5 · Small features before the prod install

Picked by the owner after Phase 6 (a disk-space guard was dropped).

- Warn players before a stop, restart or update, with a countdown and **Stop now** (R-25).
- Live status message in Discord, edited in place (R-63).
- Download a backup from the page (R-54).
- Update-available badge from Steam's build ID (R-34; scheduled updates stay Later).
- Add to home screen: web app manifest and icons (R-72).
- Loose ends: a Phase 5 test that left a fake game running; refused bad-JSON saves not audited; the Settings page's update section checked in a browser.

**Done when:** tests pass, each feature is checked in a browser at `127.0.0.1:8150` (also at phone width) against the fakes, including a local fake Discord webhook.

**Status:** built on the dev PC (2026-10-27). The stop warning is a per-server `stop_warning` (stdin or RCON command with `{minutes}`, lead time and reminders), saved in a new add-only column (migration 0005); it counts down while the server keeps running and holds the server's lock, so nothing else starts meanwhile; **Kill** ends a countdown and a restart with it; updates get a `warning` phase, and cancelling the update during it leaves the server running. Palworld (`Broadcast`) and V Rising (`announce`) templates carry an unverified, switched-off default. The live status message (`basecamp/livestatus.py`) posts with `?wait=true`, then PATCHes that message at most every 10 s, and posts a new one after a 404. Backups download as the zip itself. Build checks run `+app_info_print` through the SteamCMD queue every 6 h and on **Check for update**, kept in `steam-latest.json`. Both pages have a manifest, an SVG icon and PNGs drawn by `tools/make_icons.py`. The leaking test now waits for the restart in flight and kills what it started (a shared `stop_everything` teardown); refused saves are audited as `config.invalid`; the Settings page's update section shows no stray "null". The browser check found the same kind of stray "null" on the server page (for any non-Steam server, and after the new countdown line) and in the config editor's header while loading; fixed. Checked in the browser at `127.0.0.1:8150`, also at 375 px: a warned stop with its reminder, a warned restart with **Restart now**, Kill during a restart countdown (stays down), the live status message posting, editing every ~10 s with the countdown, and posting again after a 404, backup download, the badge after **Check for update** (available, up to date after an update, "unknown" on an unlisted branch), the manifest and icons. New fakes: `tools/fake_discord.py` (also used by the tests) and `app_info_print` in `tools/fake_steamcmd.py`. Decisions D-61..D-67. **Not built:** the RCON client, so RCON warnings (the template defaults) are skipped with a console note until it exists. **Not tried:** a real Discord channel, a real `app_info_print` against Steam, adding to the home screen on a real phone over `tailscale serve` (Phase 7).

## Phase 7 · Prod PC install (with you, one step at a time)

- `scripts/install.ps1`, `uninstall.ps1`, `add-game-firewall-rule.ps1` ([04-deployment.md](04-deployment.md)). Install pulls the latest release.
- Watchdog task (R-06).
- Import the first game (whichever is least risky) in place.
- `tailscale serve` set up.
- Reboot test without logging in.
- Update the owner's private network notes for the host PC.
- Import the remaining games one at a time.

**Done when:** after a cold reboot with nobody logged in, the on-boot servers are up, Discord got "Basecamp started", and the verify commands show nothing else on the PC changed.

**Status (scripts, 2026-10-27):** written and tried on the dev PC; the prod install itself hasn't started. `scripts/install.ps1`, `uninstall.ps1`, `add-game-firewall-rule.ps1`, `verify.ps1` and `install_release.py` (the release check, with a standard-library Ed25519 verifier), all with `-WhatIf` ([04-deployment.md](04-deployment.md)). The watchdog is the task's own 5-minute trigger (D-81). 23 new tests (the verifier against RFC 8032 and `cryptography`, refused releases, the scripts' dry runs and refusals). On the dev PC, against `C:\BasecampTest\` with a `--test-key` release: the `-WhatIf` run changed nothing; a real run with `-SkipTask` downloaded and checked Python 3.13.17, installed the release, and the launcher came up under the embeddable `pythonw.exe -I` with `/healthz` answering in about a second; a fake game started through it kept running through `uninstall.ps1` (launcher and Basecamp ended, `python\` and the data kept while the host ran), a re-install adopted it with its console, and after it stopped from the page `uninstall.ps1 -RemoveData` removed both folders. `verify.ps1` reported each state correctly. The firewall script added a real rule on the dev PC (owner's admin window), which `verify.ps1` passed, and was removed again; that run found two bugs, now fixed but not re-run as admin (a second identical run said "different" because Windows reports the remote as `100.64.0.0/255.192.0.0`, and `verify.ps1`'s remote check compared a variable with itself, PowerShell names not being case-sensitive). **Found:** the start-time port check binds `0.0.0.0` (D-19), so Windows asked whether to allow Basecamp's Python and added two "Python" allow rules (any port) for it; `verify.ps1` now fails on that, and the check itself needs changing before the prod install. **Not tried on the dev PC** (owner's call): creating the task, so S4U, the triggers, `Stop-ScheduledTask` against the hosts (Q-10) and a boot without logging in are first tried on the prod PC; the uninstall's task branch only ran as `-WhatIf`. Also not tried: downloading a release from GitHub (none published yet). Decisions D-80..D-86.

## After v1

The Later list in [01-requirements.md](01-requirements.md), ranked (first cut, 2026-10-28). Size: S = a day or less, M = a few days, L = a week or more.

**Batch 1: managing it from the phone** (small, independent; one release)
1. ~~Default Steam install folder and a pick-list of `steamapps\common` folders (S). Adding or importing a server needs no typed path.~~ **Done** (D-125).
2. ~~**Restart Basecamp** button (S).~~ **Done** (D-126).
3. ~~Apply `settings.toml` changes without a restart, `admin_logins` included (S-M).~~ **Done** (D-127).
4. ~~Firewall indicator per server (M). Catches the "Running but nobody can join" case on the host PC, where Windows never shows its popup.~~ **Done** (D-128).
5. ~~Add a template from the page (S-M).~~ **Done** (D-129).

**Batch 2: friends and new games**
6. ~~**New game from a template** in one flow (M-L). Builds on 1 and 5; templates gain a field for the values a game needs before it works (e.g. Dragonwilds' `OwnerId`).~~ **Done** (D-130, R-16).
7. ~~Friend start/stop/restart permission (M). First the Q-02 test with one friend (cheap, and it decides whether this works at all).~~ **Done** (D-131, R-74). Q-02 is still open, so the login list is untested; "anyone" works either way.
8. ~~Player count (M). Friends see who's on; and 9 can skip or delay a restart or update while players are on.~~ **Done** (D-132, R-73): the count, not names.
9. ~~Scheduled restarts, then scheduled updates (S-M each). Use the stop warnings that exist, and 8 when it's there.~~ **Done** (D-133, R-26, R-35).

**Batch 3: the fresher UI** (L)
10. After batches 1 and 2, so the new screens (the new-game flow, friend controls, player counts) are designed once rather than restyled twice.

**Only when a need shows up**
11. Copying backups off the PC (M). Same-disk backups are fine for now (Q-08).
12. Update commands for custom servers (S). When a non-Steam server is added.
13. Steam login for games that need a real account (M; a password and Steam Guard to keep in the data folder). When a game needs it.
14. Mod management (L, per game). When a game is modded through Basecamp.
15. A relay for friends without Tailscale (L, and it changes the security model). Only if a friend can't use Tailscale (Q-03), see [03-friend-access.md](03-friend-access.md).

## Rough size

Phases 1-6 are around 3,500-4,500 lines of Python plus ~1,000 of HTML/JS. Phases 1, 3 and 6 hold most of the risk (Windows process handling, SteamCMD quirks, self-update and rollback); the UI is straightforward.
