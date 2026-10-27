<#
.SYNOPSIS
Installs Basecamp on the host PC, or brings an install up to date (docs/04-deployment.md).

.DESCRIPTION
Run it in an admin PowerShell (the scheduled task needs that); Basecamp itself runs as you,
not elevated. Safe to run again: each step checks what's there and only changes what's
missing or different. Add -WhatIf to see every change without making any.

    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -WhatIf
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1

Steps:
1. Checks: admin, not SYSTEM, the folders are Basecamp's (or empty), the port is free.
2. Folders: <Root> (program, default C:\Basecamp) and <DataRoot> (data, C:\BasecampData),
   with default permissions.
3. Python: the official embeddable Python 3.13 from python.org, pinned below and checked
   against its SHA-256, into <Root>\python\. Its ._pth file is removed so the launcher's
   PYTHONPATH works. Skipped if that version is already there. Replacing it is refused
   while anything runs from it (Basecamp or a game server's host process).
4. Release: the latest GitHub release (or -ReleaseZip, with its .sig next to it). Its
   signature is checked against basecamp\release_key.pub from this checkout (or
   -PublicKey), then the manifest and every file, before it becomes versions\<v>\ and
   current.txt (scripts\install_release.py). launcher.py comes out of the release.
   If Basecamp is already installed it updates itself from the page, so only launcher.py
   is refreshed from the current version.
5. A starter <DataRoot>\settings.toml, only if there isn't one, and a record of the PC's
   network setup before Basecamp (install-baseline.json) for scripts\verify.ps1.
6. The scheduled task "Basecamp start": as you, "Do not store password" (S4U), not
   elevated; at startup with a 1 minute delay and every 5 minutes (the watchdog, R-06:
   with "do not start a new instance" it only starts the launcher when it isn't running);
   no time limit; normal priority. It runs <Root>\python\pythonw.exe -I <Root>\launcher.py.
7. Prints the tailscale serve command for you to run. Nothing here opens a firewall port.

-SkipTask leaves the task out (no admin needed): for testing on the dev PC.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Root = 'C:\Basecamp',
    [string]$DataRoot = 'C:\BasecampData',
    # A local release zip (with <zip>.sig next to it) instead of GitHub's latest.
    [string]$ReleaseZip,
    # The release public key; default: basecamp\release_key.pub in this checkout.
    [string]$PublicKey,
    # A local copy of the pinned embeddable Python zip instead of downloading it.
    [string]$PythonZip,
    [string]$Repo = 'dwyer94/basecamp',
    [int]$Port = 8150,
    [string]$TaskName = 'Basecamp start',
    [switch]$SkipTask
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest's progress bar is very slow in 5.1
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor
    [Net.SecurityProtocolType]::Tls12

# The pinned runtime (python.org, Windows embeddable package, 64-bit). 3.13.17 is the last
# 3.13 release with Windows binaries. Change all three together.
$PythonVersion = '3.13.17'
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$PythonSha256 = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'

$Cmd = $PSCmdlet
$DryRun = [bool]$WhatIfPreference
# Load the modules now: loaded under -WhatIf, they print a "What if" line per alias.
$WhatIfPreference = $false
Import-Module NetTCPIP, NetSecurity, NetConnection, ScheduledTasks, CimCmdlets
$WhatIfPreference = $DryRun
$Repo = $Repo.Trim()
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
$DataRoot = [IO.Path]::GetFullPath($DataRoot).TrimEnd('\')
$PyDir = Join-Path $Root 'python'
$Py = Join-Path $PyDir 'python.exe'
$Pyw = Join-Path $PyDir 'pythonw.exe'
$Helper = Join-Path $PSScriptRoot 'install_release.py'
if (-not $PublicKey) { $PublicKey = Join-Path (Split-Path $PSScriptRoot) 'basecamp\release_key.pub' }

function Say([string]$Text) { Write-Host $Text }
function Head([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }

# Every change goes through here: with -WhatIf it's printed instead of done.
function Change([string]$What, [scriptblock]$Do) {
    if ($Cmd.ShouldProcess($What, $What, 'Basecamp install')) { Say "-> $What"; & $Do }
}

function Test-Under([string]$Path, [string]$Folder) {
    $Path.StartsWith($Folder.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $Path.Equals($Folder.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
}

# Processes running from Basecamp's Python: the launcher, Basecamp and game server hosts.
function Get-RuntimeProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and (Test-Under $_.ExecutablePath $PyDir)
    })
}

function Get-PythonVersion([string]$Exe) {
    if (-not (Test-Path -LiteralPath $Exe)) { return '' }
    # No 2> redirect: with ErrorActionPreference Stop, PowerShell 5.1 turns stderr into an error.
    $out = & $Exe -I -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    if ($LASTEXITCODE) { return '' }
    return "$out".Trim()
}

function Get-Current {
    $file = Join-Path $Root 'current.txt'
    if (-not (Test-Path -LiteralPath $file)) { return '' }
    $v = (Get-Content -LiteralPath $file -Raw).Trim()
    if ($v -and (Test-Path -LiteralPath (Join-Path $Root "versions\$v\basecamp\__init__.py"))) { return $v }
    return ''
}

function Invoke-Helper([string[]]$HelperArgs) {
    $lines = & $Py -I $Helper @HelperArgs
    $out = @{}
    foreach ($line in @($lines)) {
        $i = "$line".IndexOf('=')
        if ($i -gt 0) { $out["$line".Substring(0, $i)] = "$line".Substring($i + 1) }
    }
    if ($LASTEXITCODE) {
        $why = if ($out.ContainsKey('error')) { $out['error'] } else { ($lines -join ' ') }
        throw "Release check failed: $why"
    }
    return $out
}

function Write-Utf8([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding $false))
}

if ($DryRun) { Say 'Dry run (-WhatIf): nothing below is changed; each "What if" line is a change a real run would make.' }

# -- 1. Checks ------------------------------------------------------------------------------
Head 'Checks'
$me = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$me).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Say "Account: $($me.Name) (Basecamp will run as this account, not elevated)"
if ($me.User.Value -eq 'S-1-5-18') { throw 'Run this as your own account, not SYSTEM' }
if (-not $isAdmin -and -not $SkipTask) {
    if ($DryRun) { Say 'Not an admin PowerShell: a real run needs one for the scheduled task (or -SkipTask).' }
    else { throw 'Run this in an admin PowerShell (the scheduled task needs it), or add -SkipTask' }
}
if ((Test-Under $DataRoot $Root) -or (Test-Under $Root $DataRoot)) {
    throw "The program folder ($Root) and data folder ($DataRoot) must be separate folders"
}
if (Test-Path -LiteralPath $Root) {
    $ours = @('python', 'versions', 'launcher.py', 'current.txt', 'logs', 'downloads')
    $other = @(Get-ChildItem -LiteralPath $Root -Force | Where-Object {
        $ours -notcontains $_.Name -and $_.Name -notmatch '\.(txt|json|lock|tmp|new|old)$|^python\.'
    })
    if (-not (Test-Path -LiteralPath (Join-Path $Root 'launcher.py')) -and
            -not (Test-Path -LiteralPath (Join-Path $Root 'current.txt')) -and $other.Count) {
        throw "$Root has other files in it ($($other[0].Name), ...) and isn't a Basecamp folder; pick another -Root"
    }
}
if (-not (Test-Path -LiteralPath $PublicKey) -and -not (Get-Current)) {
    throw "No release public key at $PublicKey. The owner makes it with scripts\new-signing-key.ps1 on the dev PC"
}
$listen = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
foreach ($l in $listen) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId = $($l.OwningProcess)" -ErrorAction SilentlyContinue
    if (-not $p -or -not $p.ExecutablePath -or -not (Test-Under $p.ExecutablePath $PyDir)) {
        $name = if ($p) { $p.Name } else { "PID $($l.OwningProcess)" }
        throw "Port $Port is already used by $name; pick another with -Port"
    }
}
Say "Port $Port is free (or Basecamp's own)."

