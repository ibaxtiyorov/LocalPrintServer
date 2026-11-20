$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "ops-common.ps1")

$configPath = Join-Path $script:LpsOpsRoot "ops-config.json"
$statePath = Join-Path $script:LpsOpsRoot "deployment-state.json"
$workRoot = Join-Path $script:LpsOpsRoot "work"
$state = $null
$cfg = $null
$existingBackupNames = @()
$installStarted = $false

try {
    if (-not (Test-Path -LiteralPath $configPath)) { throw "Missing ops-config.json. Run setup_remote_management.ps1 first." }
    $cfg = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    New-Item -ItemType Directory -Path $workRoot -Force | Out-Null

    $state = [pscustomobject]@{ last_request_id = ""; applied_commit_sha = ""; status = "not_started"; updated_at_utc = ""; preexisting_backup_names = @(); install_started_at_utc = ""; failure = "" }
    if (Test-Path -LiteralPath $statePath) {
        try { $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch {
            throw "Deployment state file is unreadable; refusing to guess or deploy: $($_.Exception.Message)"
        }
    }

    $requestUri = "https://api.github.com/repos/$($cfg.source_repo)/contents/deployment/approved-release.json?ref=deployment-requests"
    try {
        $response = Invoke-LpsGitHubApi -Method GET -Uri $requestUri
    } catch {
        if ((Get-LpsHttpStatus $_) -eq 404) {
            Write-LpsLog "No deployment request has been published yet."
            exit 0
        }
        throw
    }

    if (-not $response.content -or $response.encoding -ne "base64") { throw "GitHub deployment request response was incomplete." }
    $requestJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($response.content -replace '\s','')))
    $request = $requestJson | ConvertFrom-Json
    if ($request.schema_version -ne 1) { throw "Unsupported deployment request schema." }
    if ($request.source_repository -ne $cfg.source_repo) { throw "Request names unexpected repository '$($request.source_repository)'." }
    if ([string]$request.request_id -notmatch '^[0-9]+-[0-9]+$') { throw "Invalid deployment request_id." }
    if ([string]$request.target_commit_sha -notmatch '^[0-9a-f]{40}$') { throw "Invalid target_commit_sha." }
    if ($request.source_branch -ne "main") { throw "Only deployment requests for the main branch are accepted." }

    # The request branch is mutable, so validate the recorded run directly with GitHub.
    # It must be a successful manual run of the approved workflow for exactly this SHA.
    $requestParts = ([string]$request.request_id).Split('-')
    $approvalRunId = $requestParts[0]
    $approvalRunUri = "https://api.github.com/repos/$($cfg.source_repo)/actions/runs/$approvalRunId"
    $approvalRun = Invoke-LpsGitHubApi -Method GET -Uri $approvalRunUri
    if ($approvalRun.event -ne "workflow_dispatch") { throw "The referenced approval run was not manually triggered." }
    if ($approvalRun.path -ne ".github/workflows/approve-production-deployment.yml") { throw "The request does not reference the approved deployment workflow." }
    if ($approvalRun.status -ne "completed" -or $approvalRun.conclusion -ne "success") { throw "The referenced approval workflow run did not complete successfully." }
    if ($approvalRun.head_sha -ne $request.target_commit_sha) { throw "The approved commit SHA does not match the manual workflow run's commit." }
    if ($request.workflow_run_url -ne $approvalRun.html_url) { throw "The workflow URL does not match the referenced approval run." }
    if ([int]$requestParts[1] -ne [int]$approvalRun.run_attempt) { throw "The deployment request attempt number does not match the referenced workflow run." }
    try {
        $requestedAt = [DateTimeOffset]::Parse([string]$request.requested_at_utc).UtcDateTime
        $workflowCreatedAt = [DateTimeOffset]::Parse([string]$approvalRun.created_at).UtcDateTime
    } catch {
        throw "The deployment request or referenced workflow run has an invalid timestamp."
    }
    if ([Math]::Abs(($requestedAt - $workflowCreatedAt).TotalMinutes) -gt 15) {
        throw "The deployment request timestamp is inconsistent with the referenced manual workflow run."
    }
    $runActor = ""
    if ($approvalRun.actor -and $approvalRun.actor.login) { $runActor = [string]$approvalRun.actor.login }
    if (-not $runActor -or $request.approved_by -ne $runActor) { throw "The approval actor does not match the referenced manual workflow run." }

    if ($request.request_id -eq $state.last_request_id) {
        if ($state.status -eq "attempting") {
            # If the polling process or PC was interrupted, never rerun install over a partially
            # replaced tree. The installer creates a full pre-change backup before it copies code.
            $knownBackups = @($state.preexisting_backup_names)
            $recovery = $null
            if ($cfg.backup_root -and (Test-Path -LiteralPath $cfg.backup_root)) {
                $recovery = Get-ChildItem -LiteralPath $cfg.backup_root -Directory -Filter "LocalPrintServer_*" |
                    Where-Object {
                        $knownBackups -notcontains $_.Name -and
                        (Test-Path (Join-Path $_.FullName "manifest.json")) -and
                        (Test-Path (Join-Path $_.FullName "files\scripts\rollback.ps1"))
                    } | Sort-Object Name -Descending | Select-Object -First 1
            }
            if ($recovery) {
                Write-LpsLog "Detected an interrupted deployment. Restoring the pre-update backup $($recovery.FullName) before any retry." "WARN"
                $stagedDeployRoot = Join-Path $workRoot ("deploy-" + $request.target_commit_sha)
                $stagedExtract = Join-Path $stagedDeployRoot "extract"
                $stagedPackage = $null
                if (Test-Path -LiteralPath $stagedExtract) {
                    $stagedPackage = Get-ChildItem -LiteralPath $stagedExtract -Directory | Where-Object {
                        Test-Path (Join-Path $_.FullName "scripts\rollback.ps1")
                    } | Select-Object -First 1
                }
                if ($stagedPackage) {
                    $rollbackScript = Join-Path $stagedPackage.FullName "scripts\rollback.ps1"
                } else {
                    $rollbackScript = Join-Path $recovery.FullName "files\scripts\rollback.ps1"
                }
                if (-not (Test-Path -LiteralPath $rollbackScript)) { throw "No rollback script was found for interrupted deployment recovery." }
                & $rollbackScript -Backup $recovery.FullName
                $svc = Get-Service -Name $cfg.service_name -ErrorAction Stop
                if ($svc.Status -ne "Running") { Start-Service -Name $cfg.service_name }
                $state.status = "interrupted_rolled_back"
                $state.failure = "Polling process or PC stopped during an earlier deployment attempt; restored the pre-update backup."
                $state.updated_at_utc = [DateTime]::UtcNow.ToString("o")
                Write-LpsJsonAtomic -Path $statePath -Value $state
                Write-LpsLog "Pre-update rollback after interruption completed. Re-approve the release to try it again."
                exit 1
            }

            # No completed installer backup means application files were not replaced: install.ps1
            # creates its full backup before copying source files. Start the existing service if needed.
            $svc = Get-Service -Name $cfg.service_name -ErrorAction SilentlyContinue
            if ($svc -and $svc.Status -ne "Running") { Start-Service -Name $cfg.service_name }
            $state.status = "interrupted_before_file_replacement"
            $state.failure = "An earlier attempt was interrupted before a complete installer backup was found. No automatic update retry was made."
            $state.updated_at_utc = [DateTime]::UtcNow.ToString("o")
            Write-LpsJsonAtomic -Path $statePath -Value $state
            Write-LpsLog "Interrupted attempt found without a complete installer backup; started the prior service if it was stopped. Check health, then approve again."
            exit 1
        }
        Write-LpsLog "Request $($request.request_id) already handled (status $($state.status)); no repeated attempt."
        exit 0
    }

    if ($request.target_commit_sha -eq $state.applied_commit_sha) {
        $state.last_request_id = $request.request_id
        $state.status = "no_change"
        $state.updated_at_utc = [DateTime]::UtcNow.ToString("o")
        Write-LpsJsonAtomic -Path $statePath -Value $state
        Write-LpsLog "Approved request $($request.request_id) targets the already installed commit; no files changed."
        exit 0
    }

    # Do not replace application files unless a recent encrypted off-site backup was uploaded
    # and verified. This gates updates when the backup task/token has stopped working.
    $lastBackupPath = Join-Path $script:LpsOpsRoot "last-backup.json"
    if (-not (Test-Path -LiteralPath $lastBackupPath)) {
        throw "No verified GitHub backup exists yet. Run LocalPrintServer-Nightly-GitHub-Backup successfully before deploying."
    }
    try {
        $lastBackup = Get-Content -LiteralPath $lastBackupPath -Raw | ConvertFrom-Json
        $backupCompletedAt = [DateTimeOffset]::Parse([string]$lastBackup.completed_at_utc).UtcDateTime
    } catch {
        throw "The last verified backup state is unreadable; refusing to deploy: $($_.Exception.Message)"
    }
    if ($lastBackup.status -ne "success" -or -not $lastBackup.asset_sha256 -or -not $lastBackup.asset_name -or -not $lastBackup.tag_name) {
        throw "The last backup state does not indicate a verified uploaded asset; refusing to deploy."
    }
    $backupAgeHours = ([DateTime]::UtcNow - $backupCompletedAt).TotalHours
    if ($backupAgeHours -lt -0.1 -or $backupAgeHours -gt 36) {
        throw "The last verified GitHub backup is older than 36 hours or has a future timestamp. Run the backup task successfully before deploying."
    }

    # Record the existing local-backup inventory before changing deployment state. This permits
    # recovery after power loss without confusing a previous unrelated backup with this rollout.
    if ($cfg.backup_root -and (Test-Path -LiteralPath $cfg.backup_root)) {
        $existingBackupNames = @(Get-ChildItem -LiteralPath $cfg.backup_root -Directory -Filter "LocalPrintServer_*" | ForEach-Object Name)
    }
    $state.last_request_id = $request.request_id
    $state.preexisting_backup_names = @($existingBackupNames)
    $state.install_started_at_utc = [DateTime]::UtcNow.ToString("o")
    $state.status = "attempting"
    $state.failure = ""
    $state.updated_at_utc = [DateTime]::UtcNow.ToString("o")
    Write-LpsJsonAtomic -Path $statePath -Value $state
    Write-LpsLog "Starting approved deployment request $($request.request_id) for commit $($request.target_commit_sha)."

    $runDir = Join-Path $workRoot ("deploy-" + $request.target_commit_sha)
    if (Test-Path -LiteralPath $runDir) { Remove-Item -LiteralPath $runDir -Recurse -Force }
    New-Item -ItemType Directory -Path $runDir -Force | Out-Null
    $zipPath = Join-Path $runDir "source.zip"
    $extractPath = Join-Path $runDir "extract"
    New-Item -ItemType Directory -Path $extractPath -Force | Out-Null

    $token = Get-LpsProtectedSecret -Name "source-read-token.dpapi"
    $headers = @{
        Authorization = "Bearer $token"
        Accept = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
        "User-Agent" = "LocalPrintServerOps"
    }
    $downloadUri = "https://api.github.com/repos/$($cfg.source_repo)/zipball/$($request.target_commit_sha)"
    Invoke-WebRequest -Uri $downloadUri -Headers $headers -OutFile $zipPath -UseBasicParsing -TimeoutSec 900 -ErrorAction Stop
    if ((Get-Item -LiteralPath $zipPath).Length -lt 10000) { throw "Downloaded source archive is unexpectedly small." }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force
    $package = Get-ChildItem -LiteralPath $extractPath -Directory | Where-Object {
        (Test-Path (Join-Path $_.FullName "app.py")) -and
        (Test-Path (Join-Path $_.FullName "lps")) -and
        (Test-Path (Join-Path $_.FullName "scripts\install.ps1"))
    } | Select-Object -First 1
    if (-not $package) { throw "Downloaded commit does not contain a valid LocalPrintServer package and installer." }

    $powershellExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $installScript = Join-Path $package.FullName "scripts\install.ps1"
    $installLog = Join-Path $runDir "installer.log"
    $q = [char]34
    $argList = @(
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "$q$installScript$q",
        "-Source", "$q$($package.FullName)$q", "-Target", "$q$($cfg.target)$q"
    )
    $installStarted = $true
    $installer = Start-Process -FilePath $powershellExe -ArgumentList $argList -Wait -PassThru -RedirectStandardOutput $installLog -RedirectStandardError (Join-Path $runDir "installer-error.log")
    if ($installer.ExitCode -ne 0) { throw "Installer exited with code $($installer.ExitCode). See $installLog." }

    $service = Get-Service -Name $cfg.service_name -ErrorAction Stop
    if ($service.Status -ne "Running") { throw "Service '$($cfg.service_name)' is not running after install." }

    $pythonCheckLog = Join-Path $runDir "manage-check.log"
    $ErrorActionPreference = "Continue"
    & $cfg.python_path (Join-Path $cfg.target "manage.py") check *> $pythonCheckLog
    $checkExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($checkExit -ne 0) { throw "Post-install manage.py check failed with exit code $checkExit. See $pythonCheckLog." }

    $healthy = $false
    try {
        $health = Invoke-WebRequest -Uri "http://127.0.0.1:$($cfg.port)/login" -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 8 -ErrorAction Stop
        $healthy = ([int]$health.StatusCode -eq 200)
    } catch {
        $statusCode = Get-LpsHttpStatus $_
        $healthy = $statusCode -in @(302, 401)
        if (-not $healthy) { throw "Login endpoint health check failed (HTTP $statusCode)." }
    }
    if (-not $healthy) { throw "Login endpoint health check failed." }

    $state.applied_commit_sha = [string]$request.target_commit_sha
    $state.status = "applied"
    $state.updated_at_utc = [DateTime]::UtcNow.ToString("o")
    Write-LpsJsonAtomic -Path $statePath -Value $state
    Write-LpsLog "Deployment succeeded: commit $($request.target_commit_sha), request $($request.request_id). Health check passed."
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-LpsLog "Deployment failed: $message" "ERROR"
    if ($state -and $request -and $state.last_request_id -and $state.last_request_id -eq $request.request_id -and $state.status -eq "attempting") {
        $state.status = "failed"
        $state | Add-Member -MemberType NoteProperty -Name failure -Value $message -Force
        $state.updated_at_utc = [DateTime]::UtcNow.ToString("o")
        try { Write-LpsJsonAtomic -Path $statePath -Value $state } catch {}
    }

    # Use the rollback script from the just-created local backup, not the downloaded package.
    try {
        if ($installStarted -and $cfg -and $cfg.backup_root -and (Test-Path -LiteralPath $cfg.backup_root)) {
            if ($existingBackupNames) {
                $allBackups = Get-ChildItem -LiteralPath $cfg.backup_root -Directory -Filter "LocalPrintServer_*" |
                    Where-Object { $existingBackupNames -notcontains $_.Name -and (Test-Path (Join-Path $_.FullName "manifest.json")) } |
                    Sort-Object Name -Descending
                $newBackup = $allBackups | Select-Object -First 1
            } else {
                $newBackup = Get-ChildItem -LiteralPath $cfg.backup_root -Directory -Filter "LocalPrintServer_*" |
                    Where-Object { (Test-Path (Join-Path $_.FullName "manifest.json")) -and $_.LastWriteTime -ge (Get-Date).AddHours(-1) } |
                    Sort-Object Name -Descending | Select-Object -First 1
            }
            if ($newBackup) {
                $newRollback = ""
                if ($package -and (Test-Path -LiteralPath (Join-Path $package.FullName "scripts\rollback.ps1"))) {
                    # Use the vetted rollback helper from the approved package so data ACLs are
                    # re-applied before the restored service is started.
                    $newRollback = Join-Path $package.FullName "scripts\rollback.ps1"
                } else {
                    $newRollback = Join-Path $newBackup.FullName "files\scripts\rollback.ps1"
                }
                if (-not (Test-Path -LiteralPath $newRollback)) { throw "No rollback script was found for backup '$($newBackup.FullName)'." }
                Write-LpsLog "Attempting rollback from $($newBackup.FullName)."
                & $newRollback -Backup $newBackup.FullName
                $svc = Get-Service -Name $cfg.service_name -ErrorAction Stop
                if ($svc.Status -ne "Running") { Start-Service -Name $cfg.service_name }
                Write-LpsLog "Rollback script completed after failed deployment."
            } else {
                Write-LpsLog "No new complete installer backup found. Attempting to start the existing service." "ERROR"
                $svc = Get-Service -Name $cfg.service_name -ErrorAction SilentlyContinue
                if ($svc -and $svc.Status -ne "Running") { Start-Service -Name $cfg.service_name }
            }
        }
    } catch {
        Write-LpsLog "AUTOMATIC ROLLBACK FAILED: $($_.Exception.Message). Manual inspection is required." "ERROR"
    }
    exit 1
}
