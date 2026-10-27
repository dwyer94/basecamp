<#
.SYNOPSIS
Lets friends on the tailnet reach one game server: an inbound allow for one program and
its port(s), from Tailscale addresses only (docs/04-deployment.md).

.DESCRIPTION
Run it in an admin PowerShell, only for a game that has no rule yet (existing rules keep
working, since game exe paths don't change). Add -WhatIf to see the rule without adding it.

    scripts\add-game-firewall-rule.ps1 -Exe 'D:\Games\Palworld\Pal\Binaries\Win64\PalServer-Win64-Shipping-Cmd.exe' -Port 8211 -Protocol UDP
    scripts\add-game-firewall-rule.ps1 -Exe <exe> -Port 15636,15637 -Protocol UDP -Name 'Basecamp - Enshrouded'

The rule: group "Basecamp", inbound, allow, that program only, that protocol and port(s)
only, remote addresses 100.64.0.0/10 (Tailscale) only, all profiles (the network profile
isn't changed). Run it once per protocol if a game needs both TCP and UDP.

Refused, because they would be broad rules:
- no program, a program that isn't an existing .exe, or a shell or script runtime
  (python, java, node, powershell, cmd and the like), which would open the port to any
  script it runs; anything under the Windows folder or Basecamp's own Python (Basecamp is
  reached through tailscale serve and needs no rule);
- no port, ports below 1024, or more than 10 ports;
- a rule with the same name that allows something different (-Replace swaps it).

Heads-up: other software on the host PC may watch the firewall and send an alert when its
rules change. Adding a rule here can trigger that; it's expected, so note the time.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$Exe,
    # One port or several (8211, or 15636,15637); no ranges.
    [Parameter(Mandatory)][string[]]$Port,
    [Parameter(Mandatory)][ValidateSet('TCP', 'UDP')][string]$Protocol,
    [string]$Name,
    [string]$Root = 'C:\Basecamp',
    [switch]$Replace
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$Group = 'Basecamp'
$Remote = '100.64.0.0/10'
$RemoteForms = @('100.64.0.0/10', '100.64.0.0/255.192.0.0')  # Windows reports the mask form
$Runtimes = @('python', 'pythonw', 'py', 'pyw', 'java', 'javaw', 'node', 'powershell', 'pwsh',
              'cmd', 'wscript', 'cscript', 'mshta', 'rundll32', 'regsvr32', 'conhost', 'bash',
              'wsl', 'dotnet', 'msiexec', 'explorer', 'svchost')

function Say([string]$Text) { Write-Host $Text }

# -- What is asked for ----------------------------------------------------------------------
$Exe = $Exe.Trim().Trim('"')
if (-not [IO.Path]::IsPathRooted($Exe)) { throw "-Exe must be a full path to the game server's .exe: $Exe" }
$Exe = [IO.Path]::GetFullPath($Exe)
if ([IO.Path]::GetExtension($Exe) -ne '.exe') { throw "-Exe must be an .exe file: $Exe" }
if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { throw "$Exe isn't there" }
$base = [IO.Path]::GetFileNameWithoutExtension($Exe).ToLowerInvariant()
if ($Runtimes -contains $base) {
    throw "$Exe is a shell or script runtime; a rule for it would open the port to anything it runs. Use the game server's own exe"
}
$windir = [IO.Path]::GetFullPath($env:SystemRoot).TrimEnd('\') + '\'
if ($Exe.StartsWith($windir, [StringComparison]::OrdinalIgnoreCase)) {
    throw "$Exe is part of Windows; not adding a rule for it"
}
$pyDir = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\python\'
if ($Exe.StartsWith($pyDir, [StringComparison]::OrdinalIgnoreCase)) {
    throw "$Exe is Basecamp's own Python; Basecamp is reached through tailscale serve and needs no rule"
}
# powershell -File passes "15636,15637" as one string, so split it here.
$ports = @()
foreach ($part in @($Port | ForEach-Object { "$_" -split ',' })) {
    $n = 0
    if (-not [int]::TryParse($part.Trim(), [ref]$n)) { throw "Not a port number: '$part' (ranges aren't allowed; list the ports)" }
    $ports += $n
}
$ports = @($ports | Sort-Object -Unique)
if (-not $ports.Count) { throw 'Give at least one -Port' }
if ($ports.Count -gt 10) { throw "That's $($ports.Count) ports; at most 10 per rule" }
foreach ($p in $ports) {
    if ($p -lt 1024 -or $p -gt 65535) { throw "Port $p is outside 1024-65535; game servers don't need system ports" }
}
$portText = ($ports | ForEach-Object { "$_" }) -join ','
if (-not $Name) { $Name = "Basecamp - $([IO.Path]::GetFileNameWithoutExtension($Exe)) $Protocol $portText" }

Say "Rule:      $Name"
Say "Group:     $Group"
Say "Allows:    inbound $Protocol $portText to $Exe"
Say "From:      $Remote (Tailscale) only, all profiles"

# -- Already there? -------------------------------------------------------------------------
$same = @(Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue)
foreach ($r in $same) {
    $app = ($r | Get-NetFirewallApplicationFilter).Program
    $pf = $r | Get-NetFirewallPortFilter
    $af = $r | Get-NetFirewallAddressFilter
    $match = $r.Group -eq $Group -and "$($r.Direction)" -eq 'Inbound' -and "$($r.Action)" -eq 'Allow' -and
             "$($r.Enabled)" -eq 'True' -and $app -eq $Exe -and "$($pf.Protocol)" -eq $Protocol -and
             (@($pf.LocalPort) -join ',') -eq $portText -and $RemoteForms -contains (@($af.RemoteAddress) -join ',')
    if ($match -and $same.Count -eq 1) { Say 'That rule is already there; nothing to do.'; return }
    if (-not $Replace) {
        throw "A rule named '$Name' is already there and allows something different; use another -Name, or -Replace to swap it"
    }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $WhatIfPreference) { throw 'Run this in an admin PowerShell' }

if ($same.Count -and $PSCmdlet.ShouldProcess("Remove the existing rule(s) named '$Name'", "Remove the existing rule(s) named '$Name'", 'Firewall')) {
    $same | Remove-NetFirewallRule
}
$what = "Add firewall rule '$Name': inbound allow $Protocol $portText for $Exe from $Remote, group $Group, all profiles"
if ($PSCmdlet.ShouldProcess($what, $what, 'Firewall')) {
    New-NetFirewallRule -DisplayName $Name -Group $Group -Direction Inbound -Action Allow `
        -Program $Exe -Protocol $Protocol -LocalPort $ports -RemoteAddress $Remote `
        -Profile Any -Enabled True | Out-Null
    Say 'Added. If other software on this PC watches the firewall, it may send an alert now; that''s expected.'
}
