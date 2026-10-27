# 04 · Deployment on the host PC

How Basecamp is installed on the always-on Windows PC that hosts the game servers. The owner's machine-specific notes (PC name, addresses, other apps sharing the PC and their rules) live outside the repo, in `private/`.

**Claude doesn't deploy this on its own.** Each step below is run by you (or by Claude only after you say go for that step).

## What Basecamp needs

| Need | Choice | Why |
|---|---|---|
| Own Python | `C:\Basecamp\python\` (embeddable 3.13 or a venv from a separate install) | Don't share a runtime with other apps on the PC: firewall rules are per program path, so another app's rules would apply to Basecamp, and an upgrade there could break it |
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

## Install steps (sketch; the real script comes in Phase 7)

```powershell
# admin PowerShell on the host PC
scripts\install.ps1      # makes folders, copies Python + launcher, downloads latest release, creates task
tailscale serve --bg --https=443 http://127.0.0.1:8150
# only for a game that has no rule yet:
scripts\add-game-firewall-rule.ps1 -Exe <path to game server exe> -Port 8211 -Protocol UDP
```

Updates to Basecamp itself come from GitHub Releases and install from the admin page; see [07-updates.md](07-updates.md). `install.ps1` is only re-run to change the Python runtime or the launcher.

## After installing: verify

```powershell
# Basecamp on 127.0.0.1:8150 only, and nothing else on the PC changed
Get-NetTCPConnection -State Listen | Select LocalAddress,LocalPort,@{n='Process';e={(Get-Process -Id $_.OwningProcess).ProcessName}} | Sort LocalPort
Get-NetFirewallRule -Group Basecamp | Select DisplayName,Direction,Action,Enabled
Get-NetConnectionProfile
schtasks /Query /TN "Basecamp start"
```

Then reboot the PC **without logging in** and check from your phone that Basecamp and the on-boot servers come up.
