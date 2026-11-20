[CmdletBinding()]
param(
    [string]$Target = "C:\LocalPrintServer",
    [string]$Python = "",
    [string]$ServiceName = "LocalPrintServer",
    [int]$Port = 5000,
    [string]$BackupRoot = "C:\LocalPrintServer_backups",
    [string]$OpsRoot = "C:\ProgramData\LocalPrintServerOps",
    [ValidateRange(2,365)][int]$BackupRetentionCount = 30,
    [string]$SourceRepo = "<owner>/<private-source-repo>",
    [string]$BackupRepo = "<owner>/<private-backup-repo>"
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Security

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run setup_remote_management.ps1 in an elevated PowerShell window (Run as administrator)."
}
if (-not (Test-Path -LiteralPath (Join-Path $Target "app.py"))) { throw "LocalPrintServer app.py not found under $Target." }
if (-not (Test-Path -LiteralPath (Join-Path $Target "config.json"))) { throw "config.json not found under $Target." }
if (-not $Python) {
    foreach ($candidate in @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        "C:\Program Files\Python313\python.exe",
        "C:\Python313\python.exe",
        (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { $Python = $candidate; break }
    }
}
if (-not $Python -or -not (Test-Path -LiteralPath $Python)) { throw "Python not found: $Python" }
$service = Get-Service -Name $ServiceName -ErrorAction Stop
if ($service.Status -ne "Running") { throw "The live service '$ServiceName' is not Running. No tasks or secrets were installed." }
try {
    $health = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/login" -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 8 -ErrorAction Stop
    if ([int]$health.StatusCode -ne 200) { throw "Unexpected status $($health.StatusCode)" }
} catch {
    $code = 0
    try { $code = [int]$_.Exception.Response.StatusCode } catch {}
    if ($code -notin @(302,401)) { throw "Live service health check failed (HTTP $code). No tasks or secrets were installed." }
}

function Convert-SecureToPlain {
    param([Security.SecureString]$Value)
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}
function Store-DpapiSecret {
    param([string]$Name, [string]$PlainText, [string]$Destination)
    $bytes = [Text.Encoding]::UTF8.GetBytes($PlainText)
    try {
        $entropy = [Text.Encoding]::UTF8.GetBytes("LocalPrintServerOps-DPAPI-v1")
        $protected = [Security.Cryptography.ProtectedData]::Protect($bytes, $entropy, [Security.Cryptography.DataProtectionScope]::LocalMachine)
        [IO.File]::WriteAllBytes((Join-Path $Destination $Name), $protected)
        [Array]::Clear($protected, 0, $protected.Length)
    } finally { [Array]::Clear($bytes, 0, $bytes.Length) }
}
function Test-GitHubToken {
    param([string]$Token, [string]$Repository, [string]$Path = "")
    $headers = @{
        Authorization = "Bearer $Token"
        Accept = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
        "User-Agent" = "LocalPrintServerOps-Setup"
    }
    $uri = "https://api.github.com/repos/$Repository"
    $repoInfo = Invoke-RestMethod -Method GET -Uri $uri -Headers $headers -TimeoutSec 20 -ErrorAction Stop
    if (-not $repoInfo.private) { throw "Repository $Repository must be private." }
    if ($Path) {
        $encodedPath = ($Path -split '/') -join '/'
        $null = Invoke-RestMethod -Method GET -Uri "https://api.github.com/repos/$Repository/contents/$encodedPath?ref=main" -Headers $headers -TimeoutSec 20 -ErrorAction Stop
    }
    if ($Repository -eq $SourceRepo) {
        $null = Invoke-RestMethod -Method GET -Uri "https://api.github.com/repos/$Repository/actions/runs?per_page=1" -Headers $headers -TimeoutSec 20 -ErrorAction Stop
    }
}

$sourceRepo = $SourceRepo
$backupRepo = $BackupRepo
Write-Host "This sets up only scheduled maintenance. It does not stop or reconfigure LocalPrintServer." -ForegroundColor Cyan
Write-Host ""
Write-Host "Token A: fine-grained PAT, only $sourceRepo, Contents: Read-only."
$sourceSecure = Read-Host "Paste the source-repository READ-ONLY token" -AsSecureString
$sourceToken = Convert-SecureToPlain $sourceSecure
Write-Host ""
Write-Host "Token B: fine-grained PAT, only $backupRepo, Contents: Read and write."
$backupSecure = Read-Host "Paste the backup-repository WRITE token" -AsSecureString
$backupToken = Convert-SecureToPlain $backupSecure
try {
    Test-GitHubToken -Token $sourceToken -Repository $sourceRepo -Path "app.py"
    Test-GitHubToken -Token $backupToken -Repository $backupRepo -Path "README.md"
} catch {
    $sourceToken = ""
    $backupToken = ""
    throw "GitHub token preflight failed. Confirm each token is active and limited to the repository and Contents permissions listed above. Details: $($_.Exception.Message)"
}
$sourceSecure.Dispose()
$backupSecure.Dispose()

$passSecure = Read-Host "Set a backup encryption passphrase (at least 15 characters; store a copy offline)" -AsSecureString
$passphrase = Convert-SecureToPlain $passSecure
$passConfirmSecure = Read-Host "Enter the backup passphrase again" -AsSecureString
$passphraseConfirm = Convert-SecureToPlain $passConfirmSecure
$passSecure.Dispose()
$passConfirmSecure.Dispose()
if ($passphrase.Length -lt 15) { throw "Backup passphrase must contain at least 15 characters. Run setup again." }
if ($passphrase -cne $passphraseConfirm) { throw "Passphrases did not match. Run setup again." }
if ($passphrase -match '[\r\n]') { throw "Passphrase must not contain line breaks. Run setup again." }

$opsSource = Join-Path $PSScriptRoot "remote_ops"
if (-not (Test-Path -LiteralPath (Join-Path $opsSource "ops-common.ps1"))) { throw "Missing scripts\remote_ops directory. Run this script from a complete source package." }
New-Item -ItemType Directory -Path $OpsRoot -Force | Out-Null
$icacls = Join-Path $env:WINDIR "System32\icacls.exe"
& $icacls $OpsRoot /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict permissions on $OpsRoot." }

Get-ChildItem -LiteralPath $opsSource -File | Where-Object { $_.Extension -in @(".ps1", ".py") } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $OpsRoot $_.Name) -Force
}
Store-DpapiSecret -Name "source-read-token.dpapi" -PlainText $sourceToken -Destination $OpsRoot
Store-DpapiSecret -Name "backup-write-token.dpapi" -PlainText $backupToken -Destination $OpsRoot
Store-DpapiSecret -Name "backup-passphrase.dpapi" -PlainText $passphrase -Destination $OpsRoot
$sourceToken = ""
$backupToken = ""
$passphrase = ""
$passphraseConfirm = ""

