<#
.SYNOPSIS
  Optional: on a PC WITH internet, download all wheels so the server can install offline.
  Copy the resulting .\wheels folder next to app.py; install.ps1 will use it automatically.
  Must be run with the same Python version as the server (3.13, 64-bit).
#>
param([string]$Python = "python")
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }
$root = Split-Path -Parent $scriptDir
& $Python -m pip download -r (Join-Path $root "requirements.txt") -d (Join-Path $root "wheels") `
    --only-binary=:all: --platform win_amd64 --python-version 3.13
