<#
.SYNOPSIS
Checks a Basecamp install on the host PC (docs/04-deployment.md, "After installing").

.DESCRIPTION
Only reads; changes nothing. Run it in an admin PowerShell: the task runs Basecamp in its
own logon session ("Do not store password"), and a normal PowerShell can't read those
processes' paths or command lines, so the launcher and port checks come out as WARN there.

    powershell -ExecutionPolicy Bypass -File scripts\verify.ps1

Checks:
- Python: <Root>\python has the pinned version and Basecamp's python313._pth, and is
  isolated: run without -I and with PYTHONPATH set elsewhere, every sys.path
  entry is under <Root> and _asyncio loads from <Root>\python (D-123).
- Release: current.txt names an unpacked version; launcher.py is version 2 or newer.
- Port: Basecamp listens on 127.0.0.1:<port> only, from its own Python; then every
  listening TCP port, with the ones that weren't there before the install marked "new".
- Firewall: each rule in group Basecamp is inbound allow, for one program (not Basecamp's
  Python), specific ports, remote 100.64.0.0/10 only; enabled inbound allow rules outside
  that group that appeared since the install are listed.
- Network profiles: the same as before the install.
- Task: there, as this account, S4U, not elevated, at startup + 1 min and every 5 min, no
  time limit, no second instance, runs <Root>\python\pythonw.exe <Root>\launcher.py.
  Read from its XML (Export-ScheduledTask, or schtasks /Query /XML where the ScheduledTasks
  cmdlets fail, as they do on a PC whose task cache has entries without files).
- /healthz answers with the version in current.txt; the launcher and game hosts running.

