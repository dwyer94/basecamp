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
   against its SHA-256, into <Root>\python\, with Basecamp's python313._pth in it: Python
   then ignores the registry, PYTHONPATH, PYTHONHOME and the user's site-packages, so only
   <Root>\python\ is on its sys.path (D-123). Then a check that it is. Skipped if that
   version is already there and isolated. Replacing it is refused while anything runs
   from it (Basecamp or a game server's host process).
4. Release: the latest GitHub release (or -ReleaseZip, with its .sig next to it). Its
   signature is checked against basecamp\release_key.pub from this checkout (or
   -PublicKey), then the manifest and every file, before it becomes versions\<v>\ and
   current.txt (scripts\install_release.py). launcher.py comes out of the release.
   If Basecamp is already installed it updates itself from the page, so only launcher.py
   is refreshed from the current version. Unless that version's launcher is too old for
   the isolated runtime (launcher 1, Basecamp 0.2.0 and older): then the release is
   installed the way an update is (DB copy, the old version kept to roll back to).
   An install from before the ._pth file gets it written after that (step 4b). Both need
   the launcher and Basecamp stopped first (game servers can keep running).
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
# The runtime's ._pth file (D-123): only its own zip and folder on sys.path, no site.
$PthName = 'python313._pth'
$PthText = "# Written by Basecamp's install.ps1 (D-123). With this file Python ignores the registry,`n" +
           "# PYTHONPATH, PYTHONHOME and user site-packages. Don't remove it.`npython313.zip`n.`n"
# The oldest launcher that runs Basecamp on that runtime (LAUNCHER_VERSION in launcher.py).
$MinLauncher = 2

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

# The launcher and Basecamp: everything from Basecamp's Python but the game server hosts.
function Get-LauncherProcesses {
    @(Get-RuntimeProcesses | Where-Object { "$($_.CommandLine)" -notmatch 'host-[0-9a-f]+\.py|\\host\.py' })
}

function Get-PythonVersion([string]$Exe) {
    if (-not (Test-Path -LiteralPath $Exe)) { return '' }
    # No 2> redirect: with ErrorActionPreference Stop, PowerShell 5.1 turns stderr into an error.
    $out = & $Exe -I -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    if ($LASTEXITCODE) { return '' }
    return "$out".Trim()
}

function Get-LauncherVersion([string]$File) {
    if (-not (Test-Path -LiteralPath $File)) { return 0 }
    $m = Select-String -LiteralPath $File -Pattern '^LAUNCHER_VERSION = (\d+)' | Select-Object -First 1
    if ($m) { return [int]$m.Matches[0].Groups[1].Value }
    return 0
}

# Is the runtime in $Dir isolated (D-123)? Run without -I and with PYTHONPATH pointing
# elsewhere, so only its ._pth file can keep the registry's PythonPath and PYTHONPATH out.
# PYTHONHOME is cleared for it: set, it would hide the registry's entries. Returns what's
# wrong (nothing if it's fine).
$IsoCheck = @'
import sys, json, sqlite3, ssl, ctypes, zipfile, tomllib, _asyncio; print(json.dumps({'version': '%d.%d.%d' % sys.version_info[:3], 'path': sys.path, 'asyncio': _asyncio.__file__}))
'@
function Test-Runtime([string]$Dir) {
    $saved = @($env:PYTHONPATH, $env:PYTHONHOME)
    $env:PYTHONPATH = Join-Path $env:TEMP 'basecamp-probe-path'
    $env:PYTHONHOME = $null
    try { $out = & (Join-Path $Dir 'python.exe') -c $IsoCheck }
    finally { $env:PYTHONPATH = $saved[0]; $env:PYTHONHOME = $saved[1] }
    if ($LASTEXITCODE) { return @("it doesn't start or can't import its modules (its error is above): $out") }
    $r = "$out" | ConvertFrom-Json
    $bad = @()
    if ($r.version -ne $PythonVersion) { $bad += "it's version $($r.version), expected $PythonVersion" }
    $outside = @($r.path | Where-Object { -not ("$_" -and (Test-Under "$_" $Dir)) })
    if ($outside.Count) { $bad += "sys.path has entries outside $Dir`: '$($outside -join "', '")'" }
    if (-not (Test-Under "$($r.asyncio)" $Dir)) { $bad += "_asyncio comes from $($r.asyncio)" }
    return $bad
}

function Write-Pth([string]$Dir) {
    Get-ChildItem -LiteralPath $Dir -Filter '*._pth' | Where-Object Name -ne $PthName | Remove-Item -Force
    Write-Utf8 (Join-Path $Dir $PthName) $PthText
}

function Test-Pth([string]$Dir) {
    $file = Join-Path $Dir $PthName
    $others = @(Get-ChildItem -LiteralPath $Dir -Filter '*._pth' | Where-Object Name -ne $PthName)
    (Test-Path -LiteralPath $file) -and -not $others.Count -and
        ([IO.File]::ReadAllText($file) -eq $PthText)
}

# schtasks.exe, for when the ScheduledTasks cmdlets fail: on a PC whose task cache has
# entries without files, Get-ScheduledTask throws 0x80070002 even for one task by name.
function Invoke-Schtasks([string[]]$ArgList) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = @(& schtasks.exe @ArgList 2>&1 | ForEach-Object { "$_" }) }
    finally { $ErrorActionPreference = $saved }
    [pscustomobject]@{ Code = $LASTEXITCODE; Out = ($out -join "`n").Trim() }
}