# -- 2. Folders -----------------------------------------------------------------------------
Head 'Folders'
foreach ($dir in @($Root, $DataRoot, (Join-Path $Root 'logs'))) {
    if (Test-Path -LiteralPath $dir) { Say "$dir is there." }
    else { Change "Create folder $dir" { New-Item -ItemType Directory -Path $dir | Out-Null } }
}

# -- 3. Python ------------------------------------------------------------------------------
Head "Python $PythonVersion"
$havePy = Get-PythonVersion $Py
$pth = @(if (Test-Path -LiteralPath $PyDir) { Get-ChildItem -LiteralPath $PyDir -Filter '*._pth' })
if ($havePy -eq $PythonVersion -and -not $pth.Count) {
    Say "$PyDir has Python $PythonVersion."
} else {
    if ($havePy) { Say "$PyDir has Python $havePy; replacing it with $PythonVersion." }
    $busy = @(Get-RuntimeProcesses)
    if ($busy.Count) {
        $list = ($busy | ForEach-Object { "$($_.ProcessId) $($_.CommandLine)" }) -join "`n  "
        throw "Can't replace $PyDir while these run from it (Basecamp, or a game server's host):`n  $list`nStop the servers from Basecamp's page, then Disable-ScheduledTask '$TaskName' and end the launcher, and run this again"
    }
    if ($PythonZip) {
        $zip = [IO.Path]::GetFullPath($PythonZip)
        Say "Using $zip"
    } else {
        $zip = Join-Path $Root "downloads\python-$PythonVersion-embed-amd64.zip"
        if (-not (Test-Path -LiteralPath $zip)) {
            Change "Download $PythonUrl (about 11 MB) to $zip" {
                New-Item -ItemType Directory -Force -Path (Split-Path $zip) | Out-Null
                Invoke-WebRequest -Uri $PythonUrl -OutFile "$zip.part" -UseBasicParsing
                Move-Item -LiteralPath "$zip.part" -Destination $zip -Force
            }
        }
    }
    if (Test-Path -LiteralPath $zip) {
        $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne $PythonSha256) {
            throw "$zip has SHA-256 $hash, expected $PythonSha256 (python.org's for $PythonVersion); not using it"
        }
        Say "SHA-256 matches python.org's ($PythonSha256)."
    }
    Change "Unpack Python $PythonVersion into $PyDir (replacing what's there) and remove its ._pth file" {
        $new = "$PyDir.new"
        if (Test-Path -LiteralPath $new) { Remove-Item -LiteralPath $new -Recurse -Force }
        Expand-Archive -LiteralPath $zip -DestinationPath $new
        # The ._pth file makes Python ignore PYTHONPATH, which the launcher uses (D-56).
        Get-ChildItem -LiteralPath $new -Filter '*._pth' | Remove-Item -Force
        $probe = Join-Path $new 'probe-path'
        $oldPath = $env:PYTHONPATH
        $env:PYTHONPATH = $probe
        try {
            $check = & (Join-Path $new 'python.exe') -s -c "import sys, sqlite3, ssl, ctypes, zipfile, tomllib; print(sys.version.split()[0]); print(r'$probe' in sys.path)"
        } finally { $env:PYTHONPATH = $oldPath }
        if ($LASTEXITCODE -or @($check)[0] -ne $PythonVersion -or @($check)[1] -ne 'True') {
            throw "The unpacked Python didn't pass its check: $check"
        }
        if (Test-Path -LiteralPath $PyDir) { Remove-Item -LiteralPath $PyDir -Recurse -Force }
        Move-Item -LiteralPath $new -Destination $PyDir
        Say "Python $PythonVersion is in $PyDir."
    }
}

