$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
$pidFile = Join-Path $logs "pay153.pid"
$envFile = Join-Path $root ".env"

function Load-DotEnv {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $parts = $trimmed -split "=", 2
        if ($parts.Count -ne 2) {
            continue
        }

        $name = $parts[0].Trim()
        $value = $parts[1].Trim()
        if (-not $name) {
            continue
        }

        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Get-ProcessCommandLine {
    param([int]$ProcessId)

    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        return [string]$process.CommandLine
    } catch {
        return ""
    }
}

function Test-Pay153Process {
    param(
        [int]$ProcessId,
        [string]$ProjectRoot
    )

    $commandLine = (Get-ProcessCommandLine -ProcessId $ProcessId).ToLowerInvariant()
    if (-not $commandLine) {
        return $false
    }

    $rootText = $ProjectRoot.ToLowerInvariant()
    if ($commandLine.Contains($rootText) -and (
        $commandLine.Contains("app.py") -or
        $commandLine.Contains("flask.exe") -or
        $commandLine.Contains("--app app") -or
        $commandLine.Contains("run-pay153.ps1")
    )) {
        return $true
    }

    return $commandLine.Contains(" app.py") -or $commandLine.EndsWith("\app.py")
}

New-Item -ItemType Directory -Force -Path $logs | Out-Null
Set-Location -LiteralPath $root
Load-DotEnv -Path $envFile

$bindPort = if ($env:PAY153_PORT) { [int]$env:PAY153_PORT } else { 18082 }
$targetPids = @()

if (Test-Path -LiteralPath $pidFile) {
    $pidText = (Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue).Trim()
    [int]$savedPid = 0
    if ([int]::TryParse($pidText, [ref]$savedPid)) {
        if ((Get-Process -Id $savedPid -ErrorAction SilentlyContinue) -and
            (Test-Pay153Process -ProcessId $savedPid -ProjectRoot $root)) {
            $targetPids += $savedPid
        }
    }
}

$listeners = Get-NetTCPConnection -LocalPort $bindPort -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    [int]$ownerPid = $listener.OwningProcess
    if ((Test-Pay153Process -ProcessId $ownerPid -ProjectRoot $root)) {
        $targetPids += $ownerPid
    }
}

$targetPids = $targetPids | Select-Object -Unique
if (-not $targetPids -or $targetPids.Count -eq 0) {
    if (Test-Path -LiteralPath $pidFile) {
        Remove-Item -LiteralPath $pidFile -Force
    }
    Write-Output "PAY153 is not running."
    exit 0
}

foreach ($targetPid in $targetPids) {
    Write-Output "Stopping PAY153 PID $targetPid..."
    $targetProcess = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
    if ($targetProcess) {
        Stop-Process -Id $targetPid -Force -ErrorAction Stop
    }
}

Start-Sleep -Milliseconds 500
if (Test-Path -LiteralPath $pidFile) {
    Remove-Item -LiteralPath $pidFile -Force
}

Write-Output "PAY153 stopped."
exit 0
