<#
.SYNOPSIS
Removes Basecamp from the host PC, leaving every game server running (R-04a).

.DESCRIPTION
Run it in an admin PowerShell (removing the scheduled task needs that). Add -WhatIf to see
every change without making any.

    powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -WhatIf
    powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 [-RemoveData]

1. Disables the scheduled task, so its 5-minute trigger can't start Basecamp again.
2. Ends the launcher, then Basecamp: those two processes only, each by its PID, never a
   process tree and never through Stop-ScheduledTask (ending the task could take the
   processes in its job with it if breakaway didn't work, Q-10). Game servers and their
   host processes keep running.
3. Removes the task.
4. Removes the program folder. A running game server's host process uses <Root>\python\,
   so that folder stays until those servers have stopped; run this again then.
5. Keeps the data folder (servers, backups, settings, secrets) unless -RemoveData is given,
   and never removes it while a game server's host still runs (hosts write their console
   logs there).

Left alone, and printed for you: firewall rules in the Basecamp group (the games still
use them) and tailscale serve.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Root = 'C:\Basecamp',
    [string]$DataRoot = 'C:\BasecampData',
    [string]$TaskName = 'Basecamp start',
    [switch]$RemoveData
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$Cmd = $PSCmdlet
$DryRun = [bool]$WhatIfPreference
# Load the modules now: loaded under -WhatIf, they print a "What if" line per alias.
$WhatIfPreference = $false
Import-Module NetSecurity, ScheduledTasks, CimCmdlets
$WhatIfPreference = $DryRun
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
$DataRoot = [IO.Path]::GetFullPath($DataRoot).TrimEnd('\')
$PyDir = Join-Path $Root 'python'
# Left next to python\ while game hosts still run from it, so the next run knows the folder is ours.
$Marker = 'uninstall-pending.txt'

function Say([string]$Text) { Write-Host $Text }
function Head([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Change([string]$What, [scriptblock]$Do) {
    if ($Cmd.ShouldProcess($What, $What, 'Basecamp uninstall')) { Say "-> $What"; & $Do }
}
function Test-Under([string]$Path, [string]$Folder) {
    $Path.StartsWith($Folder.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)
}

# Processes from Basecamp's Python, sorted into the launcher, Basecamp and game hosts.
function Get-RuntimeProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and (Test-Under $_.ExecutablePath $PyDir)
    } | ForEach-Object {
        $line = "$($_.CommandLine)"
        $kind = if ($line -match 'launcher\.py') { 'launcher' }
                elseif ($line -match '\s-m\s+basecamp(\s|$)') { 'basecamp' }
                elseif ($line -match 'host-[0-9a-f]+\.py|\\host\.py') { 'host' }
                else { 'other' }
        [pscustomobject]@{ Kind = $kind; Id = $_.ProcessId; Line = $line }
    })
}

if ($DryRun) { Say 'Dry run (-WhatIf): nothing below is changed.' }
$me = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$me).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task -and -not $isAdmin -and -not $DryRun) {
    throw "Run this in an admin PowerShell: removing the task '$TaskName' needs it"
}
$looksOurs = (Test-Path -LiteralPath (Join-Path $Root 'launcher.py')) -or
             (Test-Path -LiteralPath (Join-Path $Root 'current.txt')) -or
             (Test-Path -LiteralPath (Join-Path $Root 'versions')) -or
             (Test-Path -LiteralPath (Join-Path $Root $Marker))
if ((Test-Path -LiteralPath $Root) -and -not $looksOurs) {
    throw "$Root doesn't look like a Basecamp program folder (no launcher.py, current.txt, versions or $Marker); not touching it"
}

$procs = @(Get-RuntimeProcesses)
$hosts = @($procs | Where-Object Kind -eq 'host')
Head 'What runs now'
if (-not $procs.Count) { Say 'Nothing runs from Basecamp''s Python.' }
foreach ($p in $procs) { Say ("{0,-9} PID {1,-6} {2}" -f $p.Kind, $p.Id, $p.Line) }
if ($hosts.Count) {
    Say "$($hosts.Count) game server(s) are running; they keep running, and their consoles go"
    Say 'quiet once Basecamp is gone. To stop one later, end its game process.'
}