# -- 4. Release and launcher ----------------------------------------------------------------
Head 'Basecamp release'
$current = Get-Current
if ($current) {
    Say "Basecamp $current is installed; it updates itself from the page. Refreshing launcher.py from versions\$current."
    if ($ReleaseZip) { Say "-ReleaseZip is ignored: Basecamp is already installed." }
    Change "Copy launcher.py from $Root\versions\$current to $Root (if it differs)" {
        $r = Invoke-Helper @('--root', $Root)
        Say "launcher.py $($r['launcher'])."
    }
} else {
    if (Test-Path -LiteralPath $PublicKey) {
        $keyLine = @(Get-Content -LiteralPath $PublicKey | Where-Object { $_.Trim() -and -not $_.StartsWith('#') })[0]
        Say "Release public key ($PublicKey): $keyLine"
        Say 'It must match the one on the dev PC (basecamp\release_key.pub there).'
    }
    if ($ReleaseZip) {
        $zip = [IO.Path]::GetFullPath($ReleaseZip)
        $sig = "$zip.sig"
        foreach ($f in @($zip, $sig)) { if (-not (Test-Path -LiteralPath $f)) { throw "$f isn't there" } }
        Say "Using $zip"
    } else {
        $api = "https://api.github.com/repos/$Repo/releases/latest"
        $release = Invoke-RestMethod -Uri $api -UseBasicParsing -Headers @{
            'User-Agent' = 'basecamp-install'; 'Accept' = 'application/vnd.github+json' }
        $asset = @($release.assets | Where-Object { $_.name -match '^basecamp-[0-9][0-9A-Za-z.-]*\.zip$' })
        if ($asset.Count -ne 1) { throw "Release $($release.tag_name) has no single basecamp-<version>.zip" }
        $sigAsset = @($release.assets | Where-Object { $_.name -eq "$($asset[0].name).sig" })
        if ($sigAsset.Count -ne 1) { throw "Release $($release.tag_name) has no $($asset[0].name).sig" }
        $zip = Join-Path $Root "downloads\$($asset[0].name)"
        $sig = "$zip.sig"
        Say "Latest release: $($release.tag_name)"
        Change "Download $($asset[0].name) ($([math]::Round($asset[0].size / 1MB, 1)) MB) and its .sig from GitHub to $Root\downloads" {
            New-Item -ItemType Directory -Force -Path (Split-Path $zip) | Out-Null
            Invoke-WebRequest -Uri $asset[0].browser_download_url -OutFile $zip -UseBasicParsing
            Invoke-WebRequest -Uri $sigAsset[0].browser_download_url -OutFile $sig -UseBasicParsing
        }
    }
    if ($DryRun -and (Get-PythonVersion $Py)) {
        $r = Invoke-Helper @('--check', '--zip', $zip, '--sig', $sig, '--key', $PublicKey)
        Say "Signature and manifest check out: Basecamp $($r['version'])."
    }
    Change "Check the release's signature and files, unpack it to $Root\versions\, copy launcher.py out and write current.txt" {
        $r = Invoke-Helper @('--zip', $zip, '--sig', $sig, '--key', $PublicKey, '--root', $Root)
        Say "Basecamp $($r['version']) $($r['result'])."
    }
}

