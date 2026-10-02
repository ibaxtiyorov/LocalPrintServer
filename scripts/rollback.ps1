<#
.SYNOPSIS
  Restores C:\LocalPrintServer and the NSSM service settings from a backup made by install.ps1.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\rollback.ps1                  # newest backup
  powershell -ExecutionPolicy Bypass -File .\scripts\rollback.ps1 -Backup C:\LocalPrintServer_backups\LocalPrintServer_20260927_101500
#>
[CmdletBinding()]
param(
    [string]$Backup = "",
    [string]$BackupRoot = "C:\LocalPrintServer_backups",
    [string]$Nssm = ""
)
$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated (Administrator) PowerShell."
}
if (-not $Backup) {
    $Backup = (Get-ChildItem $BackupRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1).FullName
}
$manifestPath = Join-Path $Backup "manifest.json"
if (-not (Test-Path $manifestPath)) { throw "No manifest.json in '$Backup'." }
$m = Get-Content $manifestPath -Raw | ConvertFrom-Json
$Target, $ServiceName = $m.target, $m.service
if (-not $Nssm) {
    # An existing NSSM service's executable IS nssm.exe.
    $svcPath = (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue).PathName
    if ($svcPath -and $svcPath.Trim('"') -match '(?i)^(.*nssm\.exe)') {
        if (-not $Nssm -and (Test-Path $Matches[1])) { $Nssm = $Matches[1] }
    }
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if (-not $Nssm -and $cmd) { $Nssm = $cmd.Source }
}
if ($m.service_existed -and -not $Nssm) {
    Write-Host "nssm.exe not found: service settings will NOT be restored. Re-run with -Nssm <path>." -ForegroundColor Yellow
}
Write-Host "Restoring $Target from $Backup" -ForegroundColor Cyan

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -ne "Stopped") {
    Stop-Service -Name $ServiceName -Force
    (Get-Service $ServiceName).WaitForStatus("Stopped", [TimeSpan]::FromSeconds(60))
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
if (Test-Path $Target) {
    $failed = "${Target}_replaced_$stamp"
    Move-Item $Target $failed
    Write-Host "Current version moved to $failed (kept for investigation)."
}
$files = Join-Path $Backup "files"
if (Test-Path $files) {
    & robocopy.exe $files $Target /E /COPY:DAT /R:1 /W:1 /NFL /NDL /NP /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Restore copy failed (robocopy exit $LASTEXITCODE)." }
} else {
    Write-Host "The backup was taken before the first install (no previous files)."
}

if ($m.service_existed -and $Nssm -and $m.nssm) {
    foreach ($p in $m.nssm.PSObject.Properties) {
        $value = ([string]$p.Value) -replace "`0", ""          # older manifests: nssm UTF-16 output
        if (-not $value -or $p.Name -eq "AppExit") { continue }
        if ($p.Name -eq "ObjectName" -and $value -ne "LocalSystem") {
            Write-Host "Service account was '$value': set it again in services.msc (a password is needed)." -ForegroundColor Yellow
            continue
        }
        $ErrorActionPreference = "Continue"
        $out = (& $Nssm set $ServiceName $p.Name $value 2>&1 | Out-String) -replace "`0", ""
        $ErrorActionPreference = "Stop"
        if ($LASTEXITCODE -ne 0) { Write-Host "nssm set $($p.Name) failed: $out" -ForegroundColor Yellow }
    }
    Write-Host "NSSM settings restored."
} elseif (-not $m.service_existed -and (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    Write-Host "Service did not exist before the install; leaving it stopped. Remove with:  nssm remove $ServiceName confirm"
}
foreach ($d in (Join-Path $Backup "startup_shortcuts")) {
    if (Test-Path $d) { Write-Host "Moved Startup launchers are in $d (restore manually if still wanted)." }
}
if ($m.service_existed) {
    Start-Service -Name $ServiceName
    Write-Host "Service $ServiceName started." -ForegroundColor Green
}
