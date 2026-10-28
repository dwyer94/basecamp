# Changes

Release notes. `scripts\release.ps1 <version>` publishes the section for that version as the
GitHub release's notes, which the admin dashboard shows next to its Install button.

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
