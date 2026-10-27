# 08 · Game templates: adding a game

A template is a JSON file that fills in the **Add server** form for one game (R-13): Steam app ID, exe, launch arguments, ports, the line that means "ready", how to stop it gracefully, config files and the save folder. You still review the form and can change anything before saving. A template only pre-fills new servers; servers made from it earlier keep their own settings.

Templates come from two places:

| Where | When to use it | Source label in the UI |
|---|---|---|
| `templates\<id>.json` in the repo | Games everyone should get. Ships with each release | built-in |
| `<data>\templates\<id>.json` (prod: `C:\BasecampData\templates\`, dev: `.\data\templates\`) | Trying a new game now, or fixing a built-in one without waiting for a release | data folder |

A data-folder template with the same `id` **overrides** the built-in one (the UI says "data folder (overrides built-in)"). Basecamp reads both folders every time the form or the **Templates** page opens, so no restart is needed. A broken file is skipped and shown in red on the Templates page with the reason; the others keep working.

## The fields

Paths are relative to the server's **install folder**, unless they start with a drive (`D:\...`), a slash, or an environment variable (`%LOCALAPPDATA%\...`, expanded on the PC Basecamp runs on). In JSON, every `\` is written `\\`.

| Field | Required | What it is |
|---|---|---|
| `id` | yes | Lower-case letters, digits and `-`. Must match the file name (`palworld` → `palworld.json`) |
| `name` | yes | Shown in the game picker and as the default server name |
| `kind` | | `"steam"` (default) or `"custom"` |
| `steam_app_id` | for steam | The **dedicated server's** app ID, not the game's (see below) |
| `exe` | yes | The exe Basecamp starts, e.g. `"PalServer.exe"` |
| `args` | | Launch arguments as you'd type them. Keep any port in here the same as in `ports` |
| `working_dir` | | Folder to start in. Empty = the exe's folder |
| `real_exe_name` | | For games whose exe is a launcher that starts the real server and exits (most Unreal games). The real exe's file name, e.g. `"PalServer-Win64-Shipping-Cmd.exe"`. Basecamp then tracks that process instead (D-22) |
| `ports` | yes | List of `{"port": 8211, "protocol": "udp", "label": "game"}`. Basecamp refuses to start if one is in use (D-19). List every port the game opens, including query and RCON |
| `join_port_label` | | Which port friends type in, by label (R-24). Default: the first port |
| `log_source` | | `"stdout"` (default: the console pipe) or `"file"` (tail a log file the game writes) |
| `log_file` | for file | The log file, relative to the working folder |
| `ready_pattern` | | Text in the output that means the server is up, e.g. `"Running Palworld dedicated server on"`. Plain text, not a regex |
| `ready_after_s` | | With no ready line (or until it shows), the server counts as Running after this many seconds. Default 60 |
| `stop_recipe` | | Steps for a graceful stop, see below. Empty = Ctrl+Break, wait 30 s, kill |
| `stop_warning` | | A command that tells players a stop is coming, see below. Optional |
| `config_files` | | Files the admin may edit from the page (Phase 5) |
| `save_dir` | | The folder to back up (Phase 4) |
| `rcon_port` | | RCON port, if the game has RCON (password goes in `secrets.toml`, never here) |
| `min_free_ram_mb` | | Warn before starting if free RAM is below this (never blocks) |
| `notes` | | Setup tips shown on the Templates page and copied into the server's notes |
| `docs` | | Links to where the facts came from. Built-in templates must have at least one |
| `unverified` | | Field names not yet checked against a real install, e.g. `["ready_pattern", "stop_recipe"]`. Shown as "Not yet checked" in the UI and the form. Remove a name once you've checked it |

Unknown fields are refused (a typo like `"prots"` is reported, not ignored).

### Stop recipe

Each step does something, then waits up to `wait_s` seconds for the server to exit. If it hasn't exited after the last step, Basecamp kills it.

```json
"stop_recipe": [
  {"action": "rcon", "command": "Save", "wait_s": 15},
  {"action": "stdin", "command": "quit", "wait_s": 60},
  {"action": "ctrl_break", "wait_s": 30}
]
```

- `stdin`: types `command` into the server's console. Only for games that read commands from their console window.
- `rcon`: sends `command` over RCON. Until Basecamp's RCON client is built these steps are skipped (no wait), so always end with a `ctrl_break` step.
- `ctrl_break`: Ctrl+Break, like pressing Ctrl+C in the server's console window. Most dedicated servers save and quit on it, but check.

### Stop warning

Before a stop, restart or update started from Basecamp (never a crash restart), Basecamp can tell players (R-25, D-61). The command goes out at each of `minutes` before the stop, with `{minutes}` and `{seconds}` filled in:

```json
"stop_warning": {"action": "rcon", "command": "announce Server restarting in {minutes} min", "minutes": []}
```

- `action`: `stdin` or `rcon`, as in the stop recipe.
- `command`: the game's broadcast or announce command. Other braces are left as they are.
- `minutes`: the lead time and reminders, e.g. `[5, 1]`. Leave it empty in a template: the warning is then off until the admin types the minutes into the server's form, so a game's default never delays anyone's stop by surprise.

If the warning can't be sent (no stdin after adoption, RCON not available), the countdown ends and the stop goes ahead at once. Until Basecamp's RCON client is built, `rcon` warnings are skipped like `rcon` stop steps. Put `stop_warning` in `unverified` until you've seen the message in game.

## Finding the facts for a new game

1. **App ID.** Search the game on [SteamDB](https://steamdb.info/) and open its **Dedicated Server** entry (a separate "Tool" app, usually with "Dedicated Server" in the name). The number in the URL is the app ID. Check that it says it can be downloaded anonymously; Basecamp's SteamCMD runs `+login anonymous`.
2. **Install it** on the dev PC into a scratch folder:
   `steamcmd +force_install_dir C:\Temp\MyGame +login anonymous +app_update <app id> validate +quit`
3. **Exe.** Look in the install folder for the server exe (often `<Game>Server.exe`, or a `.bat` the developers ship: open the `.bat` to see the real exe and arguments). Start it once by hand in a console. In Task Manager → Details, check whether a *different* exe keeps running after the first one exits (Unreal games: `...\Binaries\Win64\<Name>-Win64-Shipping.exe`). If so, that's `real_exe_name`.
4. **Ports.** The game's official server guide is best. To check, while it runs: `Get-NetUDPEndpoint -OwningProcess <pid>` and `Get-NetTCPConnection -State Listen -OwningProcess <pid>` in PowerShell.
5. **Ready line.** Watch the console (or log file) during that first start and copy a line that appears only once the server accepts players.
6. **Output.** If the console window shows output but a pipe would be empty, or the game says it writes `...\Logs\<name>.log`, use `"log_source": "file"`.
7. **Graceful stop.** Press Ctrl+C in its console window: does it save and exit within a minute? Does it accept `quit`/`stop`/`shutdown` typed into the console? Does it have RCON? Write the recipe from that.
8. **Config and saves.** Note which files the first start created (settings `.ini`/`.json`, the save folder). Many Unreal games use `<Project>\Saved\Config\WindowsServer\` and `<Project>\Saved\SaveGames\`; some put saves in `%LOCALAPPDATA%` or `%USERPROFILE%\AppData\LocalLow\`.

Anything you got from a guide but didn't see yourself goes in `unverified`.

## Testing a new template locally (dev PC)

1. Save it as `.\data\templates\<id>.json` and open **Templates** in Basecamp (`http://127.0.0.1:8150/#templates`): it should be listed with the source "data folder", and any mistake is shown in red with the reason.
2. **Add server** → pick it → enter the scratch install folder → **Fill in**. Check the paths it filled in, and any warnings (exe not found, unverified fields).
3. Save, **Start**, and watch the console: it should reach Running by the ready line, not the timer. Send a command if the game reads stdin.
4. **Stop**: the console shows each step. It should exit during a recipe step, not "killed". Check the save folder was written.
5. Delete the test server (game files are never touched) and the scratch install.

`tools\dev_template.py` writes a "Fake server (dev)" template into `.\data\templates\` that runs `tools\fake_server.py`, if you want to see the flow without a real game. Automated tests never run real games; the built-in files are checked by `tests\test_templates.py` (valid, have `docs` links and a join port).

## Shipping it

- **Just for this PC, now:** copy the file to `C:\BasecampData\templates\` on the prod PC. It works at once, no release needed.
- **For good:** move it to `templates\` in the repo, drop the fields from `unverified` that you checked, run `pytest` and `ruff check .`, and commit to `main`. It goes out with the next release ([07-updates.md](07-updates.md#making-a-release-dev-pc)), which the owner publishes. Then delete the data-folder copy on the prod PC, or it keeps overriding the built-in one.

## Built-in templates and what's not yet checked

Researched from official guides where they exist (links in each file's `docs`), not yet run on the prod PC. Everything in `unverified` needs checking before relying on it, and Phase 0's "save locations" task feeds into `save_dir`.

| Game | App ID | Unverified |
|---|---|---|
| Palworld | 2394010 | `real_exe_name` (Shipping-Cmd vs older Test-Cmd), `ready_pattern`, `stop_recipe` (does Ctrl+Break save?), `ports` (query 27015), `stop_warning` (RCON `Broadcast`; spaces in it are reported to cut the message, so it uses `_`) |
| Enshrouded | 2278520 | `ports`, `join_port_label` (join on the query port?), `stop_recipe`, `save_dir` |
| V Rising | 1829350 | `ready_pattern` (none found), `stop_recipe`, `ports` (docs examples use 27015/27016, defaults 9876/9877), `stop_warning` (RCON `announce`) |
| RuneScape: Dragonwilds | 4019830 | `real_exe_name`, `args`, `log_file`, `ready_pattern`, `stop_recipe`, `config_files`, `save_dir` (Jagex's guide and the wiki disagree on install folder vs `%LOCALAPPDATA%`) |
| Windrose | 4129620 | `real_exe_name`, `ports` (only with direct connection; default is invite codes), `log_file`, `ready_pattern`, `stop_recipe`, `config_files` (`ServerDescription.json` in the install folder or `R5\`) |
