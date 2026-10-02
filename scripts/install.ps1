<#
.SYNOPSIS
  Installs / upgrades LocalPrintServer on the print-server PC.

.DESCRIPTION
  1. Stops the LocalPrintServer service (if present).
  2. BACKS UP the existing C:\LocalPrintServer folder and the NSSM service settings
     to C:\LocalPrintServer_backups\<timestamp>  (used by rollback.ps1).
  3. Copies the new application files (keeps existing config.json, data\ and logs\).
  4. Installs Python dependencies (online, or offline from .\wheels if present).
  5. Runs "manage.py check" (reads printer info, prints nothing).
  6. Creates/updates the NSSM service (auto start, restart on failure, log files).
  7. Adds a Windows Firewall rule for TCP <Port> limited to the local subnet.
  8. Starts the service and checks http://127.0.0.1:<Port>/.

  Run from an elevated PowerShell in the unpacked package folder:
     powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#>
[CmdletBinding()]
param(
    # Resolved in the script body: Windows PowerShell 5.1 has not set $PSScriptRoot yet
    # while parameter defaults are evaluated under "powershell.exe -File".
    [string]$Source = "",
    [string]$Target = "C:\LocalPrintServer",
    [string]$Python = "C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe",
    [string]$Nssm = "",
    [string]$ServiceName = "LocalPrintServer",
    [int]$Port = 5000,
    [string]$BackupRoot = "C:\LocalPrintServer_backups",
    [switch]$SkipPip,
    [switch]$SkipService,
    [switch]$SkipFirewall,
    [switch]$RemoveStartupShortcuts
)

$ErrorActionPreference = "Stop"
function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "WARNING: $msg" -ForegroundColor Yellow }
# Runs a native tool without letting its stderr abort the script (Windows PowerShell 5.1
# turns redirected native stderr into terminating errors under ErrorActionPreference=Stop).
function Invoke-Native([string]$exe, [string[]]$arguments) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $out = & $exe @arguments 2>&1 | ForEach-Object { "$_" } | Out-String } finally { $ErrorActionPreference = $old }
    # nssm.exe writes UTF-16 when redirected: drop the NUL characters that PowerShell keeps.
    return ($out -replace "`0", "").Trim()
}

# ---------------------------------------------------------------- checks
if (-not $Source) {
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition }
    $Source = Split-Path -Parent $scriptDir          # package root = parent of scripts\
}
if (-not (Test-Path (Join-Path $Source "app.py")) -or -not (Test-Path (Join-Path $Source "lps"))) {
    throw "Source '$Source' does not look like the LocalPrintServer package (app.py / lps missing)."
}
$Source = (Resolve-Path $Source).Path.TrimEnd('\')
$TargetFull = [IO.Path]::GetFullPath($Target).TrimEnd('\')
# The package may have been extracted directly into the target folder.
$inPlace = [string]::Equals($Source, $TargetFull, [StringComparison]::OrdinalIgnoreCase)
Write-Host "Package: $Source"
Write-Host "Target:  $TargetFull$(if ($inPlace) { '  (package already extracted here: in-place install)' })"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated (Administrator) PowerShell."
}
if (-not (Test-Path $Python)) { throw "Python not found at '$Python'. Pass -Python <path>." }
if (-not $SkipService) {
    if (-not $Nssm) {
        # An existing NSSM service's executable IS nssm.exe.
        $svcPath = (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue).PathName
        if ($svcPath -and $svcPath.Trim('"') -match '(?i)^(.*nssm\.exe)') {
            if (-not $Nssm -and (Test-Path $Matches[1])) { $Nssm = $Matches[1] }
        }
        $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
        if (-not $Nssm -and $cmd) { $Nssm = $cmd.Source }
        foreach ($c in @("C:\nssm\win64\nssm.exe", "C:\nssm-2.24\win64\nssm.exe", "C:\Program Files\nssm\win64\nssm.exe",
                         "C:\tools\nssm\win64\nssm.exe")) { if (-not $Nssm -and (Test-Path $c)) { $Nssm = $c } }
    }
    if (-not $Nssm -or -not (Test-Path $Nssm)) { throw "nssm.exe not found. Pass -Nssm <path to nssm 2.24 win64\nssm.exe>." }
}
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backup = Join-Path $BackupRoot "LocalPrintServer_$stamp"

# ---------------------------------------------------------------- stop service
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -ne "Stopped") {
    Step "Stopping service $ServiceName"
    Stop-Service -Name $ServiceName -Force
    (Get-Service $ServiceName).WaitForStatus("Stopped", [TimeSpan]::FromSeconds(60))
}

