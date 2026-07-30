$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$logDirectory = Join-Path $projectRoot "logs"
$logPath = Join-Path $logDirectory "five_minute_pipeline.log"
$python = "C:\Users\gunji\AppData\Local\Programs\Python\Python314\python.exe"

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
Set-Location $projectRoot

function Write-Log {
    param([string]$Message)
    "[$(Get-Date -Format s)] $Message" | Out-File -FilePath $logPath -Append -Encoding utf8
}

Write-Log "Starting five-minute candle pipeline"
try {
    # Redirect every native-process stream directly to the log.  Piping a
    # Python logging message through PowerShell can turn stderr into an error
    # record and prematurely terminate this launcher under Stop preference.
    & $python -m scheduler.run_5min_pipeline *>> $logPath

    if ($LASTEXITCODE -ne 0) {
        throw "Five-minute pipeline exited with code $LASTEXITCODE"
    }

    Write-Log "Five-minute candle pipeline completed"
}
catch {
    Write-Log "Five-minute candle pipeline failed: $($_.Exception.Message)"
    exit 1
}
