# Shared helpers for scheduled LocalPrintServer maintenance tasks.
# Secrets are stored using DPAPI LocalMachine and never committed to Git.
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Security
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$script:LpsOpsRoot = "C:\ProgramData\LocalPrintServerOps"
$script:LpsSourceRepo = "<owner>/<private-source-repo>"
$script:LpsBackupRepo = "<owner>/<private-backup-repo>"
$script:LpsDpapiEntropy = [Text.Encoding]::UTF8.GetBytes("LocalPrintServerOps-DPAPI-v1")
$script:LpsBackupMagic = [Text.Encoding]::ASCII.GetBytes("LPSENC01")
$script:LpsBackupIterations = 310000

function Write-LpsLog {
    param([Parameter(Mandatory=$true)][string]$Message, [string]$Level = "INFO")
    $logPath = Join-Path $script:LpsOpsRoot "operations.log"
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    try {
        Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
        if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
            $old = Join-Path $script:LpsOpsRoot "operations.previous.log"
            Move-Item -LiteralPath $logPath -Destination $old -Force
        }
    } catch {
        try { Write-EventLog -LogName Application -Source "LocalPrintServerOps" -EntryType Error -EventId 5701 -Message $line -ErrorAction SilentlyContinue } catch {}
    }
}

function Get-LpsProtectedSecret {
    param([Parameter(Mandatory=$true)][ValidateSet("source-read-token.dpapi", "backup-write-token.dpapi", "backup-passphrase.dpapi")][string]$Name)
    $path = Join-Path $script:LpsOpsRoot $Name
    if (-not (Test-Path -LiteralPath $path)) { throw "Protected secret missing: $path. Run setup_remote_management.ps1 as Administrator." }
    $bytes = [IO.File]::ReadAllBytes($path)
    $plain = [Security.Cryptography.ProtectedData]::Unprotect($bytes, $script:LpsDpapiEntropy, [Security.Cryptography.DataProtectionScope]::LocalMachine)
    try { return [Text.Encoding]::UTF8.GetString($plain) }
    finally { [Array]::Clear($plain, 0, $plain.Length) }
}

function Invoke-LpsGitHubApi {
    param(
        [Parameter(Mandatory=$true)][ValidateSet("GET","POST","PATCH","DELETE")][string]$Method,
        [Parameter(Mandatory=$true)][string]$Uri,
        [object]$Body = $null,
        [string]$InFile = "",
        [string]$ContentType = "application/vnd.github+json",
        [int]$TimeoutSec = 120,
        [ValidateSet("source-read-token.dpapi", "backup-write-token.dpapi")][string]$TokenName = "source-read-token.dpapi"
    )
    $token = Get-LpsProtectedSecret -Name $TokenName
    $headers = @{
        Authorization = "Bearer $token"
        Accept = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
        "User-Agent" = "LocalPrintServerOps"
    }
    if ($InFile) {
        return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $headers -ContentType $ContentType -InFile $InFile -TimeoutSec $TimeoutSec -ErrorAction Stop
    }
    if ($null -ne $Body) {
        $json = ConvertTo-Json -InputObject $Body -Depth 12
        return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $headers -ContentType $ContentType -Body $json -TimeoutSec $TimeoutSec -ErrorAction Stop
    }
    return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $headers -TimeoutSec $TimeoutSec -ErrorAction Stop
}

function Get-LpsHttpStatus {
    param([System.Management.Automation.ErrorRecord]$ErrorRecord)
    try { return [int]$ErrorRecord.Exception.Response.StatusCode } catch { return 0 }
}

