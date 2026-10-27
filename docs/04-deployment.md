# 04 · Deployment on the host PC

How Basecamp is installed on the always-on Windows PC that hosts the game servers. The owner's machine-specific notes (PC name, addresses, other apps sharing the PC and their rules) live outside the repo, in `private/`.

**Claude doesn't deploy this on its own.** Each step below is run by you (or by Claude only after you say go for that step).

## What Basecamp needs

| Need | Choice | Why |
|---|---|---|
| Own Python | `C:\Basecamp\python\`: python.org's embeddable 3.13.17, pinned and hash-checked by `install.ps1` | Don't share a runtime with other apps on the PC: firewall rules are per program path, so another app's rules would apply to Basecamp, and an upgrade there could break it |
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
Start-ScheduledTask -TaskName 'Basecamp start'    # or reboot
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
| Python | python.org's embeddable Python **3.13.17** (pinned with its SHA-256 in the script; the last 3.13 with Windows binaries) into `<root>\python\`, its `._pth` file removed so the launcher's `PYTHONPATH` works, then a smoke test (`sqlite3`, `ssl`, `ctypes`, `PYTHONPATH`) before it replaces anything. `-PythonZip` uses a local copy | Skipped if 3.13.17 is there. Replacing it is refused while anything runs from it (Basecamp or a game's host): stop the servers and the launcher first (D-82) |
| Release | The latest GitHub release (`-ReleaseZip` for a local one, `.sig` next to it). Signature checked against `basecamp\release_key.pub` from the copy you run it from (printed, so you can compare it with the dev PC's), then the manifest, the Python version and every file's hash, by `scripts\install_release.py` on the new Python (D-80). Unpacked to `versions\<v>\`, `launcher.py` copied out, `current.txt` written | Basecamp is installed and updates itself from the page, so only `launcher.py` is refreshed from the current version |
| Settings | A starter `<data>\settings.toml` (port, commented `join_host` and `admin_logins`, `steamcmd_path` if it finds SteamCMD on your Desktop) | Never overwritten |
| Baseline | `<data>\install-baseline.json`: network profiles, enabled inbound allow rules, listening ports, before Basecamp runs. `verify.ps1` compares with it | Kept |
| Task | `Basecamp start`, registered from XML (D-83): you (by SID), **S4U** ("Do not store password"), not elevated; **at startup + 1 min** and **every 5 min, indefinitely**; **no time limit**; **do not start a new instance**; normal priority (Task Scheduler's default is below normal, and the games would inherit it); runs `<root>\python\pythonw.exe -I <root>\launcher.py --data <data>`. `-SkipTask` leaves it out (no admin needed; dev PC tests) | Re-registered with the same definition; a running launcher keeps running |
| Last | Prints the `tailscale serve` command, `Start-ScheduledTask` and `verify.ps1` | Same |

Nothing in it adds a firewall rule, a port forward or a network-profile change.

### Watchdog (R-06)

The every-5-minutes trigger on `Basecamp start` **is** the watchdog; there's no second task (D-81). The task's action is the launcher, so a task instance lives exactly as long as the launcher. With "do not start a new instance" the 5-minute trigger does nothing while it runs, and starts it again within 5 minutes once it has died; `launcher.lock` stops a second launcher started any other way. Basecamp itself dying is the launcher's job (restart with back-off, [07-updates.md](07-updates.md)). A separate watchdog task would run the same action with the same account, and disabling or deleting `Basecamp start` should stop both anyway.

### `add-game-firewall-rule.ps1`

`-Exe -Port -Protocol [-Name] [-Replace]`: one inbound allow in group **Basecamp**, for that program only, that protocol and port(s) only (`-Port 15636,15637`, at most 10, no ranges), remote **`100.64.0.0/10`** only, all profiles. Run it once per protocol. Refused: a path that isn't a full path to an existing `.exe`; a shell or script runtime (python, java, node, powershell, cmd, ...) or anything under the Windows folder; Basecamp's own Python; ports below 1024; a rule with the same name that allows something else (unless `-Replace`). The same rule again changes nothing (D-85). Other software on the host PC may alert when firewall rules change; that's expected after this.

## After installing: verify

`scripts\verify.ps1` only reads, needs no admin, and exits with the number of FAILs (D-86):

- `<root>\python` has 3.13.17 and no `._pth`; `current.txt` names an unpacked version.
- Basecamp listens on `127.0.0.1:<port>` only, from its own Python; every listening TCP port is listed, with the ones that weren't there before the install marked `new`.
- Each rule in group Basecamp is an inbound allow for one program, specific ports, remote `100.64.0.0/10`. **No** inbound allow rule exists for Basecamp's own Python (Windows adds "Query User" rules, any port, if someone answers its "allow this app?" prompt). Enabled inbound allow rules outside the group that appeared since the install are listed.
- Each network profile is what it was before the install.
- The task's account, S4U, run level, triggers, time limit, instances, priority and action.
- `/healthz` answers with the version in `current.txt`; one launcher runs; each game host and its game are listed.

The same checks by hand:

```powershell
# Basecamp on 127.0.0.1:8150 only, and nothing else on the PC changed
Get-NetTCPConnection -State Listen | Select LocalAddress,LocalPort,@{n='Process';e={(Get-Process -Id $_.OwningProcess).ProcessName}} | Sort LocalPort
Get-NetFirewallRule -Group Basecamp | Select DisplayName,Direction,Action,Enabled
Get-NetConnectionProfile
schtasks /Query /TN "Basecamp start"
```

Then reboot the PC **without logging in** and check from your phone that Basecamp and the on-boot servers come up.

**Watch out:** when Basecamp starts a game it checks the game's ports are free by binding them on `0.0.0.0` (`basecamp/procs.py`, D-19). On the dev PC that made Windows show its "allow this app?" prompt, and answering it added two "Python" allow rules (TCP and UDP, any port, Private profile) for Basecamp's `pythonw.exe`. Until that check stops binding a non-loopback address, answer **Cancel** if that prompt appears on the host PC, and run `verify.ps1` after the first game start.

## Removing Basecamp

```powershell
scripts\uninstall.ps1 -WhatIf
scripts\uninstall.ps1              # keeps C:\BasecampData
scripts\uninstall.ps1 -RemoveData  # also removes it
```

It disables the task (so the 5-minute trigger can't bring it back), ends the launcher and then Basecamp, **each by its PID and never as a tree**, removes the task and the program folder. Game servers and their hosts keep running (R-04a): it never uses `Stop-ScheduledTask`, which ends the processes in the task's job and could take the hosts with it if breakaway didn't work (Q-10). A running game's host runs from `<root>\python\`, so that folder stays until those games have stopped; run it again then. The data folder stays unless `-RemoveData`, and also while a host still writes its logs there (D-84). Firewall rules in group Basecamp and `tailscale serve` are left alone; it prints how to remove them.

## Testing the scripts on the dev PC

Against a test folder, with a release built by `scripts\build_release.py --test-key` and a throwaway key (never the real one):

```powershell
scripts\install.ps1 -Root C:\BasecampTest -DataRoot C:\BasecampTestData -ReleaseZip <zip> -PublicKey <throwaway .pub> -Port 8156 -SkipTask
```

Then start the launcher the way the task does (`C:\BasecampTest\python\pythonw.exe -I C:\BasecampTest\launcher.py --data C:\BasecampTestData`), and run `verify.ps1` and `uninstall.ps1` with the same `-Root`/`-DataRoot`. `tests\test_install_scripts.py` covers the release check and the scripts' `-WhatIf` runs.
