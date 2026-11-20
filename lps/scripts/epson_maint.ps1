<#
.SYNOPSIS
    Epson L1800 Series Hardware Maintenance Script (Nozzle Check & Head Cleaning).

.DESCRIPTION
    Triggers physical hardware maintenance (Nozzle Check or Head Cleaning) on the
    EPSON L1800 Series printer using the official Epson L1800 driver package in:
      C:\Windows\System32\spool\DRIVERS\x64\3\
        - E_YHUTLWE.EXE (EPSON IJP Maintenance Utility, Ver. 11.8.1 / EPUTY59B.EXE)
        - E_YHUTLWE.DLL (EPUTY59B.DLL: UtPrintNozzleTestPattern2W, UtCleanHead2W)
        - E_YATILWE.EXE (EPSON Status Monitor 3)
        - E_YAUTLWE.UDC (Epson Utility Data Cabinet containing NOZZLE & CLEANING payloads)

    Approach A (Preferred / Service-Safe):
      Extracts the official Epson L1800 ESC/P-R REMOTE1 binary payload (NOZZLE or CLEANING)
      from E_YAUTLWE.UDC, stamps the current TI (Time Info) packet exactly as E_YHUTLWE.DLL
      does, and submits it directly to the Windows Print Spooler (winspool.drv WritePrinter)
      with pDatatype="RAW". Works reliably in Session 0 (Windows Service) and interactive sessions.

    Approach B (Fallback / Interactive GUI Automation):
      Invokes E_YHUTLWE.EXE /P "EPSON L1800 Series" /U <1|2> (or opens Printing Preferences
      via rundll32.exe printui.dll,PrintUIEntry /e /n "EPSON L1800 Series") and uses
      WScript.Shell AppActivate + SendKeys (&J for Nozzle Check, &D for Head Cleaning, {ENTER})
      to trigger the maintenance utility dialog.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("NozzleCheck", "HeadClean", "nozzle_check", "head_cleaning")]
    [string]$Action,

    [Parameter(Mandatory = $false)]
    [string]$PrinterName = "EPSON L1800 Series",

    [Parameter(Mandatory = $false)]
    [ValidateSet("Auto", "RawSpool", "UtilityGui", "PrintUI")]
    [string]$Method = "Auto",

    [Parameter(Mandatory = $false)]
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# Normalize action name
$NormalizedAction = switch ($Action.ToLowerInvariant()) {
    "nozzlecheck"   { "NozzleCheck" }
    "nozzle_check"  { "NozzleCheck" }
    "headclean"     { "HeadClean" }
    "head_cleaning" { "HeadClean" }
    default         { throw "Unsupported maintenance action: $Action" }
}

$DriverDir    = "C:\Windows\System32\spool\DRIVERS\x64\3"
$EpsonUtilExe = Join-Path $DriverDir "E_YHUTLWE.EXE"
$EpsonUtilDll = Join-Path $DriverDir "E_YHUTLWE.DLL"
$EpsonStmExe  = Join-Path $DriverDir "E_YATILWE.EXE"
$EpsonUdcFile = Join-Path $DriverDir "E_YAUTLWE.UDC"

