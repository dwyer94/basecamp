<#
.SYNOPSIS
Builds, signs and publishes a Basecamp release on GitHub (docs/07-updates.md).

.DESCRIPTION
    scripts\release.ps1 1.3.0             # checks, bump, tag, build, sign, ask, publish
    scripts\release.ps1 1.3.0 -NoPublish  # checks, build and sign only (no commit or tag)

1. Refuses if the git tree is dirty, not on main, not the same as origin/main, the tag
   exists, CHANGES.md has no "## <version>" section, or the tests or ruff fail.
2. Sets __version__, builds dist\basecamp-<v>.zip with the dependencies vendored in lib\,
   writes manifest.json (SHA-256 of every file) and signs the zip with
   %USERPROFILE%\.basecamp-signing\release.key (used by path, never shown).
3. Commits and tags v<version>, then asks before pushing and running gh release create.
   The host PC installs the newest release, so a release is a deploy once someone taps
   Install there (or auto-install is on).

A version with a "-" (1.3.0-beta.1) is published as a pre-release: only PCs with the beta
channel on see it.
#>
param(
    [Parameter(Mandatory)][string]$Version,
    [switch]$NoPublish
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot)
$py = '.venv\Scripts\python.exe'
$key = Join-Path $env:USERPROFILE '.basecamp-signing\release.key'
$init = 'basecamp\__init__.py'

if ($Version -notmatch '^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$') { throw "Not a version: $Version" }
if (-not (Test-Path $key)) { throw "No signing key at $key; run scripts\new-signing-key.ps1 first" }

# 1. Checks
if (git status --porcelain) { throw "The git tree isn't clean; commit or remove changes first" }
if ((git rev-parse --abbrev-ref HEAD) -ne 'main') { throw "Not on main" }
git fetch origin main --quiet
if ((git rev-parse HEAD) -ne (git rev-parse origin/main)) { throw "main isn't the same as origin/main; pull or push first" }
if (git tag --list "v$Version") { throw "Tag v$Version already exists" }

$notes = @()
$inSection = $false
foreach ($line in Get-Content CHANGES.md -Encoding utf8) {
    if ($line -match '^## ') {
        if ($inSection) { break }
        $inSection = $line -match "^## v?$([regex]::Escape($Version))(\s|$)"
        continue
    }
    if ($inSection) { $notes += $line }
}
$notesText = ($notes -join "`n").Trim()
if (-not $notesText) { throw "CHANGES.md has no '## $Version' section (the release notes)" }

& $py -m pytest -q
if ($LASTEXITCODE) { throw "Tests failed" }
& $py -m ruff check .
if ($LASTEXITCODE) { throw "ruff found problems" }

# 2. Version bump and build
$utf8 = New-Object System.Text.UTF8Encoding $false
$old = [IO.File]::ReadAllText((Resolve-Path $init), $utf8)
$new = $old -replace '__version__ = "[^"]+"', "__version__ = `"$Version`""
[IO.File]::WriteAllText((Resolve-Path $init), $new, $utf8)
& $py scripts\build_release.py $Version --key $key --out dist
if ($LASTEXITCODE) {
    git checkout -- $init
    throw "Build failed"
}
$zip = "dist\basecamp-$Version.zip"
if ($NoPublish) {
    git checkout -- $init
    Write-Host "Built and signed $zip (not committed, tagged or published)."
    return
}

# 3. Commit, tag, ask, publish
if (git status --porcelain $init) { git commit -q -m "Release v$Version" -- $init }
git tag -a "v$Version" -m "Basecamp $Version"
Write-Host ""
Write-Host "Ready: $zip and its .sig, commit and tag v$Version (local only so far)."
Write-Host "Publishing makes v$Version the release the host PC offers to install."
$answer = Read-Host "Push and publish v$Version on GitHub? Type yes"
if ($answer -ne 'yes') {
    Write-Host "Not published. To undo locally: git tag -d v$Version, then git reset --soft HEAD~1 if a release commit was made."
    return
}
git push origin main
if ($LASTEXITCODE) { throw "git push failed" }
git push origin "v$Version"
if ($LASTEXITCODE) { throw "Pushing the tag failed" }
$notesFile = New-TemporaryFile
[IO.File]::WriteAllText($notesFile, $notesText, $utf8)
$ghArgs = @('release', 'create', "v$Version", $zip, "$zip.sig", '--verify-tag',
            '--title', "Basecamp $Version", '--notes-file', $notesFile)
if ($Version -match '-') { $ghArgs += '--prerelease' }
gh @ghArgs
$code = $LASTEXITCODE
Remove-Item $notesFile
if ($code) { throw "gh release create failed" }
Write-Host "Published v$Version."