function Test-Task([string]$Name) {
    try { return [bool](Get-ScheduledTask -TaskName $Name -ErrorAction Stop) }
    catch { return -not (Invoke-Schtasks @('/Query', '/TN', $Name)).Code }
}

$StopHint = "Stop them first (game servers can keep running), in this admin PowerShell:`n" +
    "  schtasks /Change /TN `"$TaskName`" /DISABLE`n  schtasks /End /TN `"$TaskName`"`n" +
    "then wait a few seconds for Basecamp to exit, and run this again (it enables the task again).`n" +
    "Without the task (-SkipTask), end the launcher process listed above instead."

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
# An install from before D-123 has the right version but no ._pth: it's fixed in step 4b,
# once launcher.py is one that doesn't need PYTHONPATH.
$repairPth = $false
if ($havePy -eq $PythonVersion) {
    $bad = @(Test-Runtime $PyDir)
    if ((Test-Pth $PyDir) -and -not $bad.Count) {
        Say "$PyDir has Python $PythonVersion, isolated (only $PyDir on its sys.path)."
    } else {
        $repairPth = $true
        Say "$PyDir has Python $PythonVersion but isn't isolated: $(if ($bad.Count) { $bad -join '; ' } else { "no $PthName as Basecamp writes it" })."
        Say "Its $PthName is written in step 4b."
    }
} else {
    if ($havePy) { Say "$PyDir has Python $havePy; replacing it with $PythonVersion." }
    $busy = @(Get-RuntimeProcesses)
    if ($busy.Count) {
        $list = ($busy | ForEach-Object { "$($_.ProcessId) $($_.CommandLine)" }) -join "`n  "
        throw "Can't replace $PyDir while these run from it (Basecamp, or a game server's host):`n  $list`nStop the servers from Basecamp's page, then:`n  schtasks /Change /TN `"$TaskName`" /DISABLE`n  schtasks /End /TN `"$TaskName`"`nand run this again"
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
        # Not Get-FileHash: under -WhatIf it skips reading the file and returns nothing.
        $stream = [IO.File]::OpenRead($zip)
        try { $hash = -join ([Security.Cryptography.SHA256]::Create().ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) }
        finally { $stream.Dispose() }
        if ($hash -ne $PythonSha256) {
            throw "$zip has SHA-256 $hash, expected $PythonSha256 (python.org's for $PythonVersion); not using it"
        }
        Say "SHA-256 matches python.org's ($PythonSha256)."
    }
    Change "Unpack Python $PythonVersion into $PyDir (replacing what's there) with Basecamp's $PthName" {
        $new = "$PyDir.new"
        if (Test-Path -LiteralPath $new) { Remove-Item -LiteralPath $new -Recurse -Force }
        Expand-Archive -LiteralPath $zip -DestinationPath $new
        Write-Pth $new
        $bad = @(Test-Runtime $new)
        if ($bad.Count) { throw "The unpacked Python didn't pass its check: $($bad -join '; ')" }
        if (Test-Path -LiteralPath $PyDir) { Remove-Item -LiteralPath $PyDir -Recurse -Force }
        Move-Item -LiteralPath $new -Destination $PyDir
        Say "Python $PythonVersion is in $PyDir."
    }
}