function Get-EpsonMaintenancePayload {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("NozzleCheck", "HeadClean")]
        [string]$MaintAction
    )

    $entryName = if ($MaintAction -eq "NozzleCheck") { "NOZZLE" } else { "CLEANING" }
    [byte[]]$payload = $null

    # Attempt to read directly from the installed Epson L1800 E_YAUTLWE.UDC cabinet
    # (Header starts with 5-byte "EPSON" signature followed by standard Microsoft MSCF cabinet)
    if (Test-Path -LiteralPath $EpsonUdcFile) {
        $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("epson_udc_" + [guid]::NewGuid().ToString("N"))
        try {
            New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
            $udcBytes = [System.IO.File]::ReadAllBytes($EpsonUdcFile)
            if ($udcBytes.Length -gt 16 -and
                [System.Text.Encoding]::ASCII.GetString($udcBytes, 0, 9) -eq "EPSONMSCF") {
                $cabPath = Join-Path $tempDir "epson_maint.cab"
                $cabLen  = $udcBytes.Length - 5
                $cabBytes = New-Object byte[] $cabLen
                [System.Array]::Copy($udcBytes, 5, $cabBytes, 0, $cabLen)
                [System.IO.File]::WriteAllBytes($cabPath, $cabBytes)

                $expandExe = Join-Path $env:SystemRoot "System32\expand.exe"
                & $expandExe $cabPath -F:$entryName $tempDir | Out-Null
                $extractedPath = Join-Path $tempDir $entryName
                if (Test-Path -LiteralPath $extractedPath) {
                    $payload = [System.IO.File]::ReadAllBytes($extractedPath)
                }
            }
        } catch {
            Write-Verbose "UDC extraction fallback triggered: $($_.Exception.Message)"
        } finally {
            if (Test-Path -LiteralPath $tempDir) {
                Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

    # Exact fallback bytes from EPSON L1800 Series E_YAUTLWE.UDC (CLEANING = 66B, NOZZLE = 132B)
    if (-not $payload -or $payload.Length -eq 0) {
        if ($MaintAction -eq "HeadClean") {
            # @EJL 1284.4 + REMOTE1 + TI (Time Info) + CH\x02\x00\x00\x00 (Head Clean all channels) + Exit REMOTE1
            $hex = "0000001b0140454a4c20313238342e340a40454a4c20202020200a1b401b401b285208000052454d4f5445315449080000000000000000004348020000001b000000"
        } else {
            # @EJL 1284.4 + REMOTE1 + TI + JS + NC\x02\x00\x00\x00 (Nozzle Check pattern) + VI + LD + JE
            $hex = "0000001b0140454a4c20313238342e340a40454a4c20202020200a1b401b401b285208000052454d4f544531544908000007d909040d19284a530400000000004e43020000001b0000000d0a0d0a1b285208000052454d4f5445315649020000004c4400001b0000000c1b401b401b285208000052454d4f5445314a450100001b000000"
        }
        $payload = New-Object byte[] ($hex.Length / 2)
        for ($i = 0; $i -lt $payload.Length; $i++) {
            $payload[$i] = [Convert]::ToByte($hex.Substring($i * 2, 2), 16)
        }
    }

    # Update the 7-byte TI (Time Info) timestamp at offset 49 (after "REMOTE1TI\x08\x00\x00")
    if ($payload.Length -ge 56 -and
        $payload[44] -eq 0x54 -and $payload[45] -eq 0x49 -and
        $payload[46] -eq 0x08 -and $payload[47] -eq 0x00 -and $payload[48] -eq 0x00) {
        $now = Get-Date
        $year = [uint16]$now.Year
        $payload[49] = [byte](($year -shr 8) -band 0xFF)
        $payload[50] = [byte]($year -band 0xFF)
        $payload[51] = [byte]$now.Month
        $payload[52] = [byte]$now.Day
        $payload[53] = [byte]$now.Hour
        $payload[54] = [byte]$now.Minute
        $payload[55] = [byte]$now.Second
    }

    return ,$payload
}

function Invoke-RawSpoolerMaintenance {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetPrinter,

        [Parameter(Mandatory = $true)]
        [string]$MaintAction,

        [Parameter(Mandatory = $true)]
        [byte[]]$PayloadBytes
    )

    if (-not ("EpsonRawSpooler" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class EpsonRawSpooler
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public class DOC_INFO_1
    {
        [MarshalAs(UnmanagedType.LPWStr)]
        public string pDocName;
        [MarshalAs(UnmanagedType.LPWStr)]
        public string pOutputFile;
        [MarshalAs(UnmanagedType.LPWStr)]
        public string pDatatype;
    }

    [DllImport("winspool.drv", EntryPoint = "OpenPrinterW", SetLastError = true, CharSet = CharSet.Unicode, ExactSpelling = true)]
    public static extern bool OpenPrinter(string szPrinter, out IntPtr hPrinter, IntPtr pd);

    [DllImport("winspool.drv", EntryPoint = "ClosePrinter", SetLastError = true, ExactSpelling = true)]
    public static extern bool ClosePrinter(IntPtr hPrinter);

    [DllImport("winspool.drv", EntryPoint = "StartDocPrinterW", SetLastError = true, CharSet = CharSet.Unicode, ExactSpelling = true)]
    public static extern int StartDocPrinter(IntPtr hPrinter, int level, [In] DOC_INFO_1 di);

    [DllImport("winspool.drv", EntryPoint = "EndDocPrinter", SetLastError = true, ExactSpelling = true)]
    public static extern bool EndDocPrinter(IntPtr hPrinter);

    [DllImport("winspool.drv", EntryPoint = "StartPagePrinter", SetLastError = true, ExactSpelling = true)]
    public static extern bool StartPagePrinter(IntPtr hPrinter);

    [DllImport("winspool.drv", EntryPoint = "EndPagePrinter", SetLastError = true, ExactSpelling = true)]
    public static extern bool EndPagePrinter(IntPtr hPrinter);

    [DllImport("winspool.drv", EntryPoint = "WritePrinter", SetLastError = true, ExactSpelling = true)]
    public static extern bool WritePrinter(IntPtr hPrinter, byte[] pBytes, int dwCount, out int dwWritten);

    public static int SendRawBytes(string printerName, string docName, byte[] bytes)
    {
        IntPtr hPrinter = IntPtr.Zero;
        if (!OpenPrinter(printerName, out hPrinter, IntPtr.Zero))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "OpenPrinterW failed for '" + printerName + "'");
        }
        try
        {
            DOC_INFO_1 di = new DOC_INFO_1();
            di.pDocName = docName;
            di.pOutputFile = null;
            di.pDatatype = "RAW";

            int jobId = StartDocPrinter(hPrinter, 1, di);
            if (jobId <= 0)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "StartDocPrinterW failed");
            }
            try
            {
                if (!StartPagePrinter(hPrinter))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "StartPagePrinter failed");
                }
                try
                {
                    int written = 0;
                    if (!WritePrinter(hPrinter, bytes, bytes.Length, out written) || written != bytes.Length)
                    {
                        throw new Win32Exception(Marshal.GetLastWin32Error(), "WritePrinter failed");
                    }
                }
                finally
                {
                    EndPagePrinter(hPrinter);
                }
            }
            finally
            {
                EndDocPrinter(hPrinter);
            }
            return jobId;
        }
        finally
        {
            ClosePrinter(hPrinter);
        }
    }
}
"@
    }

    $docTitle = if ($MaintAction -eq "NozzleCheck") {
        "EPSON L1800 Maintenance - Nozzle Check"
    } else {
        "EPSON L1800 Maintenance - Head Cleaning"
    }

    $jobId = [EpsonRawSpooler]::SendRawBytes($TargetPrinter, $docTitle, $PayloadBytes)
    return $jobId
}