"Before the install" is <DataRoot>\install-baseline.json, written by install.ps1.
Exit code: the number of FAILs.
#>
param(
    [string]$Root = 'C:\Basecamp',
    [string]$DataRoot = 'C:\BasecampData',
    [string]$TaskName = 'Basecamp start',
    [int]$Port = 0
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$PythonVersion = '3.13.17'  # the version install.ps1 pins
$PthName = 'python313._pth'
$MinLauncher = 2
$Remote = '100.64.0.0/10'
$RemoteForms = @('100.64.0.0/10', '100.64.0.0/255.192.0.0')  # Windows reports the mask form
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
$DataRoot = [IO.Path]::GetFullPath($DataRoot).TrimEnd('\')
$PyDir = Join-Path $Root 'python'
$script:fails = 0
$script:warns = 0

function Head([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Pass([string]$Text) { Write-Host "[PASS] $Text" -ForegroundColor Green }
function Warn([string]$Text) { $script:warns++; Write-Host "[WARN] $Text" -ForegroundColor Yellow }
function Fail([string]$Text) { $script:fails++; Write-Host "[FAIL] $Text" -ForegroundColor Red }
function Info([string]$Text) { Write-Host "       $Text" }
function Check([bool]$Ok, [string]$Good, [string]$Bad) { if ($Ok) { Pass $Good } else { Fail $Bad } }
function Test-Under([string]$Path, [string]$Folder) {
    $Path.StartsWith($Folder.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $Path.Equals($Folder.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
}
# schtasks.exe, for when the ScheduledTasks cmdlets fail: on a PC whose task cache has
# entries without files, they throw 0x80070002 even for one task by name.
function Invoke-Schtasks([string[]]$ArgList) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = @(& schtasks.exe @ArgList 2>&1 | ForEach-Object { "$_" }) }
    finally { $ErrorActionPreference = $saved }
    [pscustomobject]@{ Code = $LASTEXITCODE; Out = ($out -join "`n").Trim() }
}

$baseline = $null
$baseFile = Join-Path $DataRoot 'install-baseline.json'
if (Test-Path -LiteralPath $baseFile) { $baseline = Get-Content -LiteralPath $baseFile -Raw | ConvertFrom-Json }

if (-not $Port) {
    $Port = 8150
    $settings = Join-Path $DataRoot 'settings.toml'
    if (Test-Path -LiteralPath $settings) {
        $m = Select-String -LiteralPath $settings -Pattern '^\s*port\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { $Port = [int]$m.Matches[0].Groups[1].Value }
    }
}

$me = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$me).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Not an admin PowerShell: the task's processes (another logon session) can't be read, so checks on them are WARN, not PASS." -ForegroundColor Yellow
}
$procs = @(Get-CimInstance Win32_Process)
$byId = @{}
foreach ($p in $procs) { $byId[[int]$p.ProcessId] = $p }
$runtime = @($procs | Where-Object { $_.ExecutablePath -and (Test-Under $_.ExecutablePath $PyDir) })

# -- Program folder -------------------------------------------------------------------------
Head "Program folder $Root"
$py = Join-Path $PyDir 'python.exe'
if (Test-Path -LiteralPath $py) {
    $v = "$(& $py -I -c "import sys; print('%d.%d.%d' % sys.version_info[:3])")".Trim()
    Check ($v -eq $PythonVersion) "Python $v in $PyDir" "Python in $PyDir is '$v', expected $PythonVersion"
    $pth = @(Get-ChildItem -LiteralPath $PyDir -Filter '*._pth' | ForEach-Object Name)
    $pthList = if ($pth.Count) { $pth -join ', ' } else { 'none' }
    Check ($pth.Count -eq 1 -and $pth[0] -eq $PthName) "$PthName is there" "._pth files: $pthList; expected $PthName only (run install.ps1 again)"
    # Without -I, and with PYTHONPATH pointing elsewhere: only the ._pth file can keep that
    # and the registry's PythonPath out (D-123). PYTHONHOME would hide the registry: cleared.
    $saved = @($env:PYTHONPATH, $env:PYTHONHOME)
    $env:PYTHONPATH = Join-Path $env:TEMP 'basecamp-probe-path'
    $env:PYTHONHOME = $null
    try { $out = & $py -c "import sys, json, _asyncio; print(json.dumps({'path': sys.path, 'asyncio': _asyncio.__file__}))" }
    finally { $env:PYTHONPATH = $saved[0]; $env:PYTHONHOME = $saved[1] }
    if ($LASTEXITCODE) {
        Fail "Python in $PyDir fails to import _asyncio, so something outside it is on its sys.path (run install.ps1 again): $out"
    } else {
        $r = "$out" | ConvertFrom-Json
        $outside = @($r.path | Where-Object { -not ("$_" -and (Test-Under "$_" $Root)) })
        Check (-not $outside.Count) "sys.path is all under $Root ($(@($r.path).Count) entries)" "sys.path has entries outside $Root`: '$($outside -join "', '")' (the registry, PYTHONPATH or the current folder; run install.ps1 again)"
        Check (Test-Under "$($r.asyncio)" $PyDir) "_asyncio from $($r.asyncio)" "_asyncio loads from $($r.asyncio), not $PyDir"
    }
} else { Fail "No $py" }
$current = ''
if (Test-Path -LiteralPath (Join-Path $Root 'current.txt')) { $current = (Get-Content -LiteralPath (Join-Path $Root 'current.txt') -Raw).Trim() }
Check ([bool]$current -and (Test-Path -LiteralPath (Join-Path $Root "versions\$current\basecamp\__init__.py"))) `
    "current.txt: $current, unpacked in versions\$current" "current.txt names '$current', which isn't unpacked"
$launcherFile = Join-Path $Root 'launcher.py'
if (Test-Path -LiteralPath $launcherFile) {
    $m = Select-String -LiteralPath $launcherFile -Pattern '^LAUNCHER_VERSION = (\d+)' | Select-Object -First 1
    $lv = if ($m) { [int]$m.Matches[0].Groups[1].Value } else { 0 }
    Check ($lv -ge $MinLauncher) "launcher.py is version $lv" "launcher.py is version $lv; the isolated runtime needs $MinLauncher (run install.ps1 again)"
} else { Fail "No $launcherFile" }

# -- Listening ports ------------------------------------------------------------------------
Head 'Listening TCP ports'
$listen = @(Get-NetTCPConnection -State Listen | Sort-Object LocalPort, LocalAddress)
$mine = @($listen | Where-Object LocalPort -eq $Port)
if (-not $mine.Count) { Fail "Nothing listens on port $Port (is Basecamp running?)" }
foreach ($l in $mine) {
    $p = $byId[[int]$l.OwningProcess]
    $isOurs = $p -and $p.ExecutablePath -and (Test-Under $p.ExecutablePath $PyDir)
    if ($p -and -not $p.ExecutablePath -and -not $isAdmin) {
        Warn "Port $Port is held by $($p.Name) (PID $($p.ProcessId)), which this PowerShell can't read; run verify.ps1 as admin to check it's Basecamp's Python"
    }
    elseif (-not $isOurs) { Fail "Port $Port is held by $(if ($p) { $p.Name } else { "PID $($l.OwningProcess)" }), not Basecamp" }
    elseif ($l.LocalAddress -ne '127.0.0.1') { Fail "Basecamp listens on $($l.LocalAddress):$Port; it must be 127.0.0.1 only" }
    else { Pass "Basecamp listens on 127.0.0.1:$Port only" }
}
$before = @()
if ($baseline) { $before = @($baseline.listening) }
foreach ($l in $listen) {
    $p = $byId[[int]$l.OwningProcess]
    $name = if ($p) { $p.Name } else { '?' }
    $key = "$($l.LocalAddress):$($l.LocalPort)"
    $tag = if ($baseline -and $before -notcontains $key) { 'new' } else { '   ' }
    Info ("{0} {1,-40} {2,6}  {3}" -f $tag, $l.LocalAddress, $l.LocalPort, $name)
}
if (-not $baseline) { Warn "No $baseFile, so nothing to compare with (install.ps1 writes it)" }

# -- Firewall -------------------------------------------------------------------------------
Head 'Firewall group Basecamp'
$rules = @(Get-NetFirewallRule -Group 'Basecamp' -ErrorAction SilentlyContinue)
if (-not $rules.Count) { Pass 'No rules (Basecamp itself needs none; add one per game with add-game-firewall-rule.ps1)' }
foreach ($r in $rules) {
    $app = ($r | Get-NetFirewallApplicationFilter).Program
    $pf = $r | Get-NetFirewallPortFilter
    $from = @(($r | Get-NetFirewallAddressFilter).RemoteAddress) -join ','
    $ports = @($pf.LocalPort) -join ','
    $desc = "$($r.DisplayName): $($r.Direction) $($r.Action) $($pf.Protocol) $ports $app from $from, enabled $($r.Enabled)"
    $bad = @()
    if ("$($r.Direction)" -ne 'Inbound' -or "$($r.Action)" -ne 'Allow') { $bad += 'not an inbound allow' }
    if (-not $app -or $app -eq 'Any') { $bad += 'any program' }
    elseif (Test-Under $app $PyDir) { $bad += "Basecamp's own Python (tailscale serve needs no rule)" }
    if (-not $ports -or $ports -match 'Any') { $bad += 'any port' }
    if ($RemoteForms -notcontains $from) { $bad += "remote isn't $Remote only" }
    if ($bad.Count) { Fail "$desc ($($bad -join '; '))" } else { Pass $desc }
}
# Windows adds "Query User" allow rules (any port) when someone answers its "allow this
# app?" prompt for a program that bound a non-loopback address.
$own = @(Get-NetFirewallApplicationFilter | Where-Object { $_.Program -and (Test-Under ([Environment]::ExpandEnvironmentVariables($_.Program)) $PyDir) } |
    Get-NetFirewallRule | Where-Object { "$($_.Direction)" -eq 'Inbound' -and "$($_.Action)" -eq 'Allow' })
if ($own.Count) {
    Fail "$($own.Count) inbound allow rule(s) for Basecamp's own Python (Plan A needs none; remove them):"
    foreach ($r in $own) { Info "$($r.DisplayName) [$($r.Name)] profile $($r.Profile), enabled $($r.Enabled)" }
} else { Pass "No inbound allow rule for Basecamp's own Python" }
if ($baseline) {
    $was = @($baseline.inbound_allow)
    $added = @(Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True |
        Where-Object { $_.Group -ne 'Basecamp' -and $was -notcontains $_.Name })
    if ($added.Count) {
        Warn "$($added.Count) enabled inbound allow rule(s) outside group Basecamp appeared since the install:"
        foreach ($r in $added) { Info "$($r.DisplayName) (group '$($r.Group)')" }
    } else { Pass 'No other inbound allow rules appeared since the install' }
}

# -- Network profiles -----------------------------------------------------------------------
Head 'Network profiles'
$profiles = @(Get-NetConnectionProfile)
foreach ($np in $profiles) {
    $was = $null
    if ($baseline) { $was = @($baseline.profiles | Where-Object alias -eq $np.InterfaceAlias) | Select-Object -First 1 }
    $text = "$($np.InterfaceAlias): $($np.NetworkCategory)"
    if (-not $baseline) { Info $text }
    elseif (-not $was) { Warn "$text (not there before the install)" }
    elseif ($was.category -ne "$($np.NetworkCategory)") { Fail "$text (was $($was.category) before the install)" }
    else { Pass "$text (unchanged)" }
}

# -- Scheduled task -------------------------------------------------------------------------
Head "Scheduled task '$TaskName'"
# From the task's XML: Export-ScheduledTask, or schtasks.exe where the cmdlets fail.
$xmlText = $null
$viaCmdlets = $true
try { $xmlText = Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {
    $viaCmdlets = $false
    $q = Invoke-Schtasks @('/Query', '/TN', $TaskName, '/XML')
    if (-not $q.Code) { $xmlText = $q.Out }
}
if (-not $xmlText) {
    Fail "No task '$TaskName' (neither Export-ScheduledTask nor schtasks /Query has it)"
} else {
    if (-not $viaCmdlets) { Info 'The ScheduledTasks cmdlets fail here; read with schtasks /Query /XML.' }
    $doc = New-Object Xml.XmlDocument
    $doc.LoadXml($xmlText)
    # One value by path; Task Scheduler leaves out elements that have their default value.
    function X([string]$Path, [string]$Default = '', $Node = $doc.DocumentElement) {
        $xp = ($Path -split '/' | ForEach-Object { "*[local-name()='$_']" }) -join '/'
        $n = $Node.SelectSingleNode($xp)
        if ($n) { $n.InnerText.Trim() } else { $Default }
    }
    $userId = X 'Principals/Principal/UserId'
    $userOk = $userId -eq $me.User.Value -or $userId -eq $me.Name -or
              $userId -eq ($me.Name -split '\\')[-1]
    Check $userOk "Runs as $userId" "Runs as $userId, not $($me.Name)"
    $logon = X 'Principals/Principal/LogonType' 'InteractiveToken'
    Check ($logon -eq 'S4U') 'Do not store password (S4U)' "Logon type is $logon, expected S4U"
    $level = X 'Principals/Principal/RunLevel' 'LeastPrivilege'
    Check ($level -eq 'LeastPrivilege') 'Not elevated' "Run level is $level, expected LeastPrivilege"
    $boot = @($doc.SelectNodes("//*[local-name()='Triggers']/*[local-name()='BootTrigger']"))
    $bootOk = $boot.Count -eq 1 -and (X 'Delay' '' $boot[0]) -eq 'PT1M' -and (X 'Enabled' 'true' $boot[0]) -eq 'true'
    Check $bootOk 'At startup, 1 minute delay' 'No enabled at-startup trigger with a 1 minute delay'
    $rep = @($doc.SelectNodes("//*[local-name()='Triggers']/*") | Where-Object {
        (X 'Repetition/Interval' '' $_) -eq 'PT5M' -and -not (X 'Repetition/Duration' '' $_) -and
        (X 'Enabled' 'true' $_) -eq 'true' })
    Check ($rep.Count -ge 1) 'Every 5 minutes, indefinitely (watchdog)' 'No enabled every-5-minutes trigger without an end'
    $limit = X 'Settings/ExecutionTimeLimit' 'PT72H'
    Check ($limit -eq 'PT0S') 'No time limit' "Time limit is $limit, expected none (PT0S)"
    $multi = X 'Settings/MultipleInstancesPolicy' 'IgnoreNew'
    Check ($multi -eq 'IgnoreNew') 'No second instance' "Multiple instances: $multi, expected IgnoreNew"
    $prio = [int](X 'Settings/Priority' '7')
    Check ($prio -ge 4 -and $prio -le 6) "Priority $prio (normal)" "Priority $($prio): below normal, and the game servers would inherit it"
    Check ((X 'Settings/Enabled' 'true') -eq 'true') 'Enabled' 'Disabled'
    $exe = X 'Actions/Exec/Command'
    $arguments = X 'Actions/Exec/Arguments'
    $wantExe = Join-Path $PyDir 'pythonw.exe'
    Check ($exe -eq $wantExe -and $arguments -match [regex]::Escape("$Root\launcher.py")) `
        "Runs $exe $arguments" "Runs '$exe $arguments', expected $wantExe ... $Root\launcher.py"
    if ($viaCmdlets) {
        try {
            $tinfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
            Info "State $((Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State), last run $($tinfo.LastRunTime), last result $($tinfo.LastTaskResult)"
        } catch { Info "State: Get-ScheduledTaskInfo failed ($($_.Exception.Message))" }
    } else {
        $q = Invoke-Schtasks @('/Query', '/TN', $TaskName, '/FO', 'LIST', '/V')
        foreach ($line in @($q.Out -split "`n" | Where-Object { $_ -match '^(Status|Last Run Time|Last Result|Scheduled Task State):' })) { Info $line.Trim() }
    }
}

# -- Running --------------------------------------------------------------------------------
Head 'Running'
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 5 -UseBasicParsing
    Check ($h.ok -and $h.version -eq $current) "/healthz: ok, version $($h.version)" "/healthz answered $($h | ConvertTo-Json -Compress), current.txt says $current"
} catch {
    Fail "/healthz on 127.0.0.1:$Port doesn't answer: $($_.Exception.Message)"
}
$launchers = @($runtime | Where-Object { "$($_.CommandLine)" -match 'launcher\.py' })
$unreadable = @($procs | Where-Object { -not $_.ExecutablePath -and $_.Name -match '^pythonw?\.exe$' })
if (-not $isAdmin -and -not $launchers.Count -and $unreadable.Count) {
    Warn "Can't see the launcher or game hosts from a normal PowerShell ($($unreadable.Count) Python process(es) can't be read); run verify.ps1 as admin"
} else {
    Check ($launchers.Count -eq 1) "Launcher running (PID $(@($launchers | ForEach-Object ProcessId) -join ', '))" "$($launchers.Count) launcher(s) running, expected 1"
}
$hosts = @($runtime | Where-Object { "$($_.CommandLine)" -match 'host-[0-9a-f]+\.py' })
Info "$($hosts.Count) game server host(s) running"
foreach ($hp in $hosts) {
    $games = @($procs | Where-Object ParentProcessId -eq $hp.ProcessId)
    Info "  host PID $($hp.ProcessId): game $(@($games | ForEach-Object { "$($_.Name) PID $($_.ProcessId)" }) -join ', ')"
}

Write-Host ''
Write-Host "$script:fails FAIL, $script:warns WARN"
exit $script:fails
