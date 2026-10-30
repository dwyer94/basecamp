# Changes

Release notes. `scripts\release.ps1 <version>` publishes the section for that version as the
GitHub release's notes, which the admin dashboard shows next to its Install button.

## 0.3.0

A new look, friends who can run the servers, and a guided way to add a game. Install it from
the update banner: no reinstall, and game servers keep running. It adds database tables
only, so rolling back to 0.2.1 still works.

- **Add server needs no typed path.** Picking a game fills in SteamCMD's usual folder for
  it (`<SteamCMD folder>\steamapps\common\<game folder>`), and **Or import an existing
  install** lists the folders already in `steamapps\common`, with their app IDs and which
  ones a server already uses. Installs SteamCMD made there without `+force_install_dir`
  (manifest in the library, not the game folder) are detected and show their build too.
  Templates have a new optional field, `steam_installdir`.
- **Restart Basecamp** button on the Settings page: Basecamp exits and the launcher starts
  it again within seconds; game servers keep running. The page waits and reloads. It's
  refused while an install or update is queued or running, and under `python -m basecamp`.
- **`settings.toml` edits apply without a restart**, `admin_logins` included: Basecamp
  notices a saved change within a few seconds, and the Settings page has **Reload
  settings**. `host`, `port` and the console buffer sizes still wait for a restart, and the
  page says which changed. A file that doesn't load (bad TOML, a typo'd key, a wrong type)
  leaves the settings as they were and shows the error. Reloads go in the audit log with
  the names of the keys that changed, never their values.
- **Firewall indicator** on each server's page and a badge on its card: for every port,
  whether Windows Firewall lets Tailscale traffic in (open), lets more than Tailscale in
  (too broad), blocks it, or has no rule (missing, with the `add-game-firewall-rule.ps1`
  command to copy). It only reads the rules, about once a minute or on **Check again**,
  and it isn't a reachability test: Tailscale's access rules, the address the game listens
  on or Steam networking can still stop friends.
- **Add a template from the page**: paste the JSON or pick a `.json` file on the Templates
  page. It's checked as strictly as a file and saved to the data folder's `templates`, so a
  new game needs no file copied onto the host PC. Overriding a built-in or replacing an
  existing one asks first; data-folder templates (and broken files) can be deleted there.
- **New game**, in one flow: pick the game's template, and Basecamp adds the server,
  installs it with SteamCMD, starts it once so it writes its config files, stops it, asks
  for the values the game needs (Dragonwilds: your Player ID, its `OwnerId`) and writes
  them the way the config editor saves, keeping a copy of the old file, shows the firewall
  check, and then it's ready to start. Each step shows its progress; you can leave the page
  and carry on later from the dashboard. A step that fails stops there with the reason and
  **Try again**. Templates have a new optional field, `required_values`.
- **Friends can start, stop and restart servers**, if you let them: on the Settings page,
  either a list of Tailscale logins or anyone who can open the page (off by default). They
  get Start, Stop and Restart on their page and nothing else. A stop or restart still warns
  players first, each click goes in the audit log with their login (or "unknown"), and a
  server takes one friend click a minute. The login list only works if a friend's request
  carries their Tailscale login, which hasn't been checked with a friend's device yet;
  "anyone" works either way.
- **Player count** on both pages, for games that can tell: Palworld through its REST API,
  Enshrouded and V Rising through Steam's server query, or a game's RCON command. Asked
  once a minute while the server runs; "unknown" when it can't tell. Friends see the
  number, never names. Set under **Edit → Files → Player count**; templates have a new
  optional field, `players`.
- **Scheduled restarts and updates**: per server, restart every day at a time, and update
  every day at a time when Steam has a newer build (both off by default, under **Edit →
  Schedule**). They work like the buttons: stop warning, graceful stop, SteamCMD queue,
  Discord messages. While players are on, a server can go ahead anyway, skip that day, or
  wait until they've left (up to 2 hours); an unknown player count counts as nobody on.
- **A fresher page.** One page for you and your friends: a sidebar on a desktop and bottom
  tabs on a phone, light and dark (following the system, with a toggle). Server cards show
  the state in a coloured band with the one fact that matters (uptime, countdown, crash,
  update progress), the player count, when the game was last updated, and only the buttons
  that fit. The dashboard has a summary and a **Needs you** list (crashes Basecamp gave up
  on, failed updates, Basecamp's own update, unfinished setups). The server page has tabs
  (Console, Config files, Backups, Schedule, Firewall, History) beside a details column;
  schedules moved to their own tab. The config editor's Save bar stays on screen, with
  **Save and restart** for a running server. Every risky button asks in a sheet on the page
  instead of a browser box. Fonts ship with Basecamp; the page loads nothing from the
  internet.
- **Activity** replaces the audit log page: a feed by day with filters. It now also shows
  Basecamp's own events: crashes, giving up restarting, update and install results,
  scheduled backups, failed scheduled restarts, and Discord messages that didn't send. Hard
  failures get a red count in the menu until you dismiss them, or until that server runs
  normally again (Discord failures clear only on dismiss).
- **Friends see more, and may Update.** Friends get your page with your parts hidden: the
  cards (CPU and RAM, last updated, update available), the server page with a read-only
  console (earlier lines too, SteamCMD output and folder paths left out), the schedule,
  the last backup's time, firewall status and Activity without your events. With friend
  controls on they can also **Update** (same warning and save backup as yours) and tap the
  quick commands a template marks for friends. A crashed server reads "Down" for them.
- **Quick commands** in the console: one tap per command from the game's template (new
  optional template field `quick_commands`; Palworld: Save, Players, Broadcast; V Rising:
  Announce, Restart warning; both not yet tried on the real games).
