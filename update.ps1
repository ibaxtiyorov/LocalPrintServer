Start-Sleep -Seconds 3

$RepoDir = "C:\LocalPrintServer"
$LogFile = "C:\LocalPrintServer\logs\updater.log"

function Write-UpdateLog($Message) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "[$ts] $Message" -ErrorAction SilentlyContinue
}

Set-Location -LiteralPath $RepoDir
Write-UpdateLog "Starting in-app update sequence..."

Stop-Service "LocalPrintServer" -Force -ErrorAction SilentlyContinue
Write-UpdateLog "Stopped LocalPrintServer service. Running git pull..."

$pullOutput = & git -c "safe.directory=*" pull 2>&1 | Out-String
Write-UpdateLog "git pull output: $($pullOutput.Trim())"

Start-Service "LocalPrintServer" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 5

$service = Get-CimInstance Win32_Service -Filter "Name='LocalPrintServer'" | Select-Object Name, State, ProcessId
if ($service.State -eq 'Running') {
    Write-UpdateLog "SUCCESS: LocalPrintServer is Running (PID: $($service.ProcessId))."
} else {
    Write-UpdateLog "ERROR: Service failed health check (State: $($service.State)). Rolling back via git reset --hard HEAD@{1}..."
    Stop-Service "LocalPrintServer" -Force -ErrorAction SilentlyContinue
    $resetOutput = & git -c "safe.directory=*" reset --hard "HEAD@{1}" 2>&1 | Out-String
    Write-UpdateLog "git reset output: $($resetOutput.Trim())"
    Start-Service "LocalPrintServer" -ErrorAction SilentlyContinue
    Write-UpdateLog "Rollback completed and service restarted."
}
