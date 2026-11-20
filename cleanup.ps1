<#
.SYNOPSIS
    LocalPrintServer 7-Day Rolling Archive Cleanup Script
.DESCRIPTION
    Deletes physical uploaded files and conversion temporary files older than 7 days
    based on CreationTime while preserving all SQLite database records.
#>
$ErrorActionPreference = "SilentlyContinue"
$CutoffDate = (Get-Date).AddDays(-7)

$TargetDirs = @(
    "C:\LocalPrintServer\data\uploads",
    "C:\LocalPrintServer\data\convert_tmp",
    "C:\LocalPrintServer\data\jobs"
)

$DeletedCount = 0
foreach ($Dir in $TargetDirs) {
    if (Test-Path -LiteralPath $Dir) {
        $OldItems = Get-ChildItem -LiteralPath $Dir -Recurse -Force |
            Where-Object { $_.CreationTime -lt $CutoffDate } |
            Sort-Object { $_.FullName.Length } -Descending

        foreach ($Item in $OldItems) {
            Remove-Item -LiteralPath $Item.FullName -Recurse -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path -LiteralPath $Item.FullName)) {
                $DeletedCount++
            }
        }
    }
}

Write-Output "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Cleanup complete: removed $DeletedCount expired item(s) older than $($CutoffDate.ToString('yyyy-MM-dd HH:mm:ss'))."