# -- 5. Settings and the network baseline ---------------------------------------------------
Head 'Settings'
$settings = Join-Path $DataRoot 'settings.toml'
if (Test-Path -LiteralPath $settings) {
    Say "$settings is there; leaving it as it is."
} else {
    $steamcmd = Join-Path $env:USERPROFILE 'Desktop\SteamCmd\steamcmd.exe'
    $steamLine = if (Test-Path -LiteralPath $steamcmd) { "steamcmd_path = '$steamcmd'" }
                 else { "# steamcmd_path = 'C:\path\to\steamcmd.exe'" }
    $text = @"
# Basecamp settings. Most of these are also on the page's Settings screen.
[basecamp]
port = $Port
# The address friends join on (this PC's Tailscale IP):
# join_host = "100.x.y.z"
# Your Tailscale login, so the page knows you're the admin from other devices:
# admin_logins = ["you@example.com"]
$steamLine
"@
    Change "Write a starter $settings (port $Port)" { Write-Utf8 $settings ($text.Replace("`r`n", "`n")) }
}

$baseline = Join-Path $DataRoot 'install-baseline.json'
if (Test-Path -LiteralPath $baseline) {
    Say "$baseline is there (the network before Basecamp); keeping it."
} else {
    Change "Record the network profiles, inbound allow rules and listening ports in $baseline (read by verify.ps1)" {
        $snap = [ordered]@{
            taken = (Get-Date).ToString('s')
            profiles = @(Get-NetConnectionProfile | ForEach-Object {
                [ordered]@{ alias = $_.InterfaceAlias; category = "$($_.NetworkCategory)" } })
            inbound_allow = @(Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True |
                ForEach-Object { $_.Name } | Sort-Object)
            listening = @(Get-NetTCPConnection -State Listen | ForEach-Object {
                "$($_.LocalAddress):$($_.LocalPort)" } | Sort-Object -Unique)
        }
        Write-Utf8 $baseline ($snap | ConvertTo-Json -Depth 4)
    }
}

# -- 6. Scheduled task ----------------------------------------------------------------------
Head "Scheduled task '$TaskName'"
function X([string]$s) { [Security.SecurityElement]::Escape($s) }
$launcherArgs = "-I `"$Root\launcher.py`" --data `"$DataRoot`""
# Priority 4 = normal. Task Scheduler's default (7) is below normal, and Windows hands a
# below-normal priority class down to child processes: the game servers would get it too.
$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts Basecamp's launcher at startup, and again within 5 minutes if it stops (R-06). Game servers keep running if this task ends.</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT1M</Delay>
    </BootTrigger>
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(X $me.User.Value)</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$(X $Pyw)</Command>
      <Arguments>$(X $launcherArgs)</Arguments>
      <WorkingDirectory>$(X $Root)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
if ($SkipTask) {
    Say "-SkipTask: not creating it. It would run: $Pyw $launcherArgs"
} else {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($DryRun) { Say 'The task definition:'; Say $taskXml }
    $verb = if ($existing) { 'Replace' } else { 'Create' }
    Change "$verb scheduled task '$TaskName' as $($me.Name) (S4U, not elevated): at startup + 1 min and every 5 min, no time limit, no second instance" {
        Register-ScheduledTask -TaskName $TaskName -Xml $taskXml -Force | Out-Null
        Say "Task '$TaskName' is registered. A running launcher keeps running."
    }
}

# -- 7. What's left for you -----------------------------------------------------------------
Head 'Next'
if (-not $SkipTask) { Say "Start Basecamp now (or reboot):  Start-ScheduledTask -TaskName '$TaskName'" }
Say "Check it:                        scripts\verify.ps1 -Root '$Root' -DataRoot '$DataRoot'"
Say "Publish it on the tailnet (run it yourself; this script doesn't):"
Say "    tailscale serve --bg --https=443 http://127.0.0.1:$Port"
Say 'Game ports, one rule per game exe and port, only if the game has none yet:'
Say '    scripts\add-game-firewall-rule.ps1 -Exe <game server exe> -Port <port> -Protocol UDP'
if ($DryRun) { Say ''; Say 'Dry run: nothing was changed.' }
