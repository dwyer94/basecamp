<#
.SYNOPSIS
Makes the Ed25519 key pair Basecamp releases are signed with (docs/07-updates.md).

.DESCRIPTION
Run once, on the dev PC, from the repo folder:

    scripts\new-signing-key.ps1

Writes the private key to %USERPROFILE%\.basecamp-signing\release.key (only your account
can read that folder afterwards) and the public key to basecamp\release_key.pub, which you
commit. Prints only the public key. Refuses to replace an existing key.

Then back up %USERPROFILE%\.basecamp-signing\ (password manager or a USB stick). If the
key is lost, make a new pair and reinstall once by hand on the host PC.
#>
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot)
$dir = Join-Path $env:USERPROFILE '.basecamp-signing'
& .venv\Scripts\python.exe scripts\new_signing_key.py --dir $dir
if ($LASTEXITCODE) { throw "No key made" }
# Only this account (and SYSTEM) can open the folder.
icacls $dir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" /grant:r "SYSTEM:(OI)(CI)F" | Out-Null
Write-Host ""
Write-Host "Next:"
Write-Host "  1. Back up $dir now."
Write-Host "  2. Commit basecamp\release_key.pub (the public key; safe to publish)."