$config = [ordered]@{
    source_repo = $sourceRepo
    backup_repo = $backupRepo
    target = [IO.Path]::GetFullPath($Target)
    python_path = [IO.Path]::GetFullPath($Python)
    service_name = $ServiceName
    port = $Port
    backup_root = [IO.Path]::GetFullPath($BackupRoot)
    backup_retention_count = $BackupRetentionCount
    installed_at_utc = [DateTime]::UtcNow.ToString("o")
}
$configJson = ConvertTo-Json -InputObject $config -Depth 8
[IO.File]::WriteAllText((Join-Path $OpsRoot "ops-config.json"), $configJson, (New-Object Text.UTF8Encoding($false)))

# Enforce SYSTEM/Administrators-only access on scripts, config and DPAPI-protected files.
& $icacls $OpsRoot /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /T /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not apply restrictive ACLs to the operations folder." }

$psExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$taskQuote = [char]34
$pollScript = Join-Path $OpsRoot "poll_approved_deployment.ps1"
$pollCommand = "$taskQuote$psExe$taskQuote -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $taskQuote$pollScript$taskQuote"
$schtasks = Join-Path $env:WINDIR "System32\schtasks.exe"
& $schtasks /Create /TN "LocalPrintServer-GitHub-Approved-Update" /SC MINUTE /MO 5 /TR $pollCommand /RU SYSTEM /RL HIGHEST /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not register the five-minute GitHub approval poll task." }

$backupScript = Join-Path $OpsRoot "nightly_backup.ps1"
$backupAction = New-ScheduledTaskAction -Execute $psExe -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + $taskQuote + $backupScript + $taskQuote)
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At ([DateTime]::Today.AddHours(2))
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$systemPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$backupSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)
Register-ScheduledTask -TaskName "LocalPrintServer-Nightly-GitHub-Backup" -Action $backupAction -Trigger @($dailyTrigger, $startupTrigger) -Principal $systemPrincipal -Settings $backupSettings -Description "Create an encrypted, integrity-checked LocalPrintServer snapshot and store it in the separate private GitHub backup repository." -Force | Out-Null

Write-Host ""
Write-Host "Setup completed." -ForegroundColor Green
Write-Host "Update poll: every 5 minutes; it acts only on a request created by the GitHub Actions approval workflow."
Write-Host "Nightly backup: 02:00 local time, with a startup catch-up if the PC missed the scheduled run."
Write-Host "Retained backups: latest $BackupRetentionCount encrypted GitHub Releases."
Write-Host "Logs: $OpsRoot\operations.log"
Write-Host ""
Write-Host "Next: run the backup task once to verify its upload permission and create your first off-site snapshot:" -ForegroundColor Cyan
Write-Host '  Start-ScheduledTask -TaskName "LocalPrintServer-Nightly-GitHub-Backup"'
Write-Host '  Get-Content "C:\ProgramData\LocalPrintServerOps\operations.log" -Tail 30'
Write-Host ""
Write-Host "Keep the backup encryption passphrase somewhere safe outside this PC and GitHub. Without it, encrypted backups cannot be restored."
Write-Host "No live server files were updated and no service was stopped by this setup script."
