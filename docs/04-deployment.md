# 04 · Deployment on the host PC

How Basecamp is installed on the always-on Windows PC that hosts the game servers. The owner's machine-specific notes (PC name, addresses, other apps sharing the PC and their rules) live outside the repo, in `private/`.

**Claude doesn't deploy this on its own.** Each step below is run by you (or by Claude only after you say go for that step).

## What Basecamp needs

| Need | Choice | Why |
|---|---|---|
| Own Python | `C:\Basecamp\python\`: python.org's embeddable 3.13.17, pinned and hash-checked by `install.ps1`, **isolated** by its `python313._pth` file: nothing outside that folder and the version folder gets onto its `sys.path` (D-123) | Don't share a runtime with other apps on the PC: firewall rules are per program path, so another app's rules would apply to Basecamp, and an upgrade there could break it. Another Python installed on the PC can't reach it either: a python.org install's registry entry once put its `Lib` and `DLLs` ahead of Basecamp's and crashed it |
| User | Runs as **your own account**, not elevated (no "highest privileges") | Not exposed to the internet, so a separate user is more hassle than it's worth (D-11). Game saves, SteamCMD and installs stay where they are |
| Own folders | `C:\Basecamp\` (program) and `C:\BasecampData\` (data), default permissions | Kept apart from other apps' folders |
| Web port | **8150**, bound to `127.0.0.1` | Pick another if something on the PC already uses it (it's a setting) |
| Reach from tailnet | `tailscale serve --bg --https=443 http://127.0.0.1:8150` (Plan A) | No new inbound rule for Basecamp; tailscaled already handles it. Plan B needs an inbound allow for `C:\Basecamp\python\python.exe`, TCP 8150, remote `100.64.0.0/10` (Tailscale) only |
| Game ports | Existing rules stay (exe paths don't change). New games: inbound allow **per game exe, per port**, remote `100.64.0.0/10` (Tailscale) | Never broad allow rules |
| Outbound | Steam (SteamCMD, game servers' Steam master server), Discord webhook, GitHub (release checks and downloads) | Internet only, nothing to the LAN |
| Startup | Task `Basecamp start`: At startup (1 min delay), as you, "Run whether user is logged on or not" + **"Do not store password"**, plus every 5 min (starts the launcher only if it's not running). The task runs the launcher, which runs Basecamp ([07-updates.md](07-updates.md)) | Starts before login. "Do not store password" means no password in Task Scheduler and nothing breaks when you change it; the catch is the task can't open Windows file shares, which Basecamp doesn't need. If a game misbehaves that way, fall back to storing the password (Phase 0 check) |
| Task settings | **"Stop the task if it runs longer than" off** (Windows defaults it to 3 days, which would kill Basecamp and possibly its games). "If the task is already running: do not start a new instance" | R-04a |
| Network | No router port forwards, no UPnP, network profile unchanged | Everything goes over Tailscale |

## Bringing existing games in

Because Basecamp runs as you, nothing moves. SteamCMD stays where it is (its path is a setting, default `%USERPROFILE%\Desktop\SteamCmd\steamcmd.exe`), game installs stay where they are, and saves that live in your profile (e.g. Valheim's `AppData\LocalLow\IronGate\Valheim\`) keep working.

1. For each game: stop it if you started it by hand, then **import** its folder in Basecamp (R-15).
2. Existing firewall rules for the game exes keep working since the paths don't change. Check each is scoped to Tailscale (`100.64.0.0/10`) rather than "any".
3. Start it from Basecamp and check friends can join.

**What you give up:** a game server that got hacked would run with your account's rights (not admin, since the task isn't elevated, but it can read your files). That's an acceptable risk while servers are only reachable over Tailscale. If you ever move to a public relay like playit.gg ([03-friend-access.md](03-friend-access.md)), revisit this.

## Install

Four scripts in `scripts\`, all with `-WhatIf` (prints every change, makes none). Run them from a copy of the repo with **Windows PowerShell as administrator**; `powershell -ExecutionPolicy Bypass -File <script>` gets past the execution policy without changing it.

```powershell
scripts\install.ps1 -WhatIf      # read what it would do
scripts\install.ps1              # do it; safe to run again
schtasks /Run /TN "Basecamp start"    # or reboot
scripts\verify.ps1
tailscale serve --bg --https=443 http://127.0.0.1:8150   # you run this; no script does
# only for a game that has no rule yet:
scripts\add-game-firewall-rule.ps1 -Exe <path to game server exe> -Port 8211 -Protocol UDP
```

### `install.ps1`

| Step | What it does | Again on a re-run |
|---|---|---|
| Checks | Admin, not SYSTEM; the program folder is empty or Basecamp's (refuses another app's); the port is free or Basecamp's | Same |
| Folders | `C:\Basecamp\` and `C:\BasecampData\` (`-Root`, `-DataRoot`), default permissions | Skips what's there |
| Python | python.org's embeddable Python **3.13.17** (pinned with its SHA-256 in the script; the last 3.13 with Windows binaries) into `<root>\python\`, with Basecamp's `python313._pth` (`python313.zip` and `.`, no `import site`): Python then ignores the registry's `PythonPath`, `PYTHONPATH`, `PYTHONHOME` and user site-packages (D-123). Then a check run without `-I` and with `PYTHONPATH` pointing elsewhere (`PYTHONHOME` cleared, as it would hide the registry): `sqlite3`, `ssl`, `ctypes` import, every `sys.path` entry is under `<root>\python\` and `_asyncio` loads from there, before it replaces anything. `-PythonZip` uses a local copy | Skipped if 3.13.17 is there and isolated. An install from before 0.2.1 (no `._pth`) gets it written after the release step (step 4b), with the same check. Replacing the runtime is refused while anything runs from it (Basecamp or a game's host): stop the servers and the launcher first (D-82) |
| Release | The latest GitHub release (`-ReleaseZip` for a local one, `.sig` next to it). Signature checked against `basecamp\release_key.pub` from the copy you run it from (printed, so you can compare it with the dev PC's), then the manifest, the Python version, every file's hash and that its `launcher.py` is version 2 or newer, by `scripts\install_release.py` on the new Python (D-80). Unpacked to `versions\<v>\`, `launcher.py` copied out, `current.txt` written | Basecamp is installed and updates itself from the page, so only `launcher.py` is refreshed from the current version. **Except** when the current version's launcher is version 1 (Basecamp 0.2.0 and older, which set `PYTHONPATH`): then the latest release is installed the way an update is: `db-backups\before-<v>.db`, the old version in `previous.txt`, the new one on trial, so the launcher rolls back if it isn't healthy (D-123). That and step 4b need the launcher and Basecamp stopped; the script refuses and prints how otherwise |
| Settings | A starter `<data>\settings.toml` (port, commented `join_host` and `admin_logins`, `steamcmd_path` if it finds SteamCMD on your Desktop) | Never overwritten |
| Baseline | `<data>\install-baseline.json`: network profiles, enabled inbound allow rules, listening ports, before Basecamp runs. `verify.ps1` compares with it | Kept |
| Task | `Basecamp start`, registered from XML (D-83) with `Register-ScheduledTask`, or `schtasks /Create /XML` if that fails (D-124): you (by SID), **S4U** ("Do not store password"), not elevated; **at startup + 1 min** and **every 5 min, indefinitely**; **no time limit**; **do not start a new instance**; normal priority (Task Scheduler's default is below normal, and the games would inherit it); runs `<root>\python\pythonw.exe -I <root>\launcher.py --data <data>`. `-SkipTask` leaves it out (no admin needed; dev PC tests) | Re-registered with the same definition, which also enables it again if you disabled it; a running launcher keeps running |
| Last | Prints the `tailscale serve` command, `schtasks /Run` and `verify.ps1` | Same |

Nothing in it adds a firewall rule, a port forward or a network-profile change.

### Updating an install from before 0.2.1

Basecamp 0.2.0 and older ran on a runtime without a `._pth` file, so a python.org Python on the same PC could get its folders onto Basecamp's `sys.path` through the registry (D-123). Their launcher (version 1) passes Basecamp's folders in `PYTHONPATH`, which the isolated runtime ignores, so Basecamp can't update itself out of this (0.2.1 says `min_launcher: 2`, and the page says it needs a manual reinstall). Fix it with the 0.2.1 (or newer) copy of the repo, in an admin PowerShell. Game servers can keep running; only the launcher and Basecamp must be stopped:

```powershell
schtasks /Change /TN "Basecamp start" /DISABLE
schtasks /End /TN "Basecamp start"         # ends the launcher; Basecamp exits within seconds (D-110)
scripts\install.ps1 -WhatIf                # expect: "isn't isolated", a newer release, "Isolate the runtime"
scripts\install.ps1                        # installs the latest release on trial, writes python313._pth, re-registers (and so enables) the task
schtasks /Run /TN "Basecamp start"
scripts\verify.ps1
```

If the launcher or Basecamp still runs, `install.ps1` lists them and stops before changing anything; run it again once they've gone.

### Watchdog (R-06)

The every-5-minutes trigger on `Basecamp start` **is** the watchdog; there's no second task (D-81). The task's action is the launcher, so a task instance lives exactly as long as the launcher. With "do not start a new instance" the 5-minute trigger does nothing while it runs, and starts it again within 5 minutes once it has died; `launcher.lock` stops a second launcher started any other way. Basecamp itself dying is the launcher's job (restart with back-off, [07-updates.md](07-updates.md)). A separate watchdog task would run the same action with the same account, and disabling or deleting `Basecamp start` should stop both anyway.

### `add-game-firewall-rule.ps1`

`-Exe -Port -Protocol [-Name] [-Replace]`: one inbound allow in group **Basecamp**, for that program only, that protocol and port(s) only (`-Port 15636,15637`, at most 10, no ranges), remote **`100.64.0.0/10`** only, all profiles. Run it once per protocol. Refused: a path that isn't a full path to an existing `.exe`; a shell or script runtime (python, java, node, powershell, cmd, ...) or anything under the Windows folder; Basecamp's own Python; ports below 1024; a rule with the same name that allows something else (unless `-Replace`). The same rule again changes nothing (D-85). Other software on the host PC may alert when firewall rules change; that's expected after this.

## After installing: verify

`scripts\verify.ps1` only reads and exits with the number of FAILs (D-86). Run it in an **admin** PowerShell: the task runs Basecamp in its own logon session, and a normal PowerShell can't read those processes, so its checks on them come out as WARN there (D-112).

- `<root>\python` has 3.13.17 and Basecamp's `python313._pth`, and is isolated: run without `-I` and with `PYTHONPATH` set elsewhere, every `sys.path` entry is under `<root>` and `_asyncio` loads from `<root>\python` (D-123). `current.txt` names an unpacked version; `launcher.py` is version 2 or newer.
- Basecamp listens on `127.0.0.1:<port>` only, from its own Python; every listening TCP port is listed, with the ones that weren't there before the install marked `new`.
- Each rule in group Basecamp is an inbound allow for one program, specific ports, remote `100.64.0.0/10`. **No** inbound allow rule exists for Basecamp's own Python (Windows adds "Query User" rules, any port, if someone answers its "allow this app?" prompt). Enabled inbound allow rules outside the group that appeared since the install are listed.
- Each network profile is what it was before the install.
- The task's account, S4U, run level, triggers, time limit, instances, priority and action, read from its XML (`Export-ScheduledTask`, or `schtasks /Query /XML` where the cmdlets fail, D-124).
- `/healthz` answers with the version in `current.txt`; one launcher runs; each game host and its game are listed.

The same checks by hand:

```powershell
# Basecamp on 127.0.0.1:8150 only, and nothing else on the PC changed
Get-NetTCPConnection -State Listen | Select LocalAddress,LocalPort,@{n='Process';e={(Get-Process -Id $_.OwningProcess).ProcessName}} | Sort LocalPort
Get-NetFirewallRule -Group Basecamp | Select DisplayName,Direction,Action,Enabled
Get-NetConnectionProfile
schtasks /Query /TN "Basecamp start"
# Is Basecamp's Python isolated? Every entry under C:\Basecamp\python, even with PYTHONPATH set
$env:PYTHONPATH = 'C:\nowhere'; C:\Basecamp\python\python.exe -c "import sys, _asyncio; print(sys.path, _asyncio.__file__)"; Remove-Item env:PYTHONPATH
```

Then reboot the PC **without logging in** and check from your phone that Basecamp and the on-boot servers come up. Restart with `shutdown /r /t 0`: a restart from the Start menu can sign you in by itself ("Use my sign-in info to automatically finish setting up"), which spoils the test. `Get-CimInstance Win32_LogonSession | ? LogonType -eq 2` shows when you really signed in.

**Watch out:** when Basecamp starts a game it checks the game's ports are free by binding them on `0.0.0.0` (`basecamp/procs.py`, D-19). On the dev PC that made Windows show its "allow this app?" prompt, and answering it added two "Python" allow rules (TCP and UDP, any port, Private profile) for Basecamp's `pythonw.exe`. Until that check stops binding a non-loopback address, answer **Cancel** if that prompt appears on the host PC, and run `verify.ps1` after the first game start.

### When the ScheduledTasks cmdlets fail

On a PC whose task cache (the registry's `TaskCache`) lists tasks whose files are gone, `Get-ScheduledTask` fails with `0x80070002` (file not found) even for `-TaskName 'Basecamp start'`, and `Get-ScheduledTaskInfo`, `Disable-ScheduledTask`, `Stop-ScheduledTask` and `Start-ScheduledTask` may fail the same way. `schtasks.exe` still works there, and the scripts fall back to it (D-124). By hand:

| Cmdlet | `schtasks` |
|---|---|
| `Get-ScheduledTask` / `Export-ScheduledTask` | `schtasks /Query /TN "Basecamp start" /XML` (or `/FO LIST /V` for state and last result) |
| `Disable-ScheduledTask` / `Enable-ScheduledTask` | `schtasks /Change /TN "Basecamp start" /DISABLE` / `/ENABLE` |
| `Stop-ScheduledTask` | `schtasks /End /TN "Basecamp start"` (ends the launcher; see below for why the uninstall doesn't use it) |
| `Start-ScheduledTask` | `schtasks /Run /TN "Basecamp start"` |
| `Unregister-ScheduledTask` | `schtasks /Delete /TN "Basecamp start" /F` |
| `Register-ScheduledTask -Xml` | `schtasks /Create /TN "Basecamp start" /XML <file> /F` (the file in UTF-16) |

Leave the orphaned entries alone: they're Windows' own tasks, and Basecamp's scripts never touch any task but theirs.

## Removing Basecamp

```powershell
scripts\uninstall.ps1 -WhatIf
scripts\uninstall.ps1              # keeps C:\BasecampData
scripts\uninstall.ps1 -RemoveData  # also removes it
```

It disables the task (so the 5-minute trigger can't bring it back; `schtasks /Change /DISABLE` where the cmdlets fail), ends the launcher and then Basecamp, **each by its PID and never as a tree**, removes the task (`schtasks /Delete` as the fallback) and the program folder. Game servers and their hosts keep running (R-04a): it never uses `Stop-ScheduledTask`, which ends the processes in the task's job (on the dev PC only the launcher, Q-10, but it isn't needed). A running game's host runs from `<root>\python\`, so that folder stays, with a note `uninstall-pending.txt`, until those games have stopped; run it again then (D-111). The data folder stays unless `-RemoveData`, and also while a host still writes its logs there (D-84). Firewall rules in group Basecamp and `tailscale serve` are left alone; it prints how to remove them.

## Testing the scripts on the dev PC

Against a test folder, with a release built by `scripts\build_release.py --test-key` and a throwaway key (never the real one):

```powershell
scripts\install.ps1 -Root C:\BasecampTest -DataRoot C:\BasecampTestData -ReleaseZip <zip> -PublicKey <throwaway .pub> -Port 8156 -SkipTask
```

To test the task itself, leave out `-SkipTask` and give it its own name and port, e.g. `-TaskName 'Basecamp start (test)' -Port 8151`, in an admin PowerShell (results from 2026-10-28: Q-04, Q-09, Q-10). Without the task, start the launcher the way the task does (`C:\BasecampTest\python\pythonw.exe -I C:\BasecampTest\launcher.py --data C:\BasecampTestData`), and run `verify.ps1` and `uninstall.ps1` with the same `-Root`/`-DataRoot`. `tests\test_install_scripts.py` covers the release check and the scripts' `-WhatIf` runs.

To check the runtime's isolation (D-123), give the dev PC what the host PC had: a value for `HKCU\Software\Python\PythonCore\3.13\PythonPath` naming a folder with a junk `_asyncio.py` (and another 3.13's `DLLs`). An install from before 0.2.1 then fails to start Basecamp; a 0.2.1 install, fresh or re-run over the old one, comes up healthy and `verify.ps1` passes. Delete the key afterwards. The `schtasks` fallbacks (D-124) can be tried on a PC where the cmdlets work by defining throwing `Get-ScheduledTask` (and friends) functions in a copy of the script, after its `Import-Module` line, and running it with `-WhatIf` (or `verify.ps1` against any task, which only reads).