function Write-LpsJsonAtomic {
    param([Parameter(Mandatory=$true)][string]$Path, [Parameter(Mandatory=$true)][object]$Value)
    $temp = "$Path.$PID.tmp"
    $json = ConvertTo-Json -InputObject $Value -Depth 12
    [IO.File]::WriteAllText($temp, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temp -Destination $Path -Force
}

function Get-LpsDerivedKeys {
    param([Parameter(Mandatory=$true)][string]$Passphrase, [Parameter(Mandatory=$true)][byte[]]$Salt)
    $kdf = [Security.Cryptography.Rfc2898DeriveBytes]::new($Passphrase, $Salt, $script:LpsBackupIterations)
    try { return ,([byte[]]$kdf.GetBytes(64)) }
    finally { $kdf.Dispose() }
}

function Protect-LpsBackupArchive {
    param([Parameter(Mandatory=$true)][string]$InputPath, [Parameter(Mandatory=$true)][string]$OutputPath, [Parameter(Mandatory=$true)][string]$Passphrase)
    if ($Passphrase.Length -lt 15) { throw "Backup encryption passphrase must be at least 15 characters." }
    $salt = New-Object byte[] 16
    $iv = New-Object byte[] 16
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($salt); $rng.GetBytes($iv) } finally { $rng.Dispose() }
    $keys = Get-LpsDerivedKeys -Passphrase $Passphrase -Salt $salt
    [byte[]]$encKey = $keys[0..31]
    [byte[]]$macKey = $keys[32..63]
    [byte[]]$header = $script:LpsBackupMagic + $salt + $iv
    $aes = [Security.Cryptography.Aes]::Create()
    $aes.KeySize = 256; $aes.BlockSize = 128; $aes.Mode = [Security.Cryptography.CipherMode]::CBC
    $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7; $aes.Key = $encKey; $aes.IV = $iv
    try {
        $source = [IO.File]::OpenRead($InputPath)
        $dest = [IO.File]::Create($OutputPath)
        try {
            $dest.Write($header, 0, $header.Length)
            $crypto = [Security.Cryptography.CryptoStream]::new($dest, $aes.CreateEncryptor(), [Security.Cryptography.CryptoStreamMode]::Write)
            try { $source.CopyTo($crypto); $crypto.FlushFinalBlock() }
            finally { $crypto.Dispose() }
        } finally { $source.Dispose(); $dest.Dispose() }

        # Authenticate header + ciphertext (encrypt-then-MAC) before publishing the archive.
        $mac = [Security.Cryptography.HMACSHA256]::new($macKey)
        try {
            $verify = [IO.File]::OpenRead($OutputPath)
            try { [byte[]]$tag = $mac.ComputeHash($verify) } finally { $verify.Dispose() }
            $append = [IO.File]::Open($OutputPath, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try { $append.Write($tag, 0, $tag.Length) } finally { $append.Dispose() }
        } finally { $mac.Dispose() }
    } finally {
        $aes.Dispose()
        [Array]::Clear($keys, 0, $keys.Length)
        [Array]::Clear($encKey, 0, $encKey.Length)
        [Array]::Clear($macKey, 0, $macKey.Length)
    }
}

function Unprotect-LpsBackupArchive {
    param([Parameter(Mandatory=$true)][string]$InputPath, [Parameter(Mandatory=$true)][string]$OutputPath, [Parameter(Mandatory=$true)][string]$Passphrase)
    $input = [IO.File]::OpenRead($InputPath)
    $payloadTemp = "$OutputPath.payload.tmp"
    try {
        if ($input.Length -lt 88) { throw "Backup file is too short to be a valid LPS encrypted archive." }
        [byte[]]$header = New-Object byte[] 40
        if ($input.Read($header, 0, 40) -ne 40) { throw "Could not read backup header." }
        if ([Text.Encoding]::ASCII.GetString($header, 0, 8) -ne "LPSENC01") { throw "Unsupported encrypted backup format." }
        [byte[]]$salt = $header[8..23]
        [byte[]]$iv = $header[24..39]
        $payloadLength = $input.Length - 32
        $input.Position = $payloadLength
        [byte[]]$expectedTag = New-Object byte[] 32
        if ($input.Read($expectedTag, 0, 32) -ne 32) { throw "Could not read backup authentication tag." }

        $keys = Get-LpsDerivedKeys -Passphrase $Passphrase -Salt $salt
        [byte[]]$encKey = $keys[0..31]
        [byte[]]$macKey = $keys[32..63]
        try {
            $mac = [Security.Cryptography.HMACSHA256]::new($macKey)
            try {
                $input.Position = 0
                $remaining = $payloadLength
                $buffer = New-Object byte[] (1024 * 1024)
                while ($remaining -gt 0) {
                    $want = [int][Math]::Min([long]$buffer.Length, $remaining)
                    $read = $input.Read($buffer, 0, $want)
                    if ($read -le 0) { throw "Encrypted backup ended unexpectedly during verification." }
                    $null = $mac.TransformBlock($buffer, 0, $read, $buffer, 0)
                    $remaining -= $read
                }
                $null = $mac.TransformFinalBlock([byte[]]@(), 0, 0)
                [byte[]]$actualTag = $mac.Hash
            } finally { $mac.Dispose() }
            $same = $actualTag.Length -eq $expectedTag.Length
            for ($i = 0; $i -lt $expectedTag.Length -and $same; $i++) {
                if ($actualTag[$i] -ne $expectedTag[$i]) { $same = $false }
            }
            if (-not $same) { throw "Backup authentication failed: wrong passphrase or damaged/tampered file." }

            # Copy authenticated bytes except the MAC so CryptoStream stops at ciphertext EOF.
            $input.Position = 0
            $bounded = [IO.File]::Create($payloadTemp)
            try {
                $remaining = $payloadLength
                $buffer = New-Object byte[] (1024 * 1024)
                while ($remaining -gt 0) {
                    $want = [int][Math]::Min([long]$buffer.Length, $remaining)
                    $read = $input.Read($buffer, 0, $want)
                    if ($read -le 0) { throw "Encrypted backup ended unexpectedly during extraction." }
                    $bounded.Write($buffer, 0, $read)
                    $remaining -= $read
                }
            } finally { $bounded.Dispose() }

            $aes = [Security.Cryptography.Aes]::Create()
            $aes.KeySize = 256; $aes.BlockSize = 128; $aes.Mode = [Security.Cryptography.CipherMode]::CBC
            $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7; $aes.Key = $encKey; $aes.IV = $iv
            try {
                $payload = [IO.File]::OpenRead($payloadTemp)
                try {
                    $payload.Position = 40
                    $decrypt = [Security.Cryptography.CryptoStream]::new($payload, $aes.CreateDecryptor(), [Security.Cryptography.CryptoStreamMode]::Read)
                    $output = [IO.File]::Create($OutputPath)
                    try { $decrypt.CopyTo($output) }
                    finally { $output.Dispose(); $decrypt.Dispose() }
                } finally { $payload.Dispose() }
            } finally { $aes.Dispose() }
        } finally {
            [Array]::Clear($keys, 0, $keys.Length)
            [Array]::Clear($encKey, 0, $encKey.Length)
            [Array]::Clear($macKey, 0, $macKey.Length)
        }
    } finally {
        $input.Dispose()
        Remove-Item -LiteralPath $payloadTemp -Force -ErrorAction SilentlyContinue
    }
}
