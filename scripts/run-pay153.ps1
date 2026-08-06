param(
    [Parameter(Mandatory = $true)]
    [string]$Root
)

$ErrorActionPreference = "Stop"

$logs = Join-Path $Root "logs"
$outLog = Join-Path $logs "flask.out.log"
$errLog = Join-Path $logs "flask.err.log"
$pythonExe = Join-Path $Root ".venv\Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $logs | Out-Null
Set-Location -LiteralPath $Root

$process = Start-Process `
    -FilePath $pythonExe `
    -ArgumentList @("app.py") `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru

$process.WaitForExit()
exit $process.ExitCode