# -- 1-3. Task, launcher, Basecamp -----------------------------------------------------------
Head "Scheduled task '$TaskName' and Basecamp"
if ($task) {
    Change "Disable scheduled task '$TaskName' (so it can't start the launcher again)" {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
    }
} else { Say "No task '$TaskName'." }
foreach ($kind in @('launcher', 'basecamp')) {
    foreach ($p in @($procs | Where-Object Kind -eq $kind)) {
        Change "End the $kind process (PID $($p.Id) only, not its children)" {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $p.Id -Timeout 30 -ErrorAction SilentlyContinue
        }
    }
}
if ($task) {
    Change "Remove scheduled task '$TaskName'" {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
}

# -- 4. Program folder ----------------------------------------------------------------------
Head "Program folder $Root"
if (-not (Test-Path -LiteralPath $Root)) {
    Say 'Not there.'
} else {
    $left = @(Get-RuntimeProcesses | Where-Object { $_.Kind -ne 'launcher' -and $_.Kind -ne 'basecamp' })
    if (-not $DryRun -and @(Get-RuntimeProcesses | Where-Object { $_.Kind -eq 'launcher' -or $_.Kind -eq 'basecamp' }).Count) {
        throw 'The launcher or Basecamp is still running; not removing files. Run this again'
    }
    if ($left.Count) {
        foreach ($item in @(Get-ChildItem -LiteralPath $Root -Force | Where-Object { $_.Name -ne 'python' -and $_.Name -ne $Marker })) {
            Change "Remove $($item.FullName)" { Remove-Item -LiteralPath $item.FullName -Recurse -Force }
        }
        Change "Leave $Root\$Marker, so the next run knows $PyDir is Basecamp's" {
            Set-Content -LiteralPath (Join-Path $Root $Marker) -Encoding ASCII -Value @(
                "Basecamp's Python, left because game server hosts still ran from it.",
                'Run scripts\uninstall.ps1 again once they have stopped, to remove this folder.')
        }
        Say "Keeping $PyDir`: $($left.Count) process(es) still run from it (game server hosts):"
        foreach ($p in $left) { Say "  PID $($p.Id) $($p.Line)" }
        Say 'Run uninstall.ps1 again once those servers have stopped, to remove it.'
    } else {
        Change "Remove $Root and everything in it" { Remove-Item -LiteralPath $Root -Recurse -Force }
    }
}

# -- 5. Data folder -------------------------------------------------------------------------
Head "Data folder $DataRoot"
if (-not (Test-Path -LiteralPath $DataRoot)) {
    Say 'Not there.'
} elseif (-not $RemoveData) {
    Say 'Kept (servers, backups, settings, secrets). -RemoveData removes it.'
} elseif ($hosts.Count) {
    Say "Kept: game server hosts still write their logs there. Run with -RemoveData again once they've stopped."
} elseif (-not ((Test-Path -LiteralPath (Join-Path $DataRoot 'basecamp.db')) -or
                (Test-Path -LiteralPath (Join-Path $DataRoot 'settings.toml')))) {
    Say "Kept: it has no basecamp.db or settings.toml, so it may not be Basecamp's data folder."
} else {
    Change "Remove $DataRoot and everything in it (servers, backups, settings, secrets)" {
        Remove-Item -LiteralPath $DataRoot -Recurse -Force
    }
}

# -- Left for you ---------------------------------------------------------------------------
Head 'Left as it is'
$rules = @(Get-NetFirewallRule -Group 'Basecamp' -ErrorAction SilentlyContinue)
if ($rules.Count) {
    Say "$($rules.Count) firewall rule(s) in group Basecamp (the games use them). To remove them:"
    Say "    Remove-NetFirewallRule -Group Basecamp"
}
Say 'If Basecamp was published on the tailnet, stop that with:'
Say '    tailscale serve --https=443 off'
if ($DryRun) { Say ''; Say 'Dry run: nothing was changed.' }
