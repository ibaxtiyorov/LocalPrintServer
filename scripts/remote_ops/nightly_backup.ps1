$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "ops-common.ps1")

$runDir = $null
$release = $null
$uploaded = $false
$workRoot = Join-Path $script:LpsOpsRoot "work"
$statePath = Join-Path $script:LpsOpsRoot "last-backup.json"
$backupTag = $null
$cfg = $null

try {
    $cfg = Get-Content -LiteralPath (Join-Path $script:LpsOpsRoot "ops-config.json") -Raw | ConvertFrom-Json
    New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
    # A forced shutdown may leave a plaintext staging tree behind. Remove only stale backup
    # work directories, never active deployment work, after a full day has elapsed.
    Get-ChildItem -LiteralPath $workRoot -Directory -Filter "backup-*" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTimeUtc -lt [DateTime]::UtcNow.AddDays(-1) } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    $nowUtc = [DateTime]::UtcNow
    if (Test-Path -LiteralPath $statePath) {
        try {
            $lastState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
            $lastCompleted = [DateTime]::Parse([string]$lastState.completed_at_utc).ToUniversalTime()
            if (($nowUtc - $lastCompleted).TotalHours -lt 20) {
                Write-LpsLog "Backup skipped: a successful backup completed less than 20 hours ago."
                exit 0
            }
        } catch {
            Write-LpsLog "Prior backup state was unreadable; proceeding with a new backup." "WARN"
        }
    }

    $target = [string]$cfg.target
    $appConfigPath = Join-Path $target "config.json"
    if (-not (Test-Path -LiteralPath $appConfigPath)) { throw "Application config not found: $appConfigPath" }
    $appConfig = Get-Content -LiteralPath $appConfigPath -Raw | ConvertFrom-Json
    $dataSetting = [string]$appConfig.data_dir
    if ([IO.Path]::IsPathRooted($dataSetting)) { $sourceData = [IO.Path]::GetFullPath($dataSetting) }
    else { $sourceData = [IO.Path]::GetFullPath((Join-Path $target $dataSetting)) }
    $sourceDb = Join-Path $sourceData "lps.sqlite3"
    if (-not (Test-Path -LiteralPath $sourceDb)) { throw "Configured SQLite database not found: $sourceDb" }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupTag = "lps-backup-$stamp"
    $runDir = Join-Path $workRoot ("backup-" + $stamp)
    $restoreRoot = Join-Path $runDir "restore"
    $stageData = Join-Path $restoreRoot "data"
    New-Item -ItemType Directory -Path $stageData -Force | Out-Null
    Copy-Item -LiteralPath $appConfigPath -Destination (Join-Path $restoreRoot "config.json") -Force
    $snapshotScript = Join-Path $PSScriptRoot "backup_sqlite_snapshot.py"
    if (-not (Test-Path -LiteralPath $snapshotScript)) { throw "SQLite snapshot helper missing: $snapshotScript" }

    # Copy all data except live SQLite files. Snapshot the DB afterwards, then ensure every
    # current uploads-table row has a corresponding staged file. Retry if uploads raced the copy.
    $python = [string]$cfg.python_path
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        foreach ($entry in (Get-ChildItem -LiteralPath $sourceData -Force)) {
            if ($entry.Name -in @("lps.sqlite3", "lps.sqlite3-wal", "lps.sqlite3-shm")) { continue }
            $destPath = Join-Path $stageData $entry.Name
            if ($entry.PSIsContainer) {
                New-Item -ItemType Directory -Path $destPath -Force | Out-Null
                $robocopyExe = Join-Path $env:WINDIR "System32\robocopy.exe"
                & $robocopyExe $entry.FullName $destPath /E /COPY:DAT /R:1 /W:1 /NFL /NDL /NP /NJH /NJS | Out-Null
                if ($LASTEXITCODE -ge 8) { throw "Copying data directory '$($entry.Name)' failed (robocopy exit $LASTEXITCODE)." }
            } else {
                Copy-Item -LiteralPath $entry.FullName -Destination $destPath -Force
            }
        }
        $snapshotDb = Join-Path $stageData "lps.sqlite3"
        $ErrorActionPreference = "Continue"
        & $python -I $snapshotScript $sourceDb $snapshotDb $stageData *> (Join-Path $runDir "sqlite-snapshot.log")
        $snapshotExit = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        if ($snapshotExit -eq 0) { break }
        if ($snapshotExit -ne 21 -or $attempt -eq 4) {
            $details = Get-Content -LiteralPath (Join-Path $runDir "sqlite-snapshot.log") -Raw -ErrorAction SilentlyContinue
            throw "SQLite snapshot or file consistency validation failed (exit $snapshotExit): $details"
        }
        Write-LpsLog "Snapshot found upload files missing from the copied tree; repeating data copy (attempt $($attempt + 1)/4)." "WARN"
    }

    $deployStatePath = Join-Path $script:LpsOpsRoot "deployment-state.json"
    $appliedCommit = ""
    if (Test-Path -LiteralPath $deployStatePath) {
        try { $appliedCommit = [string](Get-Content -LiteralPath $deployStatePath -Raw | ConvertFrom-Json).applied_commit_sha } catch {}
    }
    $metadata = [ordered]@{
        format_version = 1
        created_at_utc = [DateTime]::UtcNow.ToString("o")
        source_repository = [string]$cfg.source_repo
        source_commit_sha = $appliedCommit
        original_application_path = $target
        original_data_path = $sourceData
        database_snapshot = "data/lps.sqlite3"
        encryption = "AES-256-CBC with HMAC-SHA256; PBKDF2-derived keys; 310000 iterations"
        note = "This archive contains sensitive application data and is encrypted. Keep the passphrase separately."
    }
    [IO.File]::WriteAllText((Join-Path $restoreRoot "metadata.json"), (ConvertTo-Json -InputObject $metadata -Depth 8), (New-Object Text.UTF8Encoding($false)))
    $restoreNotes = @"
