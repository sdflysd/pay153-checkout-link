$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
$pidFile = Join-Path $logs "pay153.pid"
$outLog = Join-Path $logs "flask.out.log"
$errLog = Join-Path $logs "flask.err.log"
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

function Get-RunningPay153Process {
    param(
        [string]$ProjectRoot,
        [int]$Port
    )

    if (Test-Path -LiteralPath $pidFile) {
        $pidText = (Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue).Trim()
        [int]$savedPid = 0
        if ([int]::TryParse($pidText, [ref]$savedPid)) {
            $savedProcess = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
            if ($savedProcess -and (Test-Pay153Process -ProcessId $savedPid -ProjectRoot $ProjectRoot)) {
                return $savedProcess
            }
        }
    }

    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener -and (Test-Pay153Process -ProcessId $listener.OwningProcess -ProjectRoot $ProjectRoot)) {
        return Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    }

    return $null
}

New-Item -ItemType Directory -Force -Path $logs | Out-Null
Set-Location -LiteralPath $root
Load-DotEnv -Path $envFile

$bindHost = if ($env:PAY153_HOST) { $env:PAY153_HOST } else { "127.0.0.1" }
$bindPort = if ($env:PAY153_PORT) { [int]$env:PAY153_PORT } else { 18082 }
$displayHost = if ($bindHost -eq "0.0.0.0" -or $bindHost -eq "::") { "127.0.0.1" } else { $bindHost }
$healthUrl = "http://${displayHost}:$bindPort/api/health"
$appUrl = "http://${displayHost}:$bindPort"

$running = Get-RunningPay153Process -ProjectRoot $root -Port $bindPort
if ($running) {
    Set-Content -LiteralPath $pidFile -Value ([string]$running.Id) -Encoding ascii
    Write-Output "PAY153 is already running."
    Write-Output "PID: $($running.Id)"
    Write-Output "URL: $appUrl"
    exit 0
}

$occupied = Get-NetTCPConnection -LocalPort $bindPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($occupied) {
    Write-Output "Port $bindPort is already in use by PID $($occupied.OwningProcess)."
    Write-Output "PAY153 was not started."
    exit 1
}

$pythonExe = Join-Path $root ".venv\Scripts\python.exe"
$flaskExe = Join-Path $root ".venv\Scripts\flask.exe"
$runnerScript = Join-Path $PSScriptRoot "run-pay153.ps1"
$packageJson = Join-Path $root "package.json"
$jsdomPackage = Join-Path $root "node_modules\jsdom\package.json"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    Write-Output "Creating virtual environment..."
    & py -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create virtual environment."
    }
}

if (-not (Test-Path -LiteralPath $flaskExe)) {
    Write-Output "Installing Python dependencies..."
    & $pythonExe -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip."
    }
    & $pythonExe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install dependencies."
    }
}

if ((Test-Path -LiteralPath $packageJson) -and -not (Test-Path -LiteralPath $jsdomPackage)) {
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) {
        $npm = Get-Command npm -ErrorAction SilentlyContinue
    }
    if (-not $npm) {
        throw "Node.js/npm is required for Sentinel token generation dependencies."
    }

    Write-Output "Installing Node dependencies..."
    & $npm.Source install --omit=dev
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install Node dependencies."
    }
}

[Environment]::SetEnvironmentVariable("PAY153_HOST", $bindHost, "Process")
[Environment]::SetEnvironmentVariable("PAY153_PORT", [string]$bindPort, "Process")

$process = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$runnerScript`"", "-Root", "`"$root`"") `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $pidFile -Value ([string]$process.Id) -Encoding ascii
Write-Output "Starting PAY153..."
Write-Output "PID: $($process.Id)"

for ($attempt = 1; $attempt -le 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
        Write-Output "PAY153 exited during startup."
        Write-Output "See: $errLog"
        exit 1
    }

    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($health.ok) {
            Write-Output "PAY153 started successfully."
            Write-Output "URL: $appUrl"
            exit 0
        }
    } catch {
        continue
    }
}

Write-Output "PAY153 process is running, but health check did not respond in time."
Write-Output "URL: $appUrl"
Write-Output "See: $errLog"
exit 1