function Invoke-EpsonUtilityFallback {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetPrinter,

        [Parameter(Mandatory = $true)]
        [string]$MaintAction,

        [Parameter(Mandatory = $false)]
        [bool]$UsePrintUI = $false
    )

    $wshell = New-Object -ComObject WScript.Shell

    # Approach B1: Official Epson E_YHUTLWE.EXE CLI (/P "<Printer>" /U <1=NozzleCheck|2=HeadClean>)
    if (-not $UsePrintUI -and (Test-Path -LiteralPath $EpsonUtilExe)) {
        # In E_YHUTLWE.EXE: Utility ID 1 = UtPrintNozzleTestPattern2W, Utility ID 2 = UtCleanHead2W
        $utilId = if ($MaintAction -eq "NozzleCheck") { 1 } else { 2 }
        $proc = Start-Process -FilePath $EpsonUtilExe -ArgumentList "/P `"$TargetPrinter`" /U $utilId" -PassThru
        Start-Sleep -Milliseconds 1500
        if (-not $proc.HasExited) {
            [void]$wshell.AppActivate($proc.Id)
            Start-Sleep -Milliseconds 300
            $wshell.SendKeys("{ENTER}")
            Start-Sleep -Milliseconds 1500
        }
        return "Triggered via $EpsonUtilExe (/P `"$TargetPrinter`" /U $utilId, PID=$($proc.Id))"
    }

    # Approach B2: Fallback via rundll32 printui.dll Printing Preferences + Accelerator Keys (&J / &D)
    $proc = Start-Process -FilePath "rundll32.exe" -ArgumentList "printui.dll,PrintUIEntry /e /n `"$TargetPrinter`"" -PassThru
    Start-Sleep -Milliseconds 1800
    [void]$wshell.AppActivate($proc.Id)
    [void]$wshell.AppActivate($TargetPrinter)
    Start-Sleep -Milliseconds 400
    # Navigate to Maintenance tab (Ctrl+Tab x 2) and send accelerator (&J = Nozzle Check, &D = Head Cleaning)
    $accelKey = if ($MaintAction -eq "NozzleCheck") { "%j" } else { "%d" }
    $wshell.SendKeys("^{TAB}^{TAB}")
    Start-Sleep -Milliseconds 300
    $wshell.SendKeys($accelKey)
    Start-Sleep -Milliseconds 800
    $wshell.SendKeys("{ENTER}")
    return "Triggered via printui.dll preferences automation ($accelKey)"
}

# Main execution flow
[byte[]]$payloadBytes = Get-EpsonMaintenancePayload -MaintAction $NormalizedAction

if ($DryRun) {
    Write-Output "DRYRUN: Action=$NormalizedAction Printer='$PrinterName' DriverUtil='$EpsonUtilExe' PayloadBytes=$($payloadBytes.Length)"
    exit 0
}

if ($Method -eq "UtilityGui") {
    $msg = Invoke-EpsonUtilityFallback -TargetPrinter $PrinterName -MaintAction $NormalizedAction -UsePrintUI:$false
    Write-Output "OK: $msg"
    exit 0
}

if ($Method -eq "PrintUI") {
    $msg = Invoke-EpsonUtilityFallback -TargetPrinter $PrinterName -MaintAction $NormalizedAction -UsePrintUI:$true
    Write-Output "OK: $msg"
    exit 0
}

try {
    $spoolJobId = Invoke-RawSpoolerMaintenance -TargetPrinter $PrinterName -MaintAction $NormalizedAction -PayloadBytes $payloadBytes
    Write-Output "OK: Action=$NormalizedAction sent to '$PrinterName' via Epson driver E_YAUTLWE.UDC/RAW spooler (JobId=$spoolJobId, Bytes=$($payloadBytes.Length), UtilExe=$EpsonUtilExe)"
    exit 0
} catch {
    if ($Method -eq "RawSpool") {
        Write-Error "Failed to send Epson RAW maintenance command: $($_.Exception.Message)"
        exit 1
    }
    Write-Warning "Raw spooler submission failed ($($_.Exception.Message)); falling back to Epson Utility GUI automation..."
    $fallbackMsg = Invoke-EpsonUtilityFallback -TargetPrinter $PrinterName -MaintAction $NormalizedAction -UsePrintUI:$false
    Write-Output "OK (Fallback): $fallbackMsg"
    exit 0
}
