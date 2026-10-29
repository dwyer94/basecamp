# Changes

Release notes. `scripts\release.ps1 <version>` publishes the section for that version as the
GitHub release's notes, which the admin dashboard shows next to its Install button.

## Unreleased

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
- `install.ps1 -WhatIf` no longer fails with "Release check failed: No such file" when
  it would download a release: a dry run skips the download, so it now says the real run
  checks the signature once the file is there.

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