# -- 4. Release and launcher ----------------------------------------------------------------
Head 'Basecamp release'
$current = Get-Current
$upgrade = $false
if ($current) {
    $haveLauncher = Get-LauncherVersion (Join-Path $Root "versions\$current\launcher.py")
    $upgrade = $haveLauncher -lt $MinLauncher
}
if ($upgrade -or $repairPth) {
    # A launcher that sets PYTHONPATH can't start Basecamp once the ._pth is there, and a
    # running one keeps the launcher.py it read at its start: stop it before either changes.
    $running = @(Get-LauncherProcesses)
    if ($running.Count) {
        $list = ($running | ForEach-Object { "$($_.ProcessId) $($_.CommandLine)" }) -join "`n  "
        $why = "The launcher or Basecamp is running:`n  $list`n$StopHint"
        if ($DryRun) { Say "A real run would stop here. $why" } else { throw $why }
    }
}
if ($current -and -not $upgrade) {
    Say "Basecamp $current is installed; it updates itself from the page. Refreshing launcher.py from versions\$current."
    if ($ReleaseZip) { Say "-ReleaseZip is ignored: Basecamp is already installed." }
    Change "Copy launcher.py from $Root\versions\$current to $Root (if it differs)" {
        $r = Invoke-Helper @('--root', $Root, '--min-launcher', $MinLauncher)
        Say "launcher.py $($r['launcher'])."
    }
} else {
    if ($upgrade) {
        Say "Basecamp $current is installed, but its launcher (version $haveLauncher) sets PYTHONPATH, which the isolated runtime ignores; version $MinLauncher is needed."
        Say "Installing a newer release the way an update does: a copy of the DB, $current kept to roll back to, the new one on trial."
        if (-not (Test-Path -LiteralPath $PublicKey)) { throw "No release public key at $PublicKey" }
    }
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
        $r = Invoke-Helper @('--zip', $zip, '--sig', $sig, '--key', $PublicKey, '--root', $Root,
                             '--data', $DataRoot, '--min-launcher', $MinLauncher)
        if ($r['result'] -eq 'upgraded') { Say "Basecamp $($r['from']) -> $($r['version']) (on trial: the launcher rolls back if it isn't healthy in 60 s)." }
        else { Say "Basecamp $($r['version']) $($r['result'])." }
    }
}

# -- 4b. Isolate an older runtime ----------------------------------------------------------
if ($repairPth) {
    Head 'Isolate the runtime'
    Change "Write $PyDir\$PthName (Python then ignores the registry, PYTHONPATH, PYTHONHOME and user site-packages) and check it" {
        Write-Pth $PyDir
        $bad = @(Test-Runtime $PyDir)
        if ($bad.Count) { throw "$PyDir still isn't isolated: $($bad -join '; ')" }
        Say "$PyDir is isolated: only it is on Python's sys.path."
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
    $existing = Test-Task $TaskName
    if ($DryRun) { Say 'The task definition:'; Say $taskXml }
    $verb = if ($existing) { 'Replace' } else { 'Create' }
    Change "$verb scheduled task '$TaskName' as $($me.Name) (S4U, not elevated, enabled): at startup + 1 min and every 5 min, no time limit, no second instance" {
        try {
            Register-ScheduledTask -TaskName $TaskName -Xml $taskXml -Force -ErrorAction Stop | Out-Null
        } catch {
            Say "Register-ScheduledTask failed ($($_.Exception.Message)); using schtasks.exe."
            $file = Join-Path $env:TEMP "basecamp-task-$PID.xml"
            [IO.File]::WriteAllText($file, $taskXml, [Text.Encoding]::Unicode)
            try { $r = Invoke-Schtasks @('/Create', '/TN', $TaskName, '/XML', $file, '/F') }
            finally { Remove-Item -LiteralPath $file -Force }
            if ($r.Code) { throw "schtasks /Create failed: $($r.Out)" }
        }
        Say "Task '$TaskName' is registered. A running launcher keeps running."
    }
}

# -- 7. What's left for you -----------------------------------------------------------------
Head 'Next'
if (-not $SkipTask) { Say "Start Basecamp now (or reboot):  schtasks /Run /TN `"$TaskName`"" }
Say "Check it:                        scripts\verify.ps1 -Root '$Root' -DataRoot '$DataRoot'"
Say "Publish it on the tailnet (run it yourself; this script doesn't):"
Say "    tailscale serve --bg --https=443 http://127.0.0.1:$Port"
Say 'Game ports, one rule per game exe and port, only if the game has none yet:'
Say '    scripts\add-game-firewall-rule.ps1 -Exe <game server exe> -Port <port> -Protocol UDP'
if ($DryRun) { Say ''; Say 'Dry run: nothing was changed.' }