LocalPrintServer recovery snapshot
=================================

This archive is encrypted before upload. Download the matching .lpsenc asset from the
private backup repository GitHub Release and decrypt it with decrypt_backup.ps1
from the source repository.

The extracted archive contains config.json and data\. The data\lps.sqlite3 file is a
SQLite online-backup snapshot verified by PRAGMA integrity_check. The entire data tree,
including the session-signing key and uploaded/job files, is included.

To restore on the original PC:
1. Keep this archive and its encryption passphrase private.
2. Stop the LocalPrintServer service.
3. Preserve the current application data and config.json before replacing anything.
4. Restore config.json to the application folder and the archived data tree to the original
   data path recorded in metadata.json. If moving to a different path, adjust data_dir in config.json.
5. Reapply restrictive permissions to the data directory (SYSTEM and Administrators only).
6. Start LocalPrintServer, verify http://127.0.0.1:5000/login, and run manage.py check.
7. Do not delete the previous data copy until login, job history, and uploaded documents are verified.

This snapshot does not replace the source-code repository; source_commit_sha identifies the
application version that was active when the backup was created.
"@
    [IO.File]::WriteAllText((Join-Path $restoreRoot "RESTORE.txt"), $restoreNotes, (New-Object Text.UTF8Encoding($false)))

    $inventory = @()
    foreach ($file in (Get-ChildItem -LiteralPath $restoreRoot -File -Recurse -Force)) {
        $relative = $file.FullName.Substring($restoreRoot.Length).TrimStart([char[]]@('\','/'))
        $inventory += [ordered]@{
            path = ($relative -replace '\\','/')
            size = [long]$file.Length
            sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    $manifest = [ordered]@{
        schema_version = 1
        created_at_utc = [DateTime]::UtcNow.ToString("o")
        file_count = $inventory.Count
        files = $inventory
    }
    [IO.File]::WriteAllText((Join-Path $restoreRoot "SHA256SUMS.json"), (ConvertTo-Json -InputObject $manifest -Depth 8), (New-Object Text.UTF8Encoding($false)))

    $zipPath = Join-Path $runDir ("LocalPrintServer-" + $stamp + ".zip")
    $assetName = "LocalPrintServer-" + $stamp + ".lpsenc"
    $encryptedPath = Join-Path $runDir $assetName
    [IO.Compression.ZipFile]::CreateFromDirectory($restoreRoot, $zipPath, [IO.Compression.CompressionLevel]::Optimal, $false)
    $passphrase = Get-LpsProtectedSecret -Name "backup-passphrase.dpapi"
    Protect-LpsBackupArchive -InputPath $zipPath -OutputPath $encryptedPath -Passphrase $passphrase
    # Do not keep the plaintext ZIP on disk during the GitHub upload.
    Remove-Item -LiteralPath $zipPath -Force
    $encryptedHash = (Get-FileHash -LiteralPath $encryptedPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $encryptedSize = (Get-Item -LiteralPath $encryptedPath).Length
    if ($encryptedSize -lt 100) { throw "Encrypted backup archive is unexpectedly small." }

    $apiRoot = "https://api.github.com/repos/$($cfg.backup_repo)"
    $bodyText = "Encrypted LocalPrintServer snapshot created at $([DateTime]::UtcNow.ToString('o'))." + [Environment]::NewLine +
        "Source commit: $appliedCommit" + [Environment]::NewLine +
        "Asset: $assetName" + [Environment]::NewLine +
        "Asset SHA-256: $encryptedHash" + [Environment]::NewLine +
        "Bytes: $encryptedSize" + [Environment]::NewLine +
        "Retention: latest $($cfg.backup_retention_count) backup releases."
    $release = Invoke-LpsGitHubApi -TokenName "backup-write-token.dpapi" -Method POST -Uri "$apiRoot/releases" -Body @{
        tag_name = $backupTag
        target_commitish = "main"
        name = "LocalPrintServer backup $stamp"
        body = $bodyText
        draft = $false
        prerelease = $false
    }
    $uploadBase = ([string]$release.upload_url) -replace '\{\?name,label\}$',''
    $uploadUri = $uploadBase + "?name=" + [Uri]::EscapeDataString($assetName)
    $null = Invoke-LpsGitHubApi -TokenName "backup-write-token.dpapi" -Method POST -Uri $uploadUri -InFile $encryptedPath -ContentType "application/octet-stream" -TimeoutSec 1800
    $assetCheck = Invoke-LpsGitHubApi -TokenName "backup-write-token.dpapi" -Method GET -Uri "$apiRoot/releases/$($release.id)/assets"
    $verifiedAsset = @($assetCheck | Where-Object { $_.name -eq $assetName -and $_.state -eq "uploaded" -and [long]$_.size -eq [long]$encryptedSize }) | Select-Object -First 1
    if (-not $verifiedAsset) { throw "GitHub release asset was not verified after upload." }
    $uploaded = $true

    $done = [ordered]@{
        release_id = [long]$release.id
        tag_name = $backupTag
        asset_name = $assetName
        asset_sha256 = $encryptedHash
        encrypted_bytes = [long]$encryptedSize
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
        status = "success"
    }
    Write-LpsJsonAtomic -Path $statePath -Value $done
    Write-LpsLog "Nightly backup uploaded and verified: release $backupTag, $encryptedSize bytes, SHA256 $encryptedHash."

    # Keep the latest N encrypted backup releases. Delete only releases created by this script.
    $releases = @(Invoke-LpsGitHubApi -TokenName "backup-write-token.dpapi" -Method GET -Uri "$apiRoot/releases?per_page=100")
    $backupReleases = @($releases | Where-Object { $_.tag_name -match '^lps-backup-[0-9]{8}-[0-9]{6}$' } | Sort-Object published_at -Descending)
    $keep = [Math]::Max(2, [int]$cfg.backup_retention_count)
    $expired = @($backupReleases | Select-Object -Skip $keep)
    foreach ($old in $expired) {
        try {
            Invoke-LpsGitHubApi -TokenName "backup-write-token.dpapi" -Method DELETE -Uri "$apiRoot/releases/$($old.id)" | Out-Null
            $escapedTag = [Uri]::EscapeDataString([string]$old.tag_name)
            Invoke-LpsGitHubApi -TokenName "backup-write-token.dpapi" -Method DELETE -Uri "$apiRoot/git/refs/tags/$escapedTag" | Out-Null
            Write-LpsLog "Retention: deleted old backup release $($old.tag_name)."
        } catch {
            Write-LpsLog "Retention could not remove $($old.tag_name): $($_.Exception.Message)" "WARN"
        }
    }
    exit 0
} catch {
    Write-LpsLog "Nightly backup failed: $($_.Exception.Message)" "ERROR"
    if ($release -and -not $uploaded) {
        try { Invoke-LpsGitHubApi -TokenName "backup-write-token.dpapi" -Method DELETE -Uri "https://api.github.com/repos/$($cfg.backup_repo)/releases/$($release.id)" | Out-Null } catch {}
        try {
            $tagPath = [Uri]::EscapeDataString([string]$backupTag)
            Invoke-LpsGitHubApi -TokenName "backup-write-token.dpapi" -Method DELETE -Uri "https://api.github.com/repos/$($cfg.backup_repo)/git/refs/tags/$tagPath" | Out-Null
        } catch {}
    }
    exit 1
} finally {
    if ($runDir -and (Test-Path -LiteralPath $runDir)) {
        Remove-Item -LiteralPath $runDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
