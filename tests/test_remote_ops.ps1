$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$syntaxErrors = @()
Get-ChildItem -LiteralPath (Join-Path $repoRoot "scripts") -Filter "*.ps1" -File -Recurse | ForEach-Object {
    $tokens = $null
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors)
    foreach ($errorItem in $errors) {
        $syntaxErrors += "{0}:{1}:{2} {3}" -f $_.FullName, $errorItem.Extent.StartLineNumber,
            $errorItem.Extent.StartColumnNumber, $errorItem.Message
    }
}
if ($syntaxErrors.Count -gt 0) {
    $syntaxErrors | ForEach-Object { Write-Error $_ }
    exit 1
}
Write-Host "PowerShell syntax validation passed for all scripts."

. (Join-Path $repoRoot "scripts\remote_ops\ops-common.ps1")
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("lps-ops-test-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempRoot | Out-Null
try {
    $plainPath = Join-Path $tempRoot "source.bin"
    $encryptedPath = Join-Path $tempRoot "source.lpsenc"
    $decryptedPath = Join-Path $tempRoot "restored.bin"
    $tamperedPath = Join-Path $tempRoot "tampered.lpsenc"
    $tamperedOutput = Join-Path $tempRoot "tampered-output.bin"
    [byte[]]$payload = New-Object byte[] 8193
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($payload) } finally { $rng.Dispose() }
    [IO.File]::WriteAllBytes($plainPath, $payload)
    $passphrase = "test-passphrase-for-LocalPrintServer-ops"

    Protect-LpsBackupArchive -InputPath $plainPath -OutputPath $encryptedPath -Passphrase $passphrase
    Unprotect-LpsBackupArchive -InputPath $encryptedPath -OutputPath $decryptedPath -Passphrase $passphrase
    $originalHash = (Get-FileHash -LiteralPath $plainPath -Algorithm SHA256).Hash
    $restoredHash = (Get-FileHash -LiteralPath $decryptedPath -Algorithm SHA256).Hash
    if ($originalHash -ne $restoredHash) { throw "Encryption round-trip hash mismatch." }

    [byte[]]$encryptedBytes = [IO.File]::ReadAllBytes($encryptedPath)
    $encryptedBytes[45] = [byte]($encryptedBytes[45] -bxor 1)
    [IO.File]::WriteAllBytes($tamperedPath, $encryptedBytes)
    $tamperRejected = $false
    try {
        Unprotect-LpsBackupArchive -InputPath $tamperedPath -OutputPath $tamperedOutput -Passphrase $passphrase
    } catch {
        $tamperRejected = $true
    }
    if (-not $tamperRejected) { throw "Tampered encrypted backup was not rejected." }
    if (Test-Path -LiteralPath $tamperedOutput) { throw "Tampered backup produced an output file before authentication." }
    Write-Host "Backup crypto round-trip and tamper-rejection tests passed."
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