- **Last updated** on each card and the server page: when the game itself was last
  updated, from Steam's app manifest (so updates outside Basecamp count), else Basecamp's
  own last update.
- The page's scripts are now checked for a newer copy on every load, so a phone never mixes
  an old and a new one after an update.
- `install.ps1 -WhatIf` no longer fails with "Release check failed: No such file" when
  it would download a release: a dry run skips the download, so it now says the real run
  checks the signature once the file is there.
- **Cancel a countdown.** While a stop, restart or update counts down with its warning to
  players, **Cancel restart** (or Cancel stop, Cancel update) on the card and the server
  page calls it off and the server keeps running. Friends with friend controls on can
  cancel too, the owner's and scheduled ones included. Once the stop itself has begun it's
  too late, and the page says so. It's in Activity with who did it and posted to Discord.
  A cancelled scheduled restart or update waits until the next day.
- **"Stopped since 23:40" and "gave up after 3 tries"** on the cards, the server page and
  Needs you, for friends too. Kept across Basecamp restarts (a new table, migration 0011).
- **See what friends see**: a button on Settings → Friends turns your own view into the
  friend view, served exactly as friends get it, with a banner that says which friend
  controls are in effect and **Back to owner view**.
- **Debug views** for the owner: read-only `/api/debug/...` answers with Basecamp's log,
  the launcher's log, a server's console log, health, the settings and recent errors, so
  the host PC can be looked at from another PC over Tailscale. Secrets are blanked out of
  everything they return, each read goes in the audit log, and friends get none of it
  ([docs/09-debug.md](docs/09-debug.md)).

## 0.2.1

Fixes for the first install on the host PC. Install it by running `scripts\install.ps1`
again from this version's copy of the repo (docs/04-deployment.md, "Updating an install
from before 0.2.1"): Basecamp 0.2.0 can't update itself to it.

- **Basecamp's Python is isolated.** Another Python installed on the PC (python.org's, with
  its registry entry) could put its own folders ahead of Basecamp's, and Basecamp crashed
  on start. The runtime now has a `python313._pth` file, so the registry, `PYTHONPATH`,
  `PYTHONHOME` and user site-packages can't reach it, and the launcher (version 2) puts
  Basecamp's folders on its path itself. `install.ps1` and `verify.ps1` check that every
  path entry is inside the install folder.
- `install.ps1` on an older install: installs this release the way an update would (DB copy,
  0.2.0 kept to roll back to), then writes the `._pth` file. It refuses while the launcher
  or Basecamp runs and prints how to stop them; game servers can keep running.
- The scripts work where Windows' ScheduledTasks commands fail (a task cache with entries
  for missing files): they fall back to `schtasks.exe`, so `verify.ps1` finds the task and
  `uninstall.ps1` disables and removes it.

## 0.2.0

No changes to Basecamp itself. This release tries out updating from GitHub: the page
offers it, downloads it, checks its signature and files, and swaps over without stopping
the game servers.

## 0.1.0

First release: the core supervisor with per-game host processes, the admin and friend
pages over Tailscale, templates, SteamCMD installs and one-click updates, save backups,
crash restart, Discord messages, the config editor, the settings page, self-update from
GitHub Releases through the launcher, RCON and Palworld's REST API.

- **RCON** (V Rising, Palworld and many others) and **Palworld's REST API** for stop
  steps, warnings to players and the console. The console's command box can send by stdin,
  RCON or REST and shows the answer. Set each server's RCON or REST port and password on its
  form; passwords stay in the data folder's `secrets.toml` and are never shown again.
  RCON and REST only talk to games on the same PC.
- An RCON or REST problem (off in the game, wrong password, no answer) never holds up a
  stop: the console says why and the stop goes on to its next step.
- Templates, checked by running each dedicated server under Basecamp and watching it stop:
  - **Palworld**: starts `PalServer-Win64-Shipping-Cmd.exe` directly, so its output reaches
    the console. With the REST API on, a stop saves and shuts down cleanly and the warning
    is a REST announce. Without it, Ctrl+Break ends it without saving (it autosaves every
    30 s). New port: TCP 8212 (REST).
  - **Enshrouded**: reads the console instead of the log file (no more doubled lines);
    Ctrl+Break saves.
  - **V Rising**: real ready line, ports and settings files; Ctrl+Break saves; warning by
    RCON announce.
  - **RuneScape: Dragonwilds**: its server never saves when it stops, so Basecamp waits
    for its next autosave and then sends Ctrl+C, which ends it cleanly. The template makes
    it autosave every 2 minutes, so a stop takes up to about 2 minutes.
  - **Windrose**: starts the Shipping exe with `-log` from the install folder, which gives
    it a console so Ctrl+Break saves; real ready line, config file and save folder.
- Stop steps **ctrl_c** (Ctrl+C to the console) and **wait_save** (wait for the game to
  save to its save folder, then go on), for any game.
- Basecamp exits when its launcher is gone, so ending the scheduled task never leaves one
  holding the port; game servers keep running.