# ---------------------------------------------------------------- backup
Step "Backing up to $backup"
New-Item -ItemType Directory -Force -Path $backup | Out-Null
if (Test-Path $Target) {
    & robocopy.exe $Target (Join-Path $backup "files") /E /COPY:DAT /R:1 /W:1 /NFL /NDL /NP /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Backup copy failed (robocopy exit $LASTEXITCODE)." }
    Write-Host "Copied existing $Target"
    if ($inPlace) {
        Warn ("The package was extracted directly into $Target, so files it replaced (e.g. the old " +
              "experimental app.py) were overwritten BEFORE this backup. The backup holds the current " +
              "folder plus the NSSM settings; restore the old app.py from your own earlier copy if needed.")
    }
} else {
    Write-Host "No existing $Target (fresh install)."
}
$manifest = [ordered]@{ created = (Get-Date).ToString("s"); target = $Target; service = $ServiceName;
                        service_existed = [bool]$svc; nssm = @{} }
if ($svc -and $Nssm) {
    foreach ($k in "Application", "AppDirectory", "AppParameters", "AppStdout", "AppStderr", "Start", "ObjectName",
                   "DisplayName", "Description", "AppExit", "AppRestartDelay") {
        $manifest.nssm[$k] = Invoke-Native $Nssm @("get", $ServiceName, $k)
    }
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 (Join-Path $backup "manifest.json")

# ---------------------------------------------------------------- startup shortcuts
Step "Checking for old Startup-folder launchers"
$startupDirs = @([Environment]::GetFolderPath("CommonStartup")) +
               (Get-ChildItem "C:\Users" -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path $_.FullName "AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup" })
$found = @()
foreach ($d in $startupDirs) {
    if (-not (Test-Path $d)) { continue }
    Get-ChildItem $d -File -ErrorAction SilentlyContinue | ForEach-Object {
        $txt = ""
        try {
            if ($_.Extension -eq ".lnk") {
                $sc = (New-Object -ComObject WScript.Shell).CreateShortcut($_.FullName)
                $txt = "$($sc.TargetPath) $($sc.Arguments) $($sc.WorkingDirectory)"
            } elseif ($_.Extension -in ".bat", ".cmd", ".vbs", ".ps1") { $txt = Get-Content $_.FullName -Raw }
        } catch { }
        if ($txt -match "LocalPrintServer|app\.py") { $found += $_.FullName }
    }
}
if ($found) {
    if ($RemoveStartupShortcuts) {
        $dest = Join-Path $backup "startup_shortcuts"
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        foreach ($f in $found) { Move-Item $f $dest; Write-Host "Moved $f -> $dest" }
    } else {
        Warn "Found old Startup launchers that also start the app (they would conflict with the service on port $Port):"
        $found | ForEach-Object { Write-Host "   $_" }
        Warn "Re-run with -RemoveStartupShortcuts to move them into the backup folder."
    }
} else { Write-Host "None found." }

