[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InputFile,
    [string]$OutputZip = "",
    [switch]$ExtractAndVerify
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "ops-common.ps1")

if (-not (Test-Path -LiteralPath $InputFile -PathType Leaf)) { throw "Encrypted backup not found: $InputFile" }
$InputFile = (Resolve-Path -LiteralPath $InputFile).Path
if (-not $OutputZip) { $OutputZip = [IO.Path]::ChangeExtension($InputFile, ".zip") }
$OutputZip = [IO.Path]::GetFullPath($OutputZip)
if (Test-Path -LiteralPath $OutputZip) { throw "Output file already exists; choose another path so an older file is not overwritten: $OutputZip" }

$secure = Read-Host "Enter the backup encryption passphrase (not your GitHub token)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $passphrase = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }

Unprotect-LpsBackupArchive -InputPath $InputFile -OutputPath $OutputZip -Passphrase $passphrase
Write-Host "Decrypted archive authentication passed: $OutputZip" -ForegroundColor Green

if ($ExtractAndVerify) {
    $verifyDir = "$OutputZip.verify"
    if (Test-Path -LiteralPath $verifyDir) { throw "Verification output already exists: $verifyDir" }
    New-Item -ItemType Directory -Path $verifyDir -Force | Out-Null
    try {
        Expand-Archive -LiteralPath $OutputZip -DestinationPath $verifyDir
        $manifestPath = Join-Path $verifyDir "SHA256SUMS.json"
        if (-not (Test-Path -LiteralPath $manifestPath)) { throw "Archive has no SHA256SUMS.json." }
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        $checked = 0
        foreach ($entry in $manifest.files) {
            $relative = ([string]$entry.path -replace '/', '\')
            $path = [IO.Path]::GetFullPath((Join-Path $verifyDir $relative))
            $rootPrefix = [IO.Path]::GetFullPath($verifyDir).TrimEnd('\') + '\'
            if (-not $path.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe path in manifest: $($entry.path)" }
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "File missing from archive: $($entry.path)" }
            $file = Get-Item -LiteralPath $path
            if ([long]$file.Length -ne [long]$entry.size) { throw "File size mismatch: $($entry.path)" }
            $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($hash -ne [string]$entry.sha256) { throw "SHA-256 mismatch: $($entry.path)" }
            $checked++
        }
        Write-Host "Verified $checked archived files against SHA-256 manifest. Extracted verification copy: $verifyDir" -ForegroundColor Green
    } catch {
        Remove-Item -LiteralPath $verifyDir -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
}