# ---------------------------------------------------------------- copy files
if ($inPlace) {
    Step "Application files are already in $Target (in-place install, nothing to copy)"
} else {
    Step "Copying application to $Target"
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    & robocopy.exe $Source $Target /E /R:1 /W:1 /NFL /NDL /NP /NJH /NJS `
        /XD ".venv" "venv" "__pycache__" ".pytest_cache" "data" "logs" ".git" `
        /XF "config.json" "*.pyc" | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "File copy failed (robocopy exit $LASTEXITCODE)." }
}
$cfg = Join-Path $Target "config.json"
if (-not (Test-Path $cfg)) {
    $json = Get-Content (Join-Path $Target "config.example.json") -Raw | ConvertFrom-Json
    $json.port = $Port
    # UTF-8 without BOM (Set-Content -Encoding UTF8 in Windows PowerShell 5.1 adds a BOM).
    [IO.File]::WriteAllText($cfg, ($json | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    Write-Host "Created config.json (port $Port)."
} else { Write-Host "Kept existing config.json." }
foreach ($d in "data", "logs") { New-Item -ItemType Directory -Force -Path (Join-Path $Target $d) | Out-Null }
# Only SYSTEM and Administrators may read the data folder (DB, secret key, uploads).
# SIDs instead of names: group names are localised (e.g. "Администраторы").
$acl = Invoke-Native "icacls.exe" @((Join-Path $Target "data"), "/inheritance:r", "/grant:r",
                                    "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")
if ($LASTEXITCODE -ne 0) { Warn "Could not restrict permissions on data\: $acl" }

# ---------------------------------------------------------------- dependencies
if (-not $SkipPip) {
    Step "Installing Python dependencies"
    # The service runs as LocalSystem, which cannot see packages installed with
    # "pip install --user" in another account's profile. Ignore the user site so
    # everything lands in the interpreter's own site-packages.
    $env:PYTHONNOUSERSITE = "1"
    $wheels = Join-Path $Target "wheels"
    if (Test-Path $wheels) {
        & $Python -m pip install --no-index --find-links $wheels -r (Join-Path $Target "requirements.txt")
    } else {
        & $Python -m pip install -r (Join-Path $Target "requirements.txt")
    }
    if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
}

Step "Checking imports exactly as the service will see them (user site disabled)"
$imports = Invoke-Native $Python @("-s", "-c", "import flask, PIL, win32print, win32ui, pymupdf, waitress; print('imports ok')")
Write-Host $imports
if ($imports -notmatch "imports ok") { throw "Required packages are not importable without the user site; see output above." }

Step "Word/Excel conversion prerequisites"
# Word/Excel started by a service (LocalSystem) fail to open files unless these folders exist.
foreach ($d in "$env:windir\System32\config\systemprofile\Desktop",
               "$env:windir\SysWOW64\config\systemprofile\Desktop") {
    try {
        if ((Test-Path (Split-Path $d)) -and -not (Test-Path $d)) {
            New-Item -ItemType Directory -Force -Path $d -ErrorAction Stop | Out-Null
            Write-Host "Created $d"
        }
    } catch {
        Warn "Could not create $d (needed only for Microsoft Office conversion): $($_.Exception.Message)"
    }
}

Step "Environment check (no printing)"
Push-Location $Target
& $Python manage.py check
$checkOk = ($LASTEXITCODE -eq 0)
Pop-Location
if (-not $checkOk) { Warn "manage.py check reported problems (see above). Continuing." }

# ---------------------------------------------------------------- NSSM service
if (-not $SkipService) {
    Step "Configuring NSSM service $ServiceName"
    if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        Invoke-Native $Nssm @("install", $ServiceName, $Python, "app.py") | Out-Null
        if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { throw "nssm install failed." }
    }
    $logs = Join-Path $Target "logs"
    $settings = [ordered]@{
        Application = $Python; AppDirectory = $Target; AppParameters = "app.py";
        DisplayName = "Local Print Server (EPSON L1800)";
        Description = "LAN print management web app on port $Port";
        Start = "SERVICE_AUTO_START"; ObjectName = "LocalSystem";
        AppStdout = (Join-Path $logs "service-stdout.log"); AppStderr = (Join-Path $logs "service-stderr.log");
        AppRotateFiles = "1"; AppRotateOnline = "1"; AppRotateBytes = "10485760";
        AppExit = "Default Restart"; AppRestartDelay = "5000"; AppThrottle = "10000";
        AppStopMethodConsole = "15000"; AppStopMethodWindow = "5000"; AppStopMethodThreads = "5000";
        AppEnvironmentExtra = "PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8"
    }
    foreach ($k in $settings.Keys) {
        if ($k -eq "AppExit") { $a = @("set", $ServiceName, "AppExit", "Default", "Restart") }
        elseif ($k -eq "AppEnvironmentExtra") {
            $a = @("set", $ServiceName, "AppEnvironmentExtra", "PYTHONUNBUFFERED=1", "PYTHONIOENCODING=utf-8")
        }
        else { $a = @("set", $ServiceName, $k, $settings[$k]) }
        $out = Invoke-Native $Nssm $a
        if ($LASTEXITCODE -ne 0) { throw "nssm $($a -join ' ') failed: $out" }
    }
    # NOTE: no DependOnService=Spooler on purpose: "Restart Print Spooler" in the admin panel
    # would otherwise stop this service too. The app tolerates spooler outages itself.
    Invoke-Native $Nssm @("reset", $ServiceName, "DependOnService") | Out-Null
    Invoke-Native "sc.exe" @("failure", $ServiceName, "reset=", "86400",
                             "actions=", "restart/5000/restart/10000/restart/30000") | Out-Null
}

# ---------------------------------------------------------------- firewall
if (-not $SkipFirewall) {
    Step "Firewall rule (TCP $Port, local subnet only, Private/Domain profiles)"
    $ruleName = "LocalPrintServer TCP $Port (LAN)"
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
        -Profile Private, Domain -RemoteAddress LocalSubnet | Out-Null
    $public = Get-NetConnectionProfile | Where-Object NetworkCategory -eq "Public"
    foreach ($p in $public) {
        Warn "Network '$($p.Name)' on '$($p.InterfaceAlias)' is PUBLIC; the LAN rule does not apply there."
        Write-Host "   If this is your office LAN, run:  Set-NetConnectionProfile -InterfaceIndex $($p.InterfaceIndex) -NetworkCategory Private"
    }
}

# ---------------------------------------------------------------- start + verify
if (-not $SkipService) {
    Step "Starting service"
    Start-Service -Name $ServiceName
    $ok = $false
    for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
        Start-Sleep -Seconds 1
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/login" -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 3 -ErrorAction Stop
            $ok = $true
        } catch {
            if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -in 302, 401) { $ok = $true }
        }
    }
    if ($ok) { Write-Host "Service is answering on port $Port." -ForegroundColor Green }
    else { Warn "Service did not answer yet. Check $Target\logs\app.log and logs\service-stderr.log" }
}

$ips = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" }).IPAddress
Write-Host "`nDone. Backup: $backup" -ForegroundColor Green
Write-Host "Open on the server:  http://127.0.0.1:$Port/setup   (first run: create the administrator)"
foreach ($ip in $ips) { Write-Host "LAN clients:         http://${ip}:$Port" }
Write-Host "Rollback:            powershell -ExecutionPolicy Bypass -File `"$Target\scripts\rollback.ps1`" -Backup `"$backup`""
